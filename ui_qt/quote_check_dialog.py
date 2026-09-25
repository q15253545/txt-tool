"""引號與標點檢查結果，以及自動修正。

有明確答案的問題（兩段對話黏在一起、標點在行首、對話中途斷行、引號方向
顛倒、少了開引號）可以勾起來一次修正，「修正後」欄先顯示改完的樣子；看不出
正確寫法的只列出來、點一下跳到那一行，由使用者自己在本文裡改。
"""

import difflib
import re

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTableWidget, QTableWidgetItem,
    QVBoxLayout,
)

from core.quote_check import QUOTE_PROBLEM_LABELS, apply_fixes, scan_quote_problems
from . import dialogs, i18n
from .sortable_table import data_index, enable_sorting, make_item, resort, setup_columns
from .theme import active_tokens
from .widgets import Divider, flow_container, size_dialog

# 每種問題該怎麼看待，寫在勾選框的提示裡。
_KIND_TIPS = {
    "unclosed": "這一行有開引號，但到行尾都沒有收尾——通常是一段對話被硬生生斷成兩行。",
    "unpaired": "收尾引號找不到對應的開引號，或巢狀順序錯亂。",
    "leading_punct": "行首就是逗號、句號這類標點，代表上一行被截斷了。",
    "missing_separator": "上一句的收尾引號後面直接接下一句的開引號，兩個人的對話黏在同一行。",
}

_FIX_COLUMN, _KIND_COLUMN, _TEXT_COLUMN, _AFTER_COLUMN = range(4)


def _one_line(text: str) -> str:
    # 不在這裡截斷：改動可能在很後面，截掉就看不到了；太長的由 _DiffDelegate
    # 照欄寬省略，而且會捲到改動的地方。
    return re.sub(r"\s+", " ", text).strip()


_CHANGED_ROLE = Qt.ItemDataRole.UserRole + 20   # 要標紅的字元位置
_FOCUS_ROLE = Qt.ItemDataRole.UserRole + 21     # 第一個改動的位置（太長時從這附近開始顯示）


def _diff_marks(before: str, after: str):
    """比對修正前後，回傳（after 裡要標紅的字元位置, before 的第一個改動位置,
    after 的第一個改動位置）。純刪除（例如兩行接回一行）沒有新字，標在接起來
    的那個字上，才看得出是在哪裡接的。"""
    changed = set()
    first_before = first_after = None
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    for tag, i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if first_before is None:
            first_before, first_after = i1, j1
        if j2 > j1:
            changed.update(range(j1, j2))
        elif j1 < len(after):
            changed.add(j1)
    return changed, first_before, first_after


class _DiffDelegate(QStyledItemDelegate):
    """「內容」「修正後」兩欄：改動的字用紅色粗體；一行太長時，不是從頭顯示到
    欄寬為止，而是從改動前一點開始顯示（前面用「…」代替），改動一定看得到。

    “ 和 ” 在小字下幾乎一模一樣，不標出來的話，修正前後看起來完全相同。"""

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        if not text:
            return
        rect = QRectF(style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, widget))
        rect.adjust(8, 0, -8, 0)          # 跟樣式表的儲存格左右 padding 一致
        changed = index.data(_CHANGED_ROLE) or ()
        focus = index.data(_FOCUS_ROLE)
        tokens = active_tokens()
        normal_font = QFont(opt.font)
        bold_font = QFont(opt.font)
        bold_font.setBold(True)
        normal_metrics, bold_metrics = QFontMetricsF(normal_font), QFontMetricsF(bold_font)

        def advance(position):
            metrics = bold_metrics if position in changed else normal_metrics
            return metrics.horizontalAdvance(text[position])

        ellipsis_width = normal_metrics.horizontalAdvance("…")
        available = rect.width()
        start = 0
        if focus is not None and sum(advance(i) for i in range(len(text))) > available:
            lead = sum(advance(i) for i in range(focus))
            if lead > available * 0.35:
                # 改動前面留大約三成欄寬的上下文。
                start, budget = focus, available * 0.3 - ellipsis_width
                while start > 0 and budget - advance(start - 1) > 0:
                    budget -= advance(start - 1)
                    start -= 1

        painter.save()
        x = rect.left()
        right = rect.right()
        normal_color, diff_color = QColor(tokens.text), QColor(tokens.diff_text)
        if start > 0:
            painter.setFont(normal_font)
            painter.setPen(normal_color)
            painter.drawText(QRectF(x, rect.top(), ellipsis_width, rect.height()),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "…")
            x += ellipsis_width
        position = start
        while position < len(text):
            marked = position in changed
            end = position
            while end < len(text) and (end in changed) == marked:
                end += 1
            font, metrics = (bold_font, bold_metrics) if marked else (normal_font, normal_metrics)
            run = text[position:end]
            width = metrics.horizontalAdvance(run)
            last_run = end >= len(text)
            limit = right - x - (0 if last_run else ellipsis_width)
            truncated = width > limit
            if truncated:
                # 放不下：剩下的空間塞幾個字，後面接「…」。
                cut = 0
                while cut < len(run) and metrics.horizontalAdvance(run[:cut + 1]) <= right - x - ellipsis_width:
                    cut += 1
                run = run[:cut]
                width = metrics.horizontalAdvance(run)
            painter.setFont(font)
            painter.setPen(diff_color if marked else normal_color)
            painter.drawText(QRectF(x, rect.top(), width + 1, rect.height()),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, run)
            x += width
            if truncated:
                painter.setFont(normal_font)
                painter.setPen(normal_color)
                painter.drawText(QRectF(x, rect.top(), ellipsis_width + 1, rect.height()),
                                 Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "…")
                break
            position = end
        painter.restore()


class QuoteCheckDialog(QDialog):
    """非模式對話框：開著時可以直接在本文手動修改。

    按「修正勾選的項目」時送出 fixesReady（修正後的整份本文, 修了幾處），由主
    視窗套用；對話框不關，主視窗再用 reload() 換成新的本文重新檢查，剩下要
    手動處理的項目繼續留在清單上。"""

    problemSelected = Signal(int)   # 1 起算的行號
    fixesReady = Signal(list, int)

    def __init__(self, raw_lines: list, parent=None, selected_ranges=None, selected_count: int = 0,
                 enabled_kinds=None):
        super().__init__(parent)
        self.setWindowTitle("引號與標點檢查")
        size_dialog(self, 960, 640)
        self._raw_lines = list(raw_lines)
        self._selected_ranges = list(selected_ranges or [])
        self._all_problems: list = []
        self._visible: list[int] = []          # 目前表格顯示的是 _all_problems 的哪幾筆
        self._checked: set[int] = set()
        self.result_lines: list | None = None
        self.applied_count = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        title = QLabel("選擇檢查項目")
        title.setObjectName("appTitle")
        root.addWidget(title)

        kind_box, kind_flow = flow_container(uniform=True)
        self._kind_checks = {}
        for key, label in QUOTE_PROBLEM_LABELS.items():
            checkbox = QCheckBox(label)
            checkbox.setChecked(enabled_kinds is None or key in enabled_kinds)
            checkbox.setToolTip(_KIND_TIPS.get(key, ""))
            checkbox.toggled.connect(self._refresh)
            self._kind_checks[key] = checkbox
            kind_flow.addWidget(checkbox)
        root.addWidget(kind_box)
        # 「只檢查選取的章節」是範圍，不是檢查項目，用分隔線隔開。
        root.addWidget(Divider())

        # 只選了一章時不預設打勾：點目錄是用來跳到那一章看內容的，幾乎隨時都
        # 有一個被選著；預設只掃那一章的話，掃不到東西會讓人以為整本都沒問題。
        # 刻意多選兩章以上，才當成「只想處理這幾章」。
        self.scope_check = QCheckBox("")
        self._set_scope(self._selected_ranges, selected_count)
        self.scope_check.setChecked(bool(self._selected_ranges) and selected_count >= 2)
        self.scope_check.toggled.connect(self._run_scan)
        root.addWidget(self.scope_check)

        select_row = QHBoxLayout()
        select_row.setSpacing(8)
        for label, slot in (("勾選可自動修正的項目", self._check_fixable), ("全部取消", self._uncheck_all)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            select_row.addWidget(button)
        select_row.addStretch(1)
        root.addLayout(select_row)

        self.status_label = QLabel("尚未檢查")
        self.status_label.setObjectName("fileLabel")
        root.addWidget(self.status_label)

        # 不放行號：點一下就會跳到本文那一行，行號本身沒有用處；
        # 「還原原本順序」就是照行號排（見 sortable_table 的三段排序）。
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["修正", "類型", "內容", "修正後"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # 「內容」與「修正後」都是長文字：內容先給表格寬度的四成，修正後吃剩下的。
        setup_columns(self.table, {_FIX_COLUMN: "contents", _KIND_COLUMN: "contents", _TEXT_COLUMN: 0.45})
        self._diff_delegate = _DiffDelegate(self.table)
        self.table.setItemDelegateForColumn(_TEXT_COLUMN, self._diff_delegate)
        self.table.setItemDelegateForColumn(_AFTER_COLUMN, self._diff_delegate)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        enable_sorting(self.table)
        root.addWidget(self.table, 1)

        buttons = QDialogButtonBox()
        close_button = buttons.addButton("關閉", QDialogButtonBox.ButtonRole.RejectRole)
        self.fix_button = buttons.addButton("修正勾選的項目", QDialogButtonBox.ButtonRole.AcceptRole)
        self.fix_button.setObjectName("primary")
        close_button.clicked.connect(self.reject)
        self.fix_button.clicked.connect(self._apply_fixes)
        root.addWidget(buttons)

        self._run_scan()

    # ------------------------------------------------------------------

    def enabled_kinds(self) -> set:
        return {key for key, box in self._kind_checks.items() if box.isChecked()}

    def _set_scope(self, selected_ranges, selected_count: int):
        self._selected_ranges = list(selected_ranges or [])
        i18n.set_text(self.scope_check,
                      f"只檢查選取的 {selected_count} 個章節" if selected_count else "只檢查選取的章節")
        self.scope_check.setEnabled(bool(self._selected_ranges))
        if not self._selected_ranges:
            self.scope_check.setChecked(False)
        self.scope_check.setToolTip("" if self._selected_ranges
                                    else i18n.T("先在目錄選取章節，再開啟檢查，就可以只檢查那幾章"))

    @staticmethod
    def _problem_key(problem):
        return problem["kind"], problem["preview"]

    def reload(self, raw_lines, selected_ranges=None, selected_count: int = 0):
        """本文改過了（使用者手動修改，或剛套用完自動修正）：用新的本文重新檢查。
        勾選狀態與目前選取的那一筆照內容對回去，不會因為行號位移而跑掉。"""
        checked = {self._problem_key(self._all_problems[index]) for index in self._checked}
        rows = self.table.selectionModel().selectedRows()
        current = self._problem_key(self._all_problems[data_index(self.table, rows[0].row())]) if rows else None
        self._raw_lines = list(raw_lines)
        self.scope_check.blockSignals(True)
        self._set_scope(selected_ranges, selected_count)
        self.scope_check.blockSignals(False)
        self._run_scan()
        self._checked = {index for index, problem in enumerate(self._all_problems)
                         if problem["fix"] and self._problem_key(problem) in checked}
        self._refresh()
        if current is not None:
            for row in range(self.table.rowCount()):
                if self._problem_key(self._all_problems[data_index(self.table, row)]) == current:
                    self.table.blockSignals(True)
                    self.table.selectRow(row)
                    self.table.blockSignals(False)
                    break

    def _run_scan(self, *_args):
        ranges = self._selected_ranges if self.scope_check.isChecked() else None
        self._all_problems = scan_quote_problems(self._raw_lines, ranges)
        self._checked = set()
        self._refresh()

    def _refresh(self, *_args):
        kinds = self.enabled_kinds()
        self._visible = [index for index, problem in enumerate(self._all_problems) if problem["kind"] in kinds]
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._visible))
        for row, index in enumerate(self._visible):
            problem = self._all_problems[index]
            fix = problem["fix"]
            check_item = make_item("", index, 0 if fix else 1)
            if fix:
                check_item.setFlags(
                    Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                check_item.setCheckState(Qt.CheckState.Checked if index in self._checked else Qt.CheckState.Unchecked)
            else:
                check_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, _FIX_COLUMN, check_item)
            self.table.setItem(row, _KIND_COLUMN, make_item(i18n.T(problem["label"]), index))
            text_item = make_item(_one_line(problem["preview"]), index)
            self.table.setItem(row, _TEXT_COLUMN, text_item)
            if fix:
                after = _one_line(" ⏎ ".join(fix["after"]))
                after_item = make_item(after, index)
                before = _one_line(" ⏎ ".join(self._raw_lines[fix["start"]:fix["end"]]))
                changed, _first_before, first_after = _diff_marks(before, after)
                after_item.setData(_CHANGED_ROLE, sorted(changed))
                after_item.setData(_FOCUS_ROLE, first_after)
                # 「內容」只有問題那一行，另外比一次，才知道要捲到哪裡。
                _changed, first_text, _first = _diff_marks(text_item.text(), after)
                text_item.setData(_FOCUS_ROLE, first_text)
            else:
                after_item = make_item(i18n.T("需手動處理"), index)
            self.table.setItem(row, _AFTER_COLUMN, after_item)
        self.table.blockSignals(False)
        resort(self.table)
        self._update_status()

    def _update_status(self):
        if not self._all_problems:
            text = "沒有發現引號或標點問題"
        else:
            fixable = sum(1 for index in self._visible if self._all_problems[index]["fix"])
            text = (f"共 {len(self._visible)} 處，其中 {fixable} 處可以自動修正；"
                    f"已勾選 {len(self._checked)} 處")
        i18n.set_text(self.status_label, text)
        self.fix_button.setEnabled(bool(self._checked))

    def _on_item_changed(self, item: QTableWidgetItem):
        if item.column() != _FIX_COLUMN:
            return
        index = data_index(self.table, item.row())
        if item.checkState() == Qt.CheckState.Checked:
            self._checked.add(index)
        else:
            self._checked.discard(index)
        self._update_status()

    def _on_selection_changed(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        problem = self._all_problems[data_index(self.table, rows[0].row())]
        self.problemSelected.emit(problem["line"])

    def _check_fixable(self):
        self._checked |= {index for index in self._visible if self._all_problems[index]["fix"]}
        self._refresh()

    def _uncheck_all(self):
        self._checked = set()
        self._refresh()

    def _apply_fixes(self):
        plans = [self._all_problems[index]["fix"] for index in sorted(self._checked)
                 if self._all_problems[index]["fix"]]
        if not plans:
            dialogs.info(self, "尚未勾選", "請先勾選要自動修正的項目。")
            return
        self.result_lines, self.applied_count = apply_fixes(self._raw_lines, plans)
        self.fixesReady.emit(self.result_lines, self.applied_count)
