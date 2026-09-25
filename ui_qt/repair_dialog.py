"""整理選定章節的硬換行／異常空白：逐行分組預覽，可勾選要套用哪些。

只處理呼叫端已經算好的 RepairPreview（見 toc_ops.preview_selected_repairs），
不重新辨識章節結構，也不碰未選取的部分。
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from core.reflow import RepairPreview
from . import dialogs, i18n
from .sortable_table import setup_columns
from .widgets import size_dialog


def _visible(text: str) -> str:
    """只把『被編輯到的空白』轉成可見符號，段首縮排維持原樣，
    避免整列被 ▢·○ 這類符號淹沒、反而看不出重點。"""
    return (text.replace("\n", "⏎").replace("\t", "→")
                .replace("　", "▢").replace(" ", "·"))


def _line_bounds(source: str, position: int):
    start = source.rfind("\n", 0, position) + 1
    end = source.find("\n", position)
    return start, (len(source) if end < 0 else end)


def _render_group(source: str, key: int, edits: list):
    """回傳整行的（原本, 改為）對照，套用該行全部編輯後的最終結果。"""
    line_start = key
    line_end = _line_bounds(source, edits[-1].end)[1]
    before_parts, after_parts = [], []
    position = line_start
    for edit in edits:
        plain = source[position:edit.start]
        before_parts.append(plain)
        after_parts.append(plain)
        before_parts.append(_visible(source[edit.start:edit.end]))
        after_parts.append(_visible(edit.after))
        position = edit.end
    tail = source[position:line_end]
    before_parts.append(tail)
    after_parts.append(tail)
    before = "".join(before_parts).replace("\n", "⏎")
    after = "".join(after_parts).replace("\n", "⏎")
    return before, after


class RepairDialog(QDialog):
    """建構時吃一份 RepairPreview；接受後結果放在 self.result_preview。"""

    def __init__(self, preview: RepairPreview, parent=None):
        super().__init__(parent)
        self.setWindowTitle("整理換行與空白")
        size_dialog(self, 980, 660)
        self.source = preview.source
        self.result_preview: RepairPreview | None = None

        self._groups: dict[int, list] = {}
        for edit in preview.edits:
            key = _line_bounds(self.source, edit.start)[0]
            self._groups.setdefault(key, []).append(edit)
        self._group_keys = sorted(self._groups)
        self._visible_keys: list[int] = []
        self._selected: set[int] = set()

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        title = QLabel("整理類型")
        title.setObjectName("appTitle")
        root.addWidget(title)

        kind_row = QHBoxLayout()
        kind_row.setSpacing(16)
        self.linebreak_check = QCheckBox("合併硬換行")
        self.linebreak_check.setChecked(True)
        # 空白整理會移除中文字之間的空白，對「姓名 地址 電話」這類刻意
        # 用空白對齊的行是破壞性的，預設關閉，交由使用者看過預覽再決定。
        self.space_check = QCheckBox("清理異常空白")
        self.space_check.setChecked(False)
        self.linebreak_check.toggled.connect(self._on_kind_changed)
        self.space_check.toggled.connect(self._on_kind_changed)
        kind_row.addWidget(self.linebreak_check)
        kind_row.addWidget(self.space_check)
        kind_row.addStretch(1)
        root.addLayout(kind_row)

        self.status_label = QLabel()
        self.status_label.setObjectName("fileLabel")
        root.addWidget(self.status_label)

        preview_title = QLabel("逐項預覽")
        preview_title.setObjectName("appTitle")
        root.addWidget(preview_title)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["類型", "原本", "改為"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        setup_columns(self.table, {0: "contents", 1: 0.45})
        self.table.itemChanged.connect(self._on_item_changed)
        root.addWidget(self.table, 1)

        select_row = QHBoxLayout()
        select_row.setSpacing(8)
        for text, slot in (("全選", self._select_all), ("全部取消", self._select_none)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            select_row.addWidget(button)
        select_row.addStretch(1)
        root.addLayout(select_row)

        buttons = QDialogButtonBox()
        cancel_button = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        apply_button = buttons.addButton("套用勾選項目", QDialogButtonBox.ButtonRole.AcceptRole)
        apply_button.setObjectName("primary")
        cancel_button.clicked.connect(self.reject)
        apply_button.clicked.connect(self._apply)
        root.addWidget(buttons)

        self._on_kind_changed()

    def _active_kinds(self):
        kinds = set()
        if self.linebreak_check.isChecked():
            kinds.add("linebreak")
        if self.space_check.isChecked():
            kinds.add("space")
        return kinds

    def _on_kind_changed(self, *_args):
        # 切換類型時，新出現的項目預設全部勾起來，符合「看到就是要改」的直覺。
        # 先純算出可見項目再刷新一次；原本是「刷新→設定勾選→再刷新」，第一次
        # 建好的整張表馬上被第二次蓋掉（審查報告 C-25）。
        kinds = self._active_kinds()
        self._selected = {key for key in self._group_keys
                          if any(edit.kind in kinds for edit in self._groups[key])}
        self._refresh()

    def _refresh(self):
        kinds = self._active_kinds()
        self._visible_keys = [key for key in self._group_keys
                              if any(edit.kind in kinds for edit in self._groups[key])]
        self._selected &= set(self._visible_keys)

        self.table.blockSignals(True)
        self.table.setRowCount(len(self._visible_keys))
        total_edits = 0
        for row, key in enumerate(self._visible_keys):
            edits = [edit for edit in self._groups[key] if edit.kind in kinds]
            total_edits += len(edits)
            before, after = _render_group(self.source, key, edits)
            group_kinds = {edit.kind for edit in edits}
            label_map = {"linebreak": "硬換行", "space": "空白"}
            label = label_map.get(next(iter(group_kinds)), "換行+空白") if len(group_kinds) == 1 else "換行+空白"
            if len(edits) > 1:
                label = f"{label}×{len(edits)}"

            kind_item = QTableWidgetItem(i18n.T(label))
            kind_item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            kind_item.setCheckState(Qt.CheckState.Checked if key in self._selected else Qt.CheckState.Unchecked)
            kind_item.setData(Qt.ItemDataRole.UserRole, key)
            self.table.setItem(row, 0, kind_item)
            self.table.setItem(row, 1, QTableWidgetItem(before))
            self.table.setItem(row, 2, QTableWidgetItem(after))
        self.table.blockSignals(False)
        self._update_status(kinds)

    def _update_status(self, kinds):
        selected_edits = sum(
            len([edit for edit in self._groups[key] if edit.kind in kinds]) for key in self._selected)
        i18n.set_text(self.status_label, 
            f"符合條件 {len(self._visible_keys)} 行（共 "
            f"{sum(len([e for e in self._groups[k] if e.kind in kinds]) for k in self._visible_keys)} 處）；"
            f"已勾選 {len(self._selected)} 行（共 {selected_edits} 處）"
        )

    def _on_item_changed(self, item: QTableWidgetItem):
        if item.column() != 0:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self._selected.add(key)
        else:
            self._selected.discard(key)
        self._update_status(self._active_kinds())

    def _select_all(self):
        self._selected = set(self._visible_keys)
        self._refresh()

    def _select_none(self):
        self._selected.clear()
        self._refresh()

    def _apply(self):
        if not self._selected:
            dialogs.info(self, "整理換行與空白", "請先勾選要套用的項目。")
            return
        kinds = self._active_kinds()
        chosen_edits = [edit for key in sorted(self._selected)
                        for edit in self._groups[key] if edit.kind in kinds]
        self.result_preview = RepairPreview(
            source=self.source,
            edits=tuple(sorted(chosen_edits, key=lambda edit: edit.start)),
        )
        self.accept()
