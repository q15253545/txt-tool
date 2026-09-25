"""左側「章節管理」面板，從目錄卡片標題列的「…」開啟。

原本這些操作（設層級／忽略／加入目錄…）只能靠右鍵選單找到，右鍵選單不會
主動告訴你有哪些功能存在；這裡集中成按鈕文字自解釋的面板，右鍵選單仍然
保留給熟悉操作的人當捷徑。重掃／全部展開／全部摺疊只影響目錄顯示，放在
目錄卡片自己的標題列，不在這裡重複。
"""

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from . import i18n, icons
from .widgets import Divider, IconButton, make_card_header

MISSING_CHECK_MODES = ["僅檢查中間缺口", "每卷從第1章起算", "同作品跨卷接續"]

# 「標註為非章節」「將所選文字加入目錄」針對的是特定一行／一個章節，
# 放在目錄與本文各自的右鍵選單裡，操作對象才清楚；這裡只留整體性的動作。
_ACTIONS = [
    ("plus", "新增章節", "insert_requested"),
    # 「檢查未辨識章節」已經併進「自訂章節規則」的「本文可疑章節」分頁。
    ("sliders-horizontal", "自訂章節規則", "rules_requested"),
    ("list-filter", "章節顯示切換", "toggle_compact_requested"),
]


class ChapterPanel(QWidget):
    closed = Signal()
    insert_requested = Signal()
    rules_requested = Signal()
    toggle_compact_requested = Signal()
    check_missing_requested = Signal()
    allowed_tail_chars_changed = Signal(str)
    missing_mode_changed = Signal()
    # 點檢查結果裡的某一筆：「群組索引|章號|gap 或 dup」
    report_link_activated = Signal(str)
    report_closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._action_buttons: dict[str, QPushButton] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header, header_layout = make_card_header("章節管理")
        self.close_button = IconButton("panel-left-close", "收起章節管理", size=16)
        self.close_button.clicked.connect(self.closed.emit)
        header_layout.addWidget(self.close_button)
        outer.addWidget(header)

        root = QVBoxLayout()
        root.setContentsMargins(16, 12, 16, 14)
        root.setSpacing(10)
        outer.addLayout(root, 1)

        for icon_name, text, signal_name in _ACTIONS:
            button = QPushButton(text)
            button.setIcon(icons.make_icon(icon_name, "#243044", 16))
            signal = getattr(self, signal_name)
            button.clicked.connect(signal.emit)
            self._action_buttons[signal_name] = button
            root.addWidget(button)

        root.addWidget(Divider())

        tail_label = QLabel("標題結尾例外字元")
        tail_label.setObjectName("fileLabel")
        root.addWidget(tail_label)
        self.allowed_tail_chars_input = QLineEdit()
        self.allowed_tail_chars_input.setPlaceholderText("例如：，。")
        self.allowed_tail_chars_input.editingFinished.connect(
            lambda: self.allowed_tail_chars_changed.emit(self.allowed_tail_chars_input.text()))
        root.addWidget(self.allowed_tail_chars_input)

        root.addWidget(Divider())

        # 缺章檢查：範圍設定跟執行按鈕放在同一組，先選範圍再按檢查。
        mode_label = QLabel("缺章檢查範圍")
        mode_label.setObjectName("fileLabel")
        root.addWidget(mode_label)
        self.missing_mode_combo = QComboBox()
        self.missing_mode_combo.addItems(MISSING_CHECK_MODES)
        self.missing_mode_combo.currentIndexChanged.connect(lambda _index: self.missing_mode_changed.emit())
        root.addWidget(self.missing_mode_combo)
        self.check_missing_button = QPushButton("檢查缺章")
        self.check_missing_button.setIcon(icons.make_icon("list-checks", "#243044", 16))
        self.check_missing_button.clicked.connect(self.check_missing_requested.emit)
        root.addWidget(self.check_missing_button)

        root.addWidget(self._build_report_pane(), 1)
        # 結果區隱藏時由這段空白把按鈕往上推；顯示時改讓結果區吃掉剩餘高度。
        root.addStretch(1)
        self._root_layout = root
        self._tail_stretch_index = root.count() - 1
        self._report = None
        self._tokens = None

    def _build_report_pane(self) -> QWidget:
        """檢查缺章的結果：固定顯示在按鈕下方，不再用狀態列（狀態列會被下
        一個操作的訊息蓋掉）。之後每次目錄重建都自動重算，一邊合併、修改
        章節，一邊就能看到問題清單縮短，不用反覆按檢查。"""
        self.report_pane = QFrame()
        self.report_pane.setObjectName("reportPane")
        pane_layout = QVBoxLayout(self.report_pane)
        pane_layout.setContentsMargins(12, 8, 6, 10)
        pane_layout.setSpacing(4)

        title_row = QHBoxLayout()
        title_row.setSpacing(4)
        title = QLabel("檢查結果")
        title.setObjectName("reportTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.report_close_button = IconButton("x", "關閉檢查結果（不再自動重算）", size=14)
        self.report_close_button.clicked.connect(self._close_report)
        title_row.addWidget(self.report_close_button)
        pane_layout.addLayout(title_row)

        scroll = QScrollArea()
        scroll.setObjectName("panelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.report_label = QLabel()
        self.report_label.setObjectName("reportBody")
        self.report_label.setWordWrap(True)
        self.report_label.setTextFormat(Qt.TextFormat.RichText)
        self.report_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.report_label.linkActivated.connect(self.report_link_activated.emit)
        # 內容含卷名（書的內容），自己組字串時再決定哪些要轉簡體。
        i18n.skip(self.report_label)
        scroll.setWidget(self.report_label)
        pane_layout.addWidget(scroll, 1)
        self.report_pane.hide()
        return self.report_pane

    def _set_report_visible(self, visible: bool):
        self.report_pane.setVisible(visible)
        self._root_layout.setStretch(self._tail_stretch_index, 0 if visible else 1)

    def _close_report(self):
        self._set_report_visible(False)
        self._report = None
        self.report_closed.emit()

    def missing_mode(self) -> str:
        return i18n.combo_value(self.missing_mode_combo)

    def is_report_visible(self) -> bool:
        return self._report is not None

    def show_missing_report(self, report: dict):
        self._report = report
        self._render_report()
        self._set_report_visible(True)

    def clear_report(self):
        self._set_report_visible(False)
        self._report = None

    def set_report_theme(self, tokens):
        self._tokens = tokens
        self.report_close_button.set_colors(tokens.icon, tokens.icon_hover, tokens.text_faint)
        if self._report is not None:
            self._render_report()

    def refresh_language(self):
        if self._report is not None:
            self._render_report()

    def _render_report(self):
        """由上到下分段：總結 → 各卷的缺口／重複（可點，跳到附近章節）→ 備註。"""
        T = i18n.T
        report = self._report
        tokens = self._tokens
        warn = tokens.warn_text if tokens else "#B4383C"
        ok = tokens.ok_text if tokens else "#1F7A4C"
        muted = tokens.text_muted if tokens else "#647084"
        accent = tokens.accent if tokens else "#3869D8"
        parts = []
        total, mode = report["total"], T(report["mode"])
        if total == 0:
            parts.append(f'<p style="color:{muted}">{T("目前目錄沒有可連號檢查的正式章節。")}</p>')
        else:
            problem_count = sum(len(group["missing"]) + len(group["duplicates"]) for group in report["groups"])
            if problem_count:
                headline = f'<b style="color:{warn}">{T(f"發現 {problem_count} 處問題")}</b>'
            else:
                headline = f'<b style="color:{ok}">✓ {T("未發現缺章")}</b>'
            parts.append(f'<p style="margin:0">{headline}<br>'
                         f'<span style="color:{muted}">{T(f"共 {total} 個正式章節")} · {mode}</span></p>')
            clean_groups = 0
            for index, group in enumerate(report["groups"]):
                if not group["missing"] and not group["duplicates"]:
                    clean_groups += 1
                    continue
                label = html.escape(group["label"]) if group["label"] != "全書" else T("全書")
                lines = []
                for start, end in group["missing"]:
                    text = T(f"缺第 {start} 章") if start == end else T(f"缺第 {start}–{end} 章")
                    lines.append(f'<a href="{index}|{start}|gap" style="color:{accent};text-decoration:none">{text}</a>')
                for number in group["duplicates"]:
                    text = T(f"第 {number} 章重複")
                    lines.append(f'<a href="{index}|{number}|dup" style="color:{accent};text-decoration:none">{text}</a>')
                parts.append(f'<p style="margin-top:8px;margin-bottom:0"><b>{label}</b><br>'
                             + "<br>".join(f"· {line}" for line in lines) + "</p>")
            if clean_groups and clean_groups < len(report["groups"]):
                parts.append(f'<p style="margin-top:8px;margin-bottom:0;color:{muted}">'
                             f'{T(f"其餘 {clean_groups} 組章節編號連續。")}</p>')
        notes = [T("番外與小數章不列入檢查。")]
        if report.get("start_unverified"):
            notes.append(T("起始章以前是否缺章未判定（可改用「每卷從第1章起算」）。"))
        notes.append(T("修改或重新整理目錄後會自動重算。"))
        parts.append(f'<p style="margin-top:8px;color:{muted}">' + "<br>".join(notes) + "</p>")
        self.report_label.setText("".join(parts))

    def set_icon_colors(self, color: str):
        for icon_name, _text, signal_name in _ACTIONS:
            self._action_buttons[signal_name].setIcon(icons.make_icon(icon_name, color, 16))
        self.check_missing_button.setIcon(icons.make_icon("list-checks", color, 16))
        self.close_button.set_colors(color, color, color)

    def set_allowed_tail_chars(self, value: str):
        self.allowed_tail_chars_input.setText(value)
