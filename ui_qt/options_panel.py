"""格式選項面板（工具列「排版設定」）：排版勾選項目＋內容清理。

勾選狀態即時存在面板自己身上；呼叫端只在使用者按下「套用格式」時，
用 current_options() 讀一次目前的勾選結果。掃描廣告也放在這裡，排版前
順手清理，不必另外切一個面板（找出沒被辨識到的章節跟章節辨識有關，放在
章節管理的「自訂章節規則」裡）。
"""

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QGridLayout, QLabel, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from core.format_options import FormatOptions
from . import i18n, icons
from .widgets import Divider, IconButton, make_card_header

_CLEANUP_ACTIONS = [
    ("scan-search", "掃描廣告", "ad_scan_requested"),
    ("quote", "引號與標點檢查", "quote_check_requested"),
    ("text-select", "繁簡轉換", "script_convert_requested"),
]

_CHECKBOX_FIELDS = [
    ("remove_extra_empty", "刪除所有空行"),
    ("add_empty", "章節插入空行"),
    ("add_paragraph_empty", "段落插入空行"),
    ("remove_extra_spaces", "刪除多餘空格"),
    ("auto_indent", "增加縮排"),
    ("remove_indent", "去除縮排"),
    ("merge_title", "合併下行標題"),
    ("format_title", "標題前後空行"),
    ("format_dialogue", "格式化對話框引號"),
]

# 只有規則不只字面意思的項目才寫提示。
_CHECKBOX_TIPS = {
    "remove_extra_spaces": (
        "行尾空白全部刪掉；段落中間兩個中文字之間的空白直接刪掉\n"
        "（「他 說 了 一句 話。」→「他說了一句話。」），\n"
        "中英數之間的空格會保留（「我用 Notepad 排版 的 檔案。」→\n"
        "「我用 Notepad 排版的檔案。」）。\n"
        "段首縮排與章節標題不受影響；標題間隔請用「編號與標題間隔」。"),
}

NUM_STYLE_CHOICES = ["保留原文", "中文數字", "阿拉伯數字"]
SEP_STYLE_CHOICES = ["保留原文", "半形空格", "全形空格", "冒號"]
PUNCT_CHOICES = ["不轉換", "轉全形", "轉半形"]
DIGIT_CHOICES = ["不轉換", "轉全形", "轉半形"]


def describe_options(options: FormatOptions) -> list:
    """目前會生效的排版項目，用來寫在確認視窗裡。

    使用者按「排版選取章節」時看不到格式選項面板（它在另一張卡片上），
    所以要把即將套用的項目列出來，不能只說「要套用格式嗎」。"""
    items = [label for field, label in _CHECKBOX_FIELDS if getattr(options, field)]
    if options.num_style != "保留原文":
        items.append(f"章節編號：{options.num_style}")
    if options.sep_style != "保留原文":
        items.append(f"編號與標題間隔：{options.sep_style}")
    if options.normalize_punct:
        items.append("標點符號：轉全形")
    elif options.halfwidth_punct:
        items.append("標點符號：轉半形")
    if options.fullwidth_digits:
        items.append("數字：轉全形")
    elif options.halfwidth_digits:
        items.append("數字：轉半形")
    return items


class _PanelScroll(QScrollArea):
    """捲動區預設不會把內容的最小寬度往上回報，卡片就能被拉得比內容還窄，
    下拉框、按鈕直接超出卡片邊界。這裡把「內容最小寬度＋捲軸寬度」當成
    自己的最小寬度，卡片最窄就只到剛好裝得下內容。"""

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        content = self.widget()
        if content is None:
            return hint
        width = (content.minimumSizeHint().width() + self.verticalScrollBar().sizeHint().width()
                 + 2 * self.frameWidth())
        return QSize(max(hint.width(), width), hint.height())


class OptionsPanel(QWidget):
    apply_requested = Signal()
    closed = Signal()
    ad_scan_requested = Signal()
    quote_check_requested = Signal()
    script_convert_requested = Signal()
    whitespace_toggled = Signal(bool)

    def __init__(self, initial: FormatOptions, parent=None):
        super().__init__(parent)
        self._checkboxes: dict[str, QCheckBox] = {}
        self._cleanup_buttons: dict[str, QPushButton] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header, header_layout = make_card_header("格式選項")
        # 顯示空格：只影響本文的顯示，方便檢查縮排、行尾多餘空白這類排版問題。
        self.whitespace_button = IconButton("space", "顯示空格（半形 ·、全形 □、Tab →；行尾多餘空白標紅）", size=16)
        self.whitespace_button.setCheckable(True)
        self.whitespace_button.toggled.connect(self.whitespace_toggled.emit)
        header_layout.addWidget(self.whitespace_button)
        self.close_button = IconButton("panel-left-close", "收起格式選項", size=16)
        self.close_button.clicked.connect(self.closed.emit)
        header_layout.addWidget(self.close_button)
        root.addWidget(header)

        # 選項加上內容清理之後比較長，視窗矮時要能捲動；「套用格式」固定在
        # 捲動區外面的底部，不會被捲走。
        scroll = _PanelScroll()
        scroll.setObjectName("panelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("panelScrollContent")
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        body = QVBoxLayout(content)
        body.setContentsMargins(16, 12, 16, 12)
        body.setSpacing(12)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)
        for index, (field, label) in enumerate(_CHECKBOX_FIELDS):
            checkbox = QCheckBox(label)
            checkbox.setToolTip(_CHECKBOX_TIPS.get(field, ""))
            checkbox.setChecked(getattr(initial, field))
            self._checkboxes[field] = checkbox
            grid.addWidget(checkbox, index, 0)
        body.addLayout(grid)

        combos = QGridLayout()
        combos.setHorizontalSpacing(10)
        combos.setVerticalSpacing(8)
        combos.setColumnStretch(1, 1)
        body.addLayout(combos)

        punct_default = "轉全形" if initial.normalize_punct else "轉半形" if initial.halfwidth_punct else "不轉換"
        digit_default = "轉全形" if initial.fullwidth_digits else "轉半形" if initial.halfwidth_digits else "不轉換"
        self.num_style_combo = self._add_combo(combos, 0, "章節編號", NUM_STYLE_CHOICES, initial.num_style)
        self.sep_style_combo = self._add_combo(combos, 1, "編號與標題間隔", SEP_STYLE_CHOICES, initial.sep_style)
        self.punct_combo = self._add_combo(combos, 2, "標點符號", PUNCT_CHOICES, punct_default)
        self.digit_combo = self._add_combo(combos, 3, "數字", DIGIT_CHOICES, digit_default)

        body.addWidget(Divider())
        cleanup_label = QLabel("內容清理")
        cleanup_label.setObjectName("fileLabel")
        body.addWidget(cleanup_label)
        for icon_name, text, signal_name in _CLEANUP_ACTIONS:
            button = QPushButton(text)
            button.setIcon(icons.make_icon(icon_name, "#243044", 16))
            button.clicked.connect(getattr(self, signal_name).emit)
            self._cleanup_buttons[icon_name] = button
            body.addWidget(button)

        body.addStretch(1)

        footer = QVBoxLayout()
        footer.setContentsMargins(16, 8, 16, 14)
        self.apply_button = QPushButton("套用格式")
        self.apply_button.setObjectName("primary")
        self.apply_button.setIcon(icons.make_icon("check", "#FFFFFF", 16))
        self.apply_button.clicked.connect(self.apply_requested.emit)
        footer.addWidget(self.apply_button)
        root.addLayout(footer)

    @staticmethod
    def _add_combo(grid: QGridLayout, row: int, label_text, choices, current_value) -> QComboBox:
        """標籤放左、下拉放右——欄位比較窄時，這樣比標籤壓在上面省一半高度。"""
        label = QLabel(label_text)
        label.setObjectName("fileLabel")
        combo = QComboBox()
        combo.addItems(choices)
        if current_value in choices:
            i18n.set_combo_value(combo, current_value)
        grid.addWidget(label, row, 0)
        grid.addWidget(combo, row, 1)
        return combo

    def current_options(self, structure_mode: str) -> FormatOptions:
        punct = i18n.combo_value(self.punct_combo)
        digit = i18n.combo_value(self.digit_combo)
        values = {field: checkbox.isChecked() for field, checkbox in self._checkboxes.items()}
        return FormatOptions(
            **values,
            normalize_punct=(punct == "轉全形"),
            halfwidth_punct=(punct == "轉半形"),
            fullwidth_digits=(digit == "轉全形"),
            halfwidth_digits=(digit == "轉半形"),
            num_style=i18n.combo_value(self.num_style_combo),
            sep_style=i18n.combo_value(self.sep_style_combo),
            structure=structure_mode,
        )

    def set_icon_colors(self, color: str, accent_text: str, accent: str = ""):
        self.close_button.set_colors(color, color, color)
        self.whitespace_button.set_colors(color, accent or color, color, accent or color)
        for icon_name, button in self._cleanup_buttons.items():
            button.setIcon(icons.make_icon(icon_name, color, 16))
        self.apply_button.setIcon(icons.make_icon("check", accent_text, 16))

    def set_cleanup_enabled(self, enabled: bool):
        for button in self._cleanup_buttons.values():
            button.setEnabled(enabled)

    def options_state(self) -> dict:
        """目前的勾選與下拉，存成可以寫進 JSON 的樣子（下次開程式時還原）。"""
        return {
            "checks": {field: checkbox.isChecked() for field, checkbox in self._checkboxes.items()},
            "num_style": i18n.combo_value(self.num_style_combo),
            "sep_style": i18n.combo_value(self.sep_style_combo),
            "punct": i18n.combo_value(self.punct_combo),
            "digit": i18n.combo_value(self.digit_combo),
        }

    def restore_options_state(self, state: dict):
        """還原 options_state() 存下來的值；認不得的欄位直接略過。"""
        checks = state.get("checks") if isinstance(state.get("checks"), dict) else {}
        for field, checked in checks.items():
            if field in self._checkboxes:
                self._checkboxes[field].setChecked(bool(checked))
        for key, combo, choices in (("num_style", self.num_style_combo, NUM_STYLE_CHOICES),
                                    ("sep_style", self.sep_style_combo, SEP_STYLE_CHOICES),
                                    ("punct", self.punct_combo, PUNCT_CHOICES),
                                    ("digit", self.digit_combo, DIGIT_CHOICES)):
            if state.get(key) in choices:
                i18n.set_combo_value(combo, state[key])

    def reset_to_defaults(self):
        """一鍵排版用自己的固定組合、不管面板目前勾了什麼；套用後把面板歸零，
        避免看起來像「這些勾選也是一鍵排版套用的」而造成誤解。"""
        for checkbox in self._checkboxes.values():
            checkbox.setChecked(False)
        for combo in (self.num_style_combo, self.sep_style_combo, self.punct_combo, self.digit_combo):
            combo.setCurrentIndex(0)
