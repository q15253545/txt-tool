"""廣告掃描對話框。

非模式：開著的時候可以直接在本文手動刪掉沒被找到的廣告。對話框拿一份
raw_lines 快照作業；本文改過之後，主視窗會呼叫 reload() 換成新的一份重新
掃描（勾選狀態照內容對回去）。按「刪除已勾選項目」送出 deletionReady，由主
視窗套用，對話框不關，可以繼續處理剩下的。
"""

import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from core.ad_scan import AD_CATEGORY_LABELS, scan_ad_candidates
from . import dialogs, i18n
from .widgets import Divider, flow_container, size_dialog
from .sortable_table import CONFIDENCE_ORDER, data_index, enable_sorting, make_item, resort, setup_columns

_CONFIDENCE_BY_BUTTON = {"高信心": "高", "中信心": "中", "低信心": "低"}

# 只有需要解釋的類型才寫提示；其餘看名字就懂。
_CATEGORY_TIPS = {
    "meta": "作者、字數、發表日期與平台，以及整行的裝飾分隔線。\n"
            "單獨的日期只給「中」信心：日記體小說每章開頭就是日期，不宜預設勾選。",
    "repeat": "整本書重複出現三次以上的短段落。",
}


class AdScanDialog(QDialog):
    candidateHighlighted = Signal(int, int)  # start_line, end_line（0-indexed，含首尾）
    deletionReady = Signal(list)             # 刪除後的整份本文

    def __init__(self, raw_lines: list, parent=None, selected_ranges=None, selected_count: int = 0,
                 enabled_categories=None):
        super().__init__(parent)
        self.setWindowTitle("掃描廣告")
        size_dialog(self, 900, 640)
        self._raw_lines = list(raw_lines)
        self._selected_ranges = list(selected_ranges or [])
        self._selected_count = selected_count
        self._candidates: list[dict] = []
        self._selected: set[int] = set()
        self.result_lines: list | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        title = QLabel("選擇偵測類型")
        title.setObjectName("appTitle")
        root.addWidget(title)

        # 類型照視窗寬度自動換行：寬的時候一列排完，不會在右邊留一大塊空白。
        category_box, category_flow = flow_container(uniform=True)
        self._category_checks = {}
        for key, label in AD_CATEGORY_LABELS.items():
            checkbox = QCheckBox(label)
            checkbox.setChecked(enabled_categories is None or key in enabled_categories)
            checkbox.setToolTip(_CATEGORY_TIPS.get(key, ""))
            checkbox.toggled.connect(self._run_scan)
            self._category_checks[key] = checkbox
            category_flow.addWidget(checkbox)
        root.addWidget(category_box)
        # 「只掃描選取的章節」是範圍，不是偵測類型，用分隔線隔開。
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
        for label in ("高信心", "中信心", "低信心", "全選", "全部取消"):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked, m=label: self._select_mode(m))
            select_row.addWidget(button)
        select_row.addStretch(1)
        root.addLayout(select_row)

        self.status_label = QLabel("尚未掃描")
        self.status_label.setObjectName("fileLabel")
        root.addWidget(self.status_label)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["信心", "類型", "內容預覽"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        setup_columns(self.table, {0: "contents", 1: 200})
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        enable_sorting(self.table)
        root.addWidget(self.table, 1)

        buttons = QDialogButtonBox()
        cancel_button = buttons.addButton("關閉", QDialogButtonBox.ButtonRole.RejectRole)
        self.delete_button = buttons.addButton("刪除已勾選項目", QDialogButtonBox.ButtonRole.AcceptRole)
        self.delete_button.setObjectName("primary")
        cancel_button.clicked.connect(self.reject)
        self.delete_button.clicked.connect(self._delete_selected)
        root.addWidget(buttons)

        self._run_scan()

    def enabled_categories(self) -> set:
        return self._enabled_categories()

    def _set_scope(self, selected_ranges, selected_count: int):
        self._selected_ranges = list(selected_ranges or [])
        self._selected_count = selected_count
        i18n.set_text(self.scope_check,
                      f"只掃描選取的 {selected_count} 個章節" if selected_count else "只掃描選取的章節")
        self.scope_check.setEnabled(bool(self._selected_ranges))
        if not self._selected_ranges:
            self.scope_check.setChecked(False)
        self.scope_check.setToolTip("" if self._selected_ranges
                                    else i18n.T("先在目錄選取章節，再開啟掃描，就可以只掃描那幾章"))

    @staticmethod
    def _candidate_key(candidate):
        return tuple(sorted(candidate["types"])), candidate["preview"]

    def reload(self, raw_lines, selected_ranges=None, selected_count: int = 0):
        """本文改過了：重新掃描。使用者勾過／取消過的項目照內容對回去；
        新出現的項目照預設（高信心勾選）。"""
        state = {self._candidate_key(candidate): index in self._selected
                 for index, candidate in enumerate(self._candidates)}
        self._raw_lines = list(raw_lines)
        self.scope_check.blockSignals(True)
        self._set_scope(selected_ranges, selected_count)
        self.scope_check.blockSignals(False)
        self._run_scan()
        if self._candidates:
            self._selected = {index for index, candidate in enumerate(self._candidates)
                              if state.get(self._candidate_key(candidate), candidate["confidence"] == "高")}
            self._refresh_table()

    def _enabled_categories(self):
        return {key for key, box in self._category_checks.items() if box.isChecked()}

    def _run_scan(self, *_args):
        categories = self._enabled_categories()
        if not categories:
            self._candidates = []
            self._selected = set()
            self._refresh_table()
            i18n.set_text(self.status_label, "尚未選擇偵測類型")
            return
        ranges = self._selected_ranges if self.scope_check.isChecked() else None
        self._candidates = scan_ad_candidates(self._raw_lines, categories, ranges)
        # 高信心項目預設勾選，中／低信心留給使用者自行決定。
        self._selected = {i for i, c in enumerate(self._candidates) if c["confidence"] == "高"}
        self._refresh_table()
        if not self._candidates:
            i18n.set_text(self.status_label, "選取的章節裡沒有符合的廣告候選"
                          if self.scope_check.isChecked() else "未找到符合所選類型的廣告候選")

    def _refresh_table(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._candidates))
        for row, candidate in enumerate(self._candidates):
            confidence_item = make_item(candidate["confidence"], row, CONFIDENCE_ORDER.get(candidate["confidence"], 9))
            confidence_item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            confidence_item.setCheckState(
                Qt.CheckState.Checked if row in self._selected else Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, confidence_item)

            labels = "、".join(AD_CATEGORY_LABELS[key] for key in AD_CATEGORY_LABELS if key in candidate["types"])
            self.table.setItem(row, 1, make_item(i18n.T(labels), row))

            preview = re.sub(r"\s+", " ", candidate["preview"]).strip()
            if len(preview) > 160:
                preview = preview[:157] + "…"
            self.table.setItem(row, 2, make_item(preview, row))
        self.table.blockSignals(False)
        resort(self.table)
        i18n.set_text(self.status_label, f"找到 {len(self._candidates)} 個候選；已勾選 {len(self._selected)} 個")

    def _on_item_changed(self, item: QTableWidgetItem):
        if item.column() != 0:
            return
        row = data_index(self.table, item.row())
        if item.checkState() == Qt.CheckState.Checked:
            self._selected.add(row)
        else:
            self._selected.discard(row)
        i18n.set_text(self.status_label, f"找到 {len(self._candidates)} 個候選；已勾選 {len(self._selected)} 個")

    def _on_selection_changed(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        candidate = self._candidates[data_index(self.table, rows[0].row())]
        self.candidateHighlighted.emit(candidate["start"], candidate["end"])

    def _select_mode(self, button_label: str):
        if button_label == "全選":
            self._selected = set(range(len(self._candidates)))
        elif button_label == "全部取消":
            self._selected = set()
        else:
            target = _CONFIDENCE_BY_BUTTON[button_label]
            self._selected = {i for i, c in enumerate(self._candidates) if c["confidence"] == target}
        self._refresh_table()

    def _delete_selected(self):
        if not self._selected:
            dialogs.info(self, "尚未勾選", "請先勾選要刪除的候選段落。")
            return
        if not dialogs.confirm(
            self, "確認刪除廣告",
            f"確定刪除已勾選的 {len(self._selected)} 個候選段落嗎？\n\n刪除後會重新建立目錄。",
        ):
            return

        ranges = sorted((self._candidates[i]["start"], self._candidates[i]["end"]) for i in self._selected)
        merged = []
        for start, end in ranges:
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))

        lines = list(self._raw_lines)
        for start, end in reversed(merged):
            del lines[start:end + 1]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()

        self.result_lines = lines
        self.deletionReady.emit(lines)
