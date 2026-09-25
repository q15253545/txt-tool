"""PySide6 主視窗：開檔／編輯／存檔／目錄樹／格式選項／搜尋取代／復原重做。

章節辨識與排版邏輯完全交給 core.structure_builder，這裡只負責畫面與把使用者
的操作轉成呼叫 core 純函式的參數。

復原／重做刻意不用 QPlainTextEdit 內建的 QTextDocument undo：忽略集合、
強制卷／章層級、自動標題快取這些「章節結構」狀態跟正文是綁在一起的，
只復原文字、不復原這些狀態，會讓目錄跟正文對不起來（點右鍵選單的操作
之後按 Ctrl+Z，文字復原了但目錄還停在操作後的樣子）。所以改成
整份文字＋結構狀態一起存成快照（見 _checkpoint_document／
_restore_document_step），輸入文字時用計時器合併成一步，不是每個按鍵
存一份。
"""

import bisect
import difflib
import os
import re

from PySide6.QtCore import QByteArray, QEvent, QObject, QPoint, QRect, QRegularExpression, QTimer, Qt, QUrl
from PySide6.QtGui import (
    QAction, QColor, QDesktopServices, QFont, QGuiApplication, QKeySequence, QShortcut,
    QTextBlockFormat, QTextCharFormat, QTextCursor, QTextFormat,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QSplitter, QTableWidget, QTextEdit, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.cn_numerals import chinese_to_arabic
from core.chapter_parse import (
    CN_NUM_FLOAT_PATTERN, build_invalid_tail_regex, chapter_unit_signature, compact_number_ranges,
    compact_toc_label, extract_author_from_intro, locate_chapter_number, looks_like_auto_chapter,
    render_chapter_number_like,
)
from core.ad_scan import scan_ad_candidates
from core.collection import chapter_gap_report, group_formal_chapters
from core.encoding import detect_line_ending, smart_detect_encoding, strip_stray_bom
from core.file_io import read_text, read_text_lossy, write_text_atomic
from core.filename_meta import build_smart_filename, extract_filename_metadata
from core.format_options import FormatOptions
from core.script_convert import (
    SCRIPT_SIMP, SCRIPT_TRAD, convert_body_text, convert_script, opencc_available,
)
from core.structure_builder import BuildContext, build_document_structure
from core.insert_suggestions import get_insert_suggestions
from core.persistence import (
    load_ui_state, load_user_chapter_rules, load_window_state, save_ui_state, save_window_state,
)
from core.title_markers import strip_export_markers, strip_persistent_title_marker

from . import app_log, dialogs, i18n, icons, toc_ops
from .app_log import action, log, timed
from .ad_scan_dialog import AdScanDialog
from .chapter_panel import ChapterPanel
from .find_bar import FindBar
from .insert_title_dialog import InsertTitleDialog
from .metadata_bar import ENCODING_CODECS, MetadataBar
from .options_panel import OptionsPanel, describe_options
from .quote_check_dialog import QuoteCheckDialog
from .script_convert_dialog import ScriptConvertDialog
from .repair_dialog import RepairDialog
from .rules_dialog import RulesDialog
from .text_positions import PositionMap
from .theme import DARK, LIGHT, build_stylesheet, set_active_tokens
from .widgets import (
    AppWidgetPolisher, Card, ClickableLabel, Editor, IconButton, IconTextButton, LanguageToggle,
    ElidedLabel,
    make_card_header,
)

MAX_HISTORY_STEPS = 30
# 復原歷史每步都保存一份完整正文，必須設上限才不會把記憶體吃光。
MAX_HISTORY_CHARS = 30_000_000
# 但無論文件多大，至少保留這麼多步，否則「復原」會形同失效。
MIN_HISTORY_STEPS = 3
# 輸入時多久沒有新的按鍵才視為一次「停頓」、存成一個復原步驟；
# 不是每個按鍵都存一份，那樣復原歷史會被打字過程灌爆。
TYPING_CHECKPOINT_DELAY_MS = 450

DEFAULT_STRUCTURE_MODE = "自動判斷"

# 本文字級縮放：基準跟 theme.py 樣式表 * 規則的 font-size 一致。
EDITOR_BASE_FONT_PX = 14
EDITOR_ZOOM_MIN = 50
EDITOR_ZOOM_MAX = 300

# 本文裡最多同時畫幾個搜尋反白；超過就只畫目前這一筆。
MAX_HIGHLIGHT_SPANS = 800

# 四種行尾標記的意義。說明框要列給使用者看，所以文字放在這裡集中管理，
# 不要散在各個提示字串裡（core/title_markers.py 是判讀它們的地方）。
MARKER_GUIDE = [
    ("[::]", "手動加入目錄", "指定該行為章節標題"),
    ("[::X]", "排除於目錄", "保留正文，不列入目錄"),
    ("[::W]", "作品標題", "多作品合集中各作品的標題"),
    ("[::T]", "特殊標題", "序章、後記等無編號的標題"),
]

# 視窗預設大小的上限；實際大小還會被螢幕可用區域夾住（見 _fit_to_screen）。
DEFAULT_WINDOW_SIZE = (1360, 860)
# 工具列縮成「只有圖示」的門檻。兩個數字不一樣是為了留遲滯：在邊界附近
# 拖動視窗時才不會一直來回切換。
COMPACT_TOOLBAR_WIDTH = 1150
FULL_TOOLBAR_WIDTH = 1210


# 行尾持久標記（連同前面的空白），畫面上要隱藏；規則與 core.title_markers 一致。
_MARKER_REGEX = QRegularExpression(r"\s*\[::[XxWwTt]?\]\s*$")

# 「第十二章」「第3.5回」「第二卷」這類編號開頭。
_HEADING_NUMBER_REGEX = re.compile(r"^第\s*" + CN_NUM_FLOAT_PATTERN + r"\s*[章回節节折幕卷集篇部]")


def _diff_line_mapper(opcodes):
    """由 difflib 的比對結果產生「舊行號 → 新行號」的換算函式。

    沒變的行照位移換算；被刪掉或整段改寫的行，對應到那段改動之前的最後
    一行——目錄選取回復時就會自然落在前一個章節。"""
    def map_line(index: int) -> int:
        for tag, a, b, c, d in opcodes:
            if a <= index < b:
                if tag == "equal" or (tag == "replace" and b - a == d - c):
                    return c + (index - a)
                return c - 1
        return index
    return map_line


def _chapter_line_mapper(old_to_new: dict):
    """排版後整份文字重排，只能靠「每個章節標題從哪一行搬到哪一行」換算；
    其他行對應到它前面最近的章節標題。"""
    keys = sorted(old_to_new)

    def map_line(index: int) -> int:
        if index in old_to_new:
            return old_to_new[index]
        position = bisect.bisect_right(keys, index) - 1
        return old_to_new[keys[position]] if position >= 0 else index
    return map_line


def short_toc_label(full_label: str) -> str:
    """目錄「簡稱」模式的顯示文字：只留章號，例如「第7章 收获的季节」→「第7章」。

    core.compact_toc_label 只會拿掉章號前面重複的書名／卷名；一般單本小說
    的目錄本來就沒有那段前綴，切換後什麼都不會變。所以簡稱模式再進一步
    只保留章號——一眼看出編號是否連續、有沒有重複的章節。抓不到章號的
    標題（序章、番外、後記…）維持原樣。
    """
    compact = compact_toc_label(full_label)
    match = _HEADING_NUMBER_REGEX.match(compact)
    return re.sub(r"\s+", "", match.group(0)) if match else compact


class _ToolDialogWatcher(QObject):
    """工具對話框被切回來（重新成為作用中視窗）時，請主視窗檢查本文有沒有
    改過；改過就讓對話框用新的本文重算。"""

    def __init__(self, window):
        super().__init__(window)
        self._window = window

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.WindowActivate:
            self._window._refresh_tool_dialog(watched)
        return False


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TXT 排版工具")
        self.setWindowIcon(icons.make_app_icon())
        # 視窗大小依螢幕決定，不寫死：直立螢幕在 200% 縮放下，
        # 程式看到的可用寬度只有 720，硬塞 1360 會有一半在畫面外。
        self._toolbar_compact = False
        self._restore_window_geometry()

        self.input_file = ""
        self.detected_encoding = "utf-8"
        self.raw_lines: list[str] = []
        self.user_chapter_rules: list = load_user_chapter_rules()
        self.auto_titles: dict = {}
        self.force_lv1_chapters: set = set()
        self.force_lv2_chapters: set = set()
        self.ignored_chapters: set = set()
        self.structure_mode = DEFAULT_STRUCTURE_MODE
        self.allowed_tail_chars = ""
        self.format_options = FormatOptions(structure=DEFAULT_STRUCTURE_MODE)

        # 目錄項目（QTreeWidgetItem 物件本身當 key）→ 原始行號（0 起算）／
        # 顯示行號（1 起算）。目錄每次重建都是新的物件，舊的 key 就作廢。
        self.chapter_raw_map: dict = {}
        self.chapter_index_map: dict = {}
        self.chapter_records: dict = {}
        self.toc_boundary_map: dict = {}
        # 開著的工具對話框（引號檢查、掃描廣告、自訂章節規則）：非模式，
        # 開著時也能編輯本文。同一種只開一個。
        self._tool_dialogs: dict = {}
        self._tool_dialog_watcher = _ToolDialogWatcher(self)

        # 目錄顯示切換：完整標題／簡稱各自快取一份，切換時不必重新計算。
        self.toc_compact_mode = False
        self.toc_full_labels: dict = {}
        self.toc_compact_labels: dict = {}
        self._breadcrumb_rows: list = []
        self._breadcrumb_items: list = []
        self._breadcrumb_current = None
        # 上次畫目錄之後，正文行號經過的每一次位移（換算函式，依序套用）；
        # 重畫目錄時用來把原本選取的章節對到新位置。
        self._pending_line_maps: list = []

        # 復原／重做：整份文字＋結構狀態的快照陣列，見模組開頭說明。
        self._history: list[tuple] = []
        self._history_position = -1
        self._restoring_history = False
        self._typing_checkpoint_timer = QTimer(self)
        self._typing_checkpoint_timer.setSingleShot(True)
        self._typing_checkpoint_timer.timeout.connect(self._checkpoint_document)
        # 連續轉滑鼠滾輪縮放時，標題格式只在停下來之後重套一次。
        self._format_refresh_timer = QTimer(self)
        self._format_refresh_timer.setSingleShot(True)
        self._format_refresh_timer.setInterval(120)
        self._format_refresh_timer.timeout.connect(self._refresh_title_formats)

        # 首次開啟固定用淺色模式，不跟系統設定走：使用者切換過的深色模式
        # 不會被記住（沒有持久化這項偏好），每次重開都是同一個起點比較好
        # 預期，之後真的要記住上次選擇再另外加。
        self.dark_mode = False
        self._icon_buttons: list[IconButton] = []
        self._panel_toggle_buttons: list[IconTextButton] = []
        self._primary_buttons: list[IconTextButton] = []
        self._editor_zoom = 100
        # 標題加粗／隱藏標記只改字元格式，Qt 一樣會發 textChanged；這段期間
        # 不能當成「使用者在打字」，否則「上一步」按鈕會亮起來。
        self._applying_formats = False
        # 章節標記平常藏起來，按本文卡片上的切換鈕才顯示（只影響顯示）。
        self._show_title_markers = False
        # 匯出時要不要移除標記：只看章節標記說明裡的勾選，跟「顯示章節標記」無關。
        self._strip_markers_on_export = True
        # 缺章檢查結果：按過一次「檢查缺章」之後，每次目錄重建都自動重算。
        self._missing_report_active = False
        self._missing_groups: list = []
        # 按「重新整理目錄」時，最新卷／最新章無條件重新填入。
        self._force_last_found = False
        # 推定卷（本文沒有卷標題、由卷結尾行推得的卷）：目錄項目 → 資訊
        self.virtual_volume_items: dict = {}
        # 已剪下、等著貼上的章節（照檔案總管的做法：按貼上才真的搬動）。
        self._cut_state: dict | None = None
        # 正文的版本號：每次正文真的變動就加一（純顯示的格式變更不算）。
        # 目錄、搜尋結果、raw_lines 各自記下自己是在哪一版算出來的，過期的
        # 位置就不能再拿去改文字——審查報告 C-03、C-05、C-11 都是這個問題。
        self._text_version = 0
        self._toc_text_version = -1
        self._synced_text_version = -1
        self._synced_text = ""
        self._position_map = PositionMap("")
        self._position_map_version = -1
        self._ad_scan_cache: list = []
        self._ad_scan_version = -1
        # 正文改過、還沒匯出：換檔案或關視窗前要先問過使用者。
        # 旗標只代表「可能改過」，真正要問之前會再跟已存檔的內容比對一次，
        # 使用者把所有修改都復原掉時就不該再問。
        self._document_dirty = False
        self._saved_text_hash = None

        app_log.set_error_callback(self._show_unhandled_error)

        self._widget_polisher = AppWidgetPolisher(self)
        QApplication.instance().installEventFilter(self._widget_polisher)

        # 上次關閉時的介面狀態（深色模式、各種勾選…），畫面建好之後還原。
        self._ui_state = load_ui_state()
        self._pending_side_panel = None

        self._build_ui()
        self._apply_theme()
        self._set_document_actions_enabled(False)
        self.setAcceptDrops(True)
        self._restore_ui_state()

    # ------------------------------------------------------------------
    # UI 建構
    # ------------------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("centralWidget")
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        self.metadata_bar = MetadataBar()
        self.metadata_bar.structure_changed.connect(self._on_structure_changed)
        self.metadata_bar.encoding_changed.connect(self._on_encoding_changed)
        self._panel_toggle_buttons.append(self.metadata_bar.toggle_button)
        root.addWidget(self.metadata_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter = splitter
        splitter.setChildrenCollapsible(False)
        # 卡片之間留一小段距離就好：太寬會把畫面切得零碎，完全貼齊又分不出
        # 是三張不同的卡片。
        splitter.setHandleWidth(10)

        # 左側常駐一張卡片，裡面疊放兩個面板（格式選項／章節管理），一次只
        # 顯示一個：格式選項由工具列「排版設定」開關，章節管理從目錄標題列的
        # 「…」開關；預設全部收起，不需要的設定不用一直佔畫面。
        self.side_card = Card()
        side_layout = QVBoxLayout(self.side_card)
        side_layout.setContentsMargins(0, 0, 0, 0)
        self.options_panel = OptionsPanel(self.format_options)
        self.options_panel.apply_requested.connect(self.apply_formatting)
        self.options_panel.closed.connect(lambda: self._set_active_side_panel(None))
        self.options_panel.ad_scan_requested.connect(self.open_ad_scan_dialog)
        self.options_panel.quote_check_requested.connect(self.open_quote_check_dialog)
        self.options_panel.script_convert_requested.connect(self.open_script_convert_dialog)
        self.options_panel.whitespace_toggled.connect(self._on_whitespace_toggled)
        side_layout.addWidget(self.options_panel)
        self.options_panel.hide()

        self.chapter_panel = ChapterPanel()
        self.chapter_panel.closed.connect(lambda: self._set_active_side_panel(None))
        self.chapter_panel.insert_requested.connect(self.open_insert_title_dialog)
        self.chapter_panel.rules_requested.connect(self.open_rules_dialog)
        self.chapter_panel.toggle_compact_requested.connect(self.toggle_toc_compact)
        self.chapter_panel.check_missing_requested.connect(self.check_missing_chapters)
        self.chapter_panel.allowed_tail_chars_changed.connect(self._on_allowed_tail_chars_changed)
        self.chapter_panel.missing_mode_changed.connect(self._refresh_missing_report)
        self.chapter_panel.report_link_activated.connect(self._on_missing_report_link)
        self.chapter_panel.report_closed.connect(self._on_missing_report_closed)
        side_layout.addWidget(self.chapter_panel)
        self.chapter_panel.hide()

        # 尋找／取代也放進同一張卡片：本文不再被壓縮，搜尋結果也有完整高度。
        self.find_bar = FindBar()
        self.find_bar.closed.connect(self.close_find_bar)
        self.find_bar.bind(
            get_text=lambda: self.editor.toPlainText(),
            on_select=self._find_on_select,
            on_replace_one=self._find_on_replace_one,
            on_replace_all_text=self._find_on_replace_all_text,
            on_matches_changed=self._find_on_matches_changed,
            get_version=self._text_version_now,
            on_message=self._show_status,
        )
        side_layout.addWidget(self.find_bar)
        self.find_bar.hide()

        # 不寫死最小寬度：讓卡片最窄就是「剛好裝得下面板內容」，寫死的數字
        # 一旦比內容窄，拉到最小時下拉框、按鈕就會超出卡片。
        self.side_card.setMaximumWidth(360)
        self.side_card.hide()
        splitter.addWidget(self.side_card)

        self.tree_card = Card()
        tree_layout = QVBoxLayout(self.tree_card)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_layout.setSpacing(0)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.tree.itemClicked.connect(self._on_tree_item_clicked)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_toc_context_menu)
        i18n.skip(self.tree)   # 目錄是書的內容，不跟著介面切換繁簡

        tree_header, tree_header_layout = make_card_header("目錄")
        for icon_name, tooltip, slot in (
            ("refresh-cw", "重新掃描目錄（F5）", self.rescan_toc),
            ("unfold-vertical", "全部展開", self.tree.expandAll),
            ("fold-vertical", "全部摺疊", self.tree.collapseAll),
            ("ellipsis", "章節管理", lambda: self._toggle_side_panel(self.chapter_panel)),
        ):
            button = IconButton(icon_name, tooltip, size=16)
            button.clicked.connect(slot)
            self._icon_buttons.append(button)
            tree_header_layout.addWidget(button)
        tree_layout.addWidget(tree_header)

        tree_body = QVBoxLayout()
        tree_body.setContentsMargins(4, 8, 4, 8)
        tree_body.addWidget(self.tree)
        tree_layout.addLayout(tree_body, 1)
        splitter.addWidget(self.tree_card)

        self.editor_card = Card()
        editor_layout = QVBoxLayout(self.editor_card)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(0)

        # 麵包屑自己吃掉剩下的寬度（stretch=1），所以標題列不另外放彈簧：
        # 兩個都放的話會平分，麵包屑只剩一半寬度可用。
        editor_header, editor_header_layout = make_card_header("本文", with_stretch=False)
        self.breadcrumb_label = ElidedLabel("")
        self.breadcrumb_label.setObjectName("fileLabel")
        i18n.skip(self.breadcrumb_label)
        editor_header_layout.addWidget(self.breadcrumb_label, 1)
        editor_header_layout.addSpacing(8)
        # 章節標記平常藏起來（1px 透明字），但它們是真的寫在檔案裡的。
        # 給一顆切換鈕讓人看得到，再給一顆問號說明每個符號的意思。
        self.marker_button = IconButton("brackets", "顯示章節標記", size=16)
        self.marker_button.setCheckable(True)
        self.marker_button.toggled.connect(self._on_markers_toggled)
        self._icon_buttons.append(self.marker_button)
        editor_header_layout.addWidget(self.marker_button)
        self.marker_help_button = IconButton("circle-help", "章節標記說明", size=16)
        self.marker_help_button.clicked.connect(self._show_marker_help)
        self._icon_buttons.append(self.marker_help_button)
        editor_header_layout.addWidget(self.marker_help_button)
        editor_layout.addWidget(editor_header)

        self.editor = Editor()
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.editor.setPlaceholderText("開啟或把 TXT 檔案拖曳到這裡開始使用（Ctrl+O）")
        self.editor.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.editor.customContextMenuRequested.connect(self._show_editor_context_menu)
        self.editor.undo_requested.connect(self._undo)
        self.editor.redo_requested.connect(self._redo)
        self.editor.zoom_requested.connect(self._on_editor_zoom)
        self.editor.file_dropped.connect(self._open_dropped_file)
        self.editor.textChanged.connect(self._on_editor_text_changed)
        editor_layout.addWidget(self.editor, 1)

        # 游標位置與編碼放在本文卡片自己的底部列，而不是視窗最下面的狀態列：
        # 它們講的是「這份本文」的狀態，貼著本文看比較直覺，也讓狀態列專心
        # 顯示操作結果訊息。
        editor_footer = QWidget()
        editor_footer.setObjectName("cardFooter")
        footer_layout = QHBoxLayout(editor_footer)
        footer_layout.setContentsMargins(18, 8, 18, 8)
        self.cursor_position_label = QLabel("第 1 行 · 第 1 欄")
        self.cursor_position_label.setObjectName("footerLabel")
        # 每次移動游標都會重寫；切換繁簡時由 _update_cursor_position_label 自己重畫。
        i18n.skip(self.cursor_position_label)
        footer_layout.addWidget(self.cursor_position_label)
        footer_layout.addStretch(1)
        self.encoding_footer_label = QLabel("")
        self.encoding_footer_label.setObjectName("footerLabel")
        footer_layout.addWidget(self.encoding_footer_label)
        footer_layout.addSpacing(6)
        footer_separator = QLabel("／")
        footer_separator.setObjectName("footerLabel")
        footer_layout.addWidget(footer_separator)
        footer_layout.addSpacing(6)
        self.zoom_label = ClickableLabel("100%")
        self.zoom_label.setObjectName("footerLabel")
        self.zoom_label.setToolTip("按住 Ctrl 滾動滑鼠滾輪調整字級；按兩下或 Ctrl+0 還原 100%")
        self.zoom_label.double_clicked.connect(lambda: self._on_editor_zoom(0))
        footer_layout.addWidget(self.zoom_label)
        editor_layout.addWidget(editor_footer)
        splitter.addWidget(self.editor_card)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 3)

        splitter_wrap = QWidget()
        splitter_wrap_layout = QHBoxLayout(splitter_wrap)
        splitter_wrap_layout.setContentsMargins(14, 8, 14, 14)
        splitter_wrap_layout.addWidget(splitter)
        root.addWidget(splitter_wrap, 1)

        self.setCentralWidget(central)
        # 工具列縮成只有圖示時，版面本身的最小寬度是 677；設成 680 剛好塞得進
        # 直立螢幕（200% 縮放下只有 720），又不會把內容擠到切掉。
        self.setMinimumSize(680, 420)
        self._show_status("準備就緒")
        self.editor.cursorPositionChanged.connect(self._update_cursor_position_label)

        QShortcut(QKeySequence("Ctrl+F"), self, activated=self.toggle_find_bar)
        QShortcut(QKeySequence("F5"), self, activated=self.rescan_toc)
        # 視窗層級的快捷鍵：焦點在目錄樹（剛做完右鍵選單操作）時按 Ctrl+Z 也要
        # 能復原。焦點在本文時，本文編輯器會自己攔下這兩組按鍵（見
        # widgets.Editor），不會重複觸發。
        QShortcut(QKeySequence("Ctrl+Z"), self, activated=self._undo)
        QShortcut(QKeySequence("Ctrl+Y"), self, activated=self._redo)
        QShortcut(QKeySequence("Ctrl+Shift+Z"), self, activated=self._redo)
        QShortcut(QKeySequence("Ctrl+Shift+L"), self, activated=self.open_log_folder)
        QShortcut(QKeySequence("Esc"), self, activated=self._on_escape)

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("headerBar")
        header.setFixedHeight(64)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(20, 0, 20, 0)
        # 所有按鈕之間用同一個間距，不再分組各自加空白；左右兩群之間只靠
        # 中間的伸縮空間隔開。
        layout.setSpacing(8)

        self.open_button = self._add_text_button(
            layout, "folder-open", "選擇檔案", "開啟 TXT 檔案（Ctrl+O）", self.open_file, "Ctrl+O", primary=True)
        self.one_click_button = self._add_text_button(
            layout, "wand-sparkles", "一鍵排版", "套用常用排版組合", self.one_click_format, None, primary=True)
        self.format_toggle_button = self._add_text_button(
            layout, "sliders-horizontal", "排版設定", "顯示／隱藏格式選項與內容清理",
            lambda: self._toggle_side_panel(self.options_panel), None, checkable=True)
        self.chapter_toggle_button = self._add_text_button(
            layout, "list-tree", "目錄功能", "顯示／隱藏章節管理（也可以按目錄卡片的「…」）",
            lambda: self._toggle_side_panel(self.chapter_panel), None, checkable=True)
        layout.addStretch(1)

        self.undo_button = self._add_header_button(layout, "undo-2", "上一步（Ctrl+Z）", self._undo, None)
        self.redo_button = self._add_header_button(layout, "redo-2", "下一步（Ctrl+Y）", self._redo, None)
        self.clear_button = self._add_header_button(layout, "eraser", "清空重來", self.clear_all, None)
        self.theme_button = self._add_header_button(layout, "moon", "切換深色／淺色模式", self.toggle_theme, None)
        self.language_toggle = LanguageToggle()
        if i18n.available():
            self.language_toggle.setToolTip("介面文字：繁體／簡體（匯出檔名預設跟著切換）")
        else:
            self.language_toggle.setEnabled(False)
            self.language_toggle.setToolTip("需要安裝 OpenCC 才能切換簡體介面")
        self.language_toggle.toggled.connect(self._on_language_toggled)
        layout.addWidget(self.language_toggle)
        self.save_button = self._add_text_button(
            layout, "download", "匯出 TXT", "另存新檔（Ctrl+S）", self.save_file_as, "Ctrl+S", primary=True)
        return header

    def _add_header_button(self, layout, icon_name, tooltip, slot, shortcut) -> IconButton:
        button = IconButton(icon_name, tooltip)
        button.setObjectName("toolbarButton")
        if slot is not None:
            button.clicked.connect(slot)
        if shortcut is not None:
            action = QAction(self)
            action.setShortcut(shortcut)
            if slot is not None:
                action.triggered.connect(slot)
            self.addAction(action)
        self._icon_buttons.append(button)
        layout.addWidget(button)
        return button

    def _add_text_button(self, layout, icon_name, text, tooltip, slot, shortcut,
                          *, primary: bool = False, checkable: bool = False) -> IconTextButton:
        button = IconTextButton(icon_name, text, checkable=checkable)
        button.setToolTip(tooltip)
        if primary:
            button.setObjectName("primary")
            self._primary_buttons.append(button)
        else:
            self._panel_toggle_buttons.append(button)
        if slot is not None:
            button.clicked.connect(slot)
        if shortcut is not None:
            action = QAction(self)
            action.setShortcut(shortcut)
            if slot is not None:
                action.triggered.connect(slot)
            self.addAction(action)
        layout.addWidget(button)
        return button

    def _set_document_actions_enabled(self, enabled: bool):
        for button in (self.one_click_button, self.save_button, self.clear_button,
                       self.format_toggle_button, self.chapter_toggle_button):
            button.setEnabled(enabled)
        self.options_panel.apply_button.setEnabled(enabled)
        self.options_panel.set_cleanup_enabled(enabled)
        if not enabled:
            self._set_active_side_panel(None)
        self._update_history_buttons()

    def _update_history_buttons(self):
        """上一步／下一步按鈕：沒有可以退回或重做的步驟時變灰。打字到一半、
        還沒被計時器存成一步時，也算有「上一步」可退。"""
        typing = self._typing_checkpoint_timer.isActive()
        self.undo_button.setEnabled(self._history_position > 0 or typing)
        self.redo_button.setEnabled(not typing and self._history_position < len(self._history) - 1)

    # ------------------------------------------------------------------
    # 主題
    # ------------------------------------------------------------------

    @action
    def toggle_theme(self):
        self.dark_mode = not self.dark_mode
        self._apply_theme()

    def _apply_theme(self):
        tokens = DARK if self.dark_mode else LIGHT
        set_active_tokens(tokens)
        icons.clear_icon_cache()
        chevron_closed = icons.icon_file_path("chevron-right", tokens.icon, 12)
        chevron_open = icons.icon_file_path("chevron-down", tokens.icon, 12)
        check_mark = icons.icon_file_path("check", tokens.accent_text, 13)
        QApplication.instance().setStyleSheet(build_stylesheet(tokens, chevron_closed, chevron_open, check_mark))
        for button in self._icon_buttons:
            button.set_colors(tokens.icon, tokens.icon_hover, tokens.text_faint)
        for button in self._panel_toggle_buttons:
            button.set_colors(tokens.icon, tokens.icon_hover, tokens.accent)
        # 主要按鈕是常駐彩色底，圖示固定用按鈕文字色，不跟著 hover 變色。
        for button in self._primary_buttons:
            button.set_colors(tokens.accent_text, tokens.accent_text, tokens.accent_text)
        self.chapter_panel.set_icon_colors(tokens.icon)
        self.chapter_panel.set_report_theme(tokens)
        self.options_panel.set_icon_colors(tokens.icon, tokens.accent_text, tokens.accent)
        self.find_bar.set_theme(tokens)
        self.find_bar.close_button.set_colors(tokens.icon, tokens.icon_hover, tokens.text_faint)
        self._apply_editor_style()
        trailing = QColor(tokens.warn_text)
        trailing.setAlpha(60 if self.dark_mode else 38)
        self.editor.set_whitespace_colors(tokens.text_faint, trailing)
        if self.dark_mode:
            self.language_toggle.set_colors(tokens.surface, "#FFFFFF", "#101318", "#657184", "#00000055")
        else:
            self.language_toggle.set_colors("#E9EDF2", "#FFFFFF", tokens.text, tokens.text_faint, "#1018271F")
        self.theme_button.set_icon_name("sun" if self.dark_mode else "moon")
        # 目錄裡自己上色的項目（推定卷、已剪下）要照新主題重算。
        self._style_virtual_volumes()
        self._style_cut_items()
        # 本文裡顯示中的章節標記同理。
        if getattr(self, "_show_title_markers", False):
            self._refresh_title_formats()
        # 繁簡切換鈕跟旁邊的圖示按鈕一樣高（要等樣式表套上 padding 後才量得
        # 準），寬度沿用設計稿的比例（118×52）。
        self.clear_button.ensurePolished()
        button_height = self.clear_button.sizeHint().height()
        self.language_toggle.setFixedSize(round(button_height * 118 / 52), button_height)
        self._sync_side_panel_widths()
        # 只有尋找列開著時才重搜（反白顏色要跟著主題換）；關著時重搜會把
        # 已經清掉的搜尋反白又畫回本文上。
        if self.find_bar.isVisible():
            self.find_bar.refresh()

    # ------------------------------------------------------------------
    # 開檔／存檔
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 視窗大小、多螢幕與顯示比例
    # ------------------------------------------------------------------

    def _restore_window_geometry(self):
        """還原上次的大小與位置；沒有紀錄就依螢幕可用區域決定。"""
        state = load_window_state()
        if state is not None:
            self.setGeometry(state["x"], state["y"], state["width"], state["height"])
            self._start_maximized = state["maximized"]
        else:
            screen = QGuiApplication.primaryScreen()
            available = screen.availableGeometry() if screen else QRect(0, 0, *DEFAULT_WINDOW_SIZE)
            width = min(DEFAULT_WINDOW_SIZE[0], int(available.width() * 0.9))
            height = min(DEFAULT_WINDOW_SIZE[1], int(available.height() * 0.9))
            self.resize(width, height)
            self.move(available.center().x() - width // 2, available.center().y() - height // 2)
            self._start_maximized = False

    def _current_screen(self):
        handle = self.windowHandle()
        return (handle.screen() if handle is not None else None) or QGuiApplication.primaryScreen()

    def _fit_to_screen(self):
        """把視窗夾回目前螢幕的可用範圍。

        直立螢幕、換螢幕、改顯示比例之後，原本的大小可能比整個桌面還大，
        視窗就會有一部分在畫面外而且拉不回來。"""
        screen = self._current_screen()
        if screen is None or self.isMaximized() or self.isFullScreen():
            return
        available = screen.availableGeometry()
        width = min(self.width(), available.width())
        height = min(self.height(), available.height())
        x = min(max(self.x(), available.left()), available.right() - width + 1)
        y = min(max(self.y(), available.top()), available.bottom() - height + 1)
        if (width, height) != (self.width(), self.height()):
            self.resize(width, height)
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def _on_screen_changed(self, _screen=None):
        """換螢幕或顯示比例改變：圖示要照新的比例重畫，視窗要夾回可用範圍。"""
        screen = self._current_screen()
        icons.set_device_scale(screen.devicePixelRatio() if screen else None)
        self._connect_screen_signals(screen)
        self._apply_theme()
        self._fit_to_screen()
        self._update_toolbar_compact()

    def _connect_screen_signals(self, screen):
        """只接目前這一台螢幕的訊號，換螢幕時把舊的斷掉。"""
        previous = getattr(self, "_watched_screen", None)
        if previous is screen:
            return
        if previous is not None:
            for signal in (previous.geometryChanged, previous.availableGeometryChanged,
                           previous.logicalDotsPerInchChanged, previous.physicalDotsPerInchChanged):
                try:
                    signal.disconnect(self._on_screen_metrics_changed)
                except (RuntimeError, TypeError):
                    pass
        self._watched_screen = screen
        if screen is not None:
            for signal in (screen.geometryChanged, screen.availableGeometryChanged,
                           screen.logicalDotsPerInchChanged, screen.physicalDotsPerInchChanged):
                signal.connect(self._on_screen_metrics_changed)

    def _on_screen_metrics_changed(self, *_args):
        self._on_screen_changed()

    def showEvent(self, event):
        super().showEvent(event)
        if getattr(self, "_screen_watch_ready", False):
            return
        self._screen_watch_ready = True
        handle = self.windowHandle()
        if handle is not None:
            handle.screenChanged.connect(self._on_screen_changed)
        self._on_screen_changed()
        if getattr(self, "_start_maximized", False):
            self.showMaximized()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_toolbar_compact()

    def _update_toolbar_compact(self):
        """視窗太窄時，工具列上有文字的按鈕改成只顯示圖示。

        整排按鈕不會換行也不會縮，原本的最小寬度是 958；直立螢幕在 200%
        縮放下只有 720 可用，視窗根本縮不進去。"""
        width = self.width()
        compact = self._toolbar_compact
        if not compact and width < COMPACT_TOOLBAR_WIDTH:
            compact = True
        elif compact and width > FULL_TOOLBAR_WIDTH:
            compact = False
        if compact == self._toolbar_compact:
            return
        self._toolbar_compact = compact
        for button in (self.open_button, self.one_click_button, self.format_toggle_button,
                       self.chapter_toggle_button, self.save_button):
            button.set_compact(compact)

    def _confirm_discard_changes(self) -> bool:
        """換掉目前這份正文之前先問過：可以先匯出、直接捨棄或取消。

        回傳 True 代表可以繼續（已匯出或使用者選擇捨棄）。只看正文有沒有
        改過；標題粗體這種純顯示的格式不算。"""
        if not self._document_dirty:
            return True
        if self._saved_text_hash is not None and hash(self.editor.toPlainText()) == self._saved_text_hash:
            self._document_dirty = False     # 改過又全部復原，等於沒改
            return True
        box = QMessageBox(QMessageBox.Icon.Warning, i18n.T("尚未匯出"),
                          i18n.T("目前的修改還沒有匯出，繼續下去會遺失。要先匯出嗎？"), parent=self)
        save_button = box.addButton(i18n.T("匯出 TXT"), QMessageBox.ButtonRole.AcceptRole)
        discard_button = box.addButton(i18n.T("不匯出，直接繼續"), QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(i18n.T("取消"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_button:
            return self.save_file_as()      # 匯出成功才算可以繼續
        return clicked is discard_button

    def closeEvent(self, event):
        # 繁簡轉換為了更新進度會讓 Qt 處理事件，這時按右上角關閉會在轉換到
        # 一半跳出「尚未匯出」詢問；轉換期間直接擋掉，完成後再關。
        if getattr(self, "_long_task_running", False):
            event.ignore()
            self._show_status("轉換進行中，完成後再關閉")
            return
        if not self._confirm_discard_changes():
            event.ignore()
            return
        geometry = self.normalGeometry() if self.isMaximized() else self.geometry()
        save_window_state(geometry.x(), geometry.y(), geometry.width(), geometry.height(),
                          self.isMaximized())
        save_ui_state(self._collect_ui_state())
        event.accept()

    # ------------------------------------------------------------------
    # 記住上次的介面狀態
    # ------------------------------------------------------------------

    def _collect_ui_state(self) -> dict:
        """關閉前的介面狀態。只記「怎麼用這個程式」的偏好，不記跟某個檔案
        綁在一起的東西（編碼、結構、選取的章節…）。"""
        side_panel = ("options" if self.options_panel.isVisible() else
                      "chapter" if self.chapter_panel.isVisible() else None)
        state = dict(self._ui_state)      # 各對話框的勾選已經記在這裡
        state.update({
            "dark_mode": self.dark_mode,
            "simplified": self.language_toggle.is_simplified(),
            "editor_zoom": self._editor_zoom,
            "show_title_markers": self.marker_button.isChecked(),
            "strip_markers_on_export": self._strip_markers_on_export,
            "show_whitespace": self.options_panel.whitespace_button.isChecked(),
            "metadata_expanded": self.metadata_bar.toggle_button.isChecked(),
            "side_panel": side_panel if self.raw_lines and any(self.raw_lines) else
            (self._pending_side_panel or side_panel),
            "splitter": bytes(self.splitter.saveState().toHex()).decode("ascii"),
            "toc_compact_mode": self.toc_compact_mode,
            "format_options": self.options_panel.options_state(),
            "missing_mode": self.chapter_panel.missing_mode(),
            "allowed_tail_chars": self.allowed_tail_chars,
            "find_regex": self.find_bar.regex_button.isChecked(),
        })
        return state

    def _restore_ui_state(self):
        """把上次的介面狀態套回來。每一項都獨立檢查，存檔裡缺的或壞的就用預設值。"""
        state = self._ui_state
        if state.get("dark_mode"):
            self.dark_mode = True
            self._apply_theme()
        if state.get("simplified") and i18n.available():
            self.language_toggle.set_simplified(True)
            self._on_language_toggled(True)
        zoom = state.get("editor_zoom")
        if isinstance(zoom, int) and EDITOR_ZOOM_MIN <= zoom <= EDITOR_ZOOM_MAX and zoom != 100:
            self._editor_zoom = zoom
            self._apply_editor_style()
            self.zoom_label.setText(f"{zoom}%")
        self._strip_markers_on_export = bool(state.get("strip_markers_on_export", True))
        if state.get("show_title_markers"):
            self.marker_button.setChecked(True)
        if state.get("show_whitespace"):
            self.options_panel.whitespace_button.setChecked(True)
        if state.get("metadata_expanded"):
            self.metadata_bar.toggle_button.setChecked(True)
        self.toc_compact_mode = bool(state.get("toc_compact_mode"))
        if isinstance(state.get("format_options"), dict):
            self.options_panel.restore_options_state(state["format_options"])
        mode = state.get("missing_mode")
        if isinstance(mode, str) and self.chapter_panel.missing_mode_combo.findText(mode) >= 0:
            i18n.set_combo_value(self.chapter_panel.missing_mode_combo, mode)
        tail = state.get("allowed_tail_chars")
        if isinstance(tail, str) and tail:
            self.allowed_tail_chars = tail
            self.chapter_panel.set_allowed_tail_chars(tail)
        splitter = state.get("splitter")
        if isinstance(splitter, str) and splitter:
            try:
                self.splitter.restoreState(QByteArray.fromHex(splitter.encode("ascii")))
            except (ValueError, UnicodeError):
                pass
        if state.get("find_regex"):
            self.find_bar.regex_button.setChecked(True)
        # 左側面板要等有檔案才能開（沒有檔案時那些按鈕是停用的）。
        if state.get("side_panel") in ("options", "chapter"):
            self._pending_side_panel = state["side_panel"]
        # 還原過程中各項會在狀態列留下訊息，最後統一改回來。
        self._show_status("準備就緒")

    def _open_pending_side_panel(self):
        """第一次開檔後，把上次開著的左側面板打開。"""
        panel = {"options": self.options_panel, "chapter": self.chapter_panel}.get(self._pending_side_panel)
        self._pending_side_panel = None
        if panel is not None and not self.side_card.isVisible():
            self._set_active_side_panel(panel)

    @action
    def open_file(self):
        if not self._confirm_discard_changes():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, i18n.T("開啟 TXT 檔案"), "", i18n.T("文字檔 (*.txt);;所有檔案 (*)"))
        if not path:
            return
        self.load_file_path(path)

    @action
    def _open_dropped_file(self, path: str):
        """拖曳到本文卡片：跟拖到視窗其他地方一樣，只接受 TXT。"""
        if not path.lower().endswith(".txt"):
            dialogs.error(self, "無法載入", "請拖曳一個有效的 TXT 檔案。")
            return
        if self._confirm_discard_changes():
            self.load_file_path(path)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path and path.lower().endswith(".txt"):
                event.acceptProposedAction()
                if self._confirm_discard_changes():
                    self.load_file_path(path)
                return
        dialogs.error(self, "無法載入", "請拖曳一個有效的 TXT 檔案。")

    @action
    def load_file_path(self, path: str, encoding: str | None = None):
        encoding = encoding or smart_detect_encoding(path)
        try:
            content, damaged = self._read_document(path, encoding)
        except OSError as error:
            dialogs.error(self, "讀取失敗", f"無法開啟檔案：\n{path}\n\n{error}")
            return
        if content is None:
            return
        content, removed_boms = strip_stray_bom(content)

        self.close_find_bar()
        self.input_file = path
        self.detected_encoding = encoding
        self.raw_lines = content.split("\n")
        self.auto_titles = {}
        self.force_lv1_chapters = set()
        self.force_lv2_chapters = set()
        self.ignored_chapters = set()
        self.structure_mode = DEFAULT_STRUCTURE_MODE
        self.format_options.structure = DEFAULT_STRUCTURE_MODE
        self.toc_full_labels = {}
        self.chapter_raw_map = {}
        self._pending_line_maps = []
        self._missing_report_active = False
        self._cut_state = None
        self.chapter_panel.clear_report()

        self.metadata_bar.reset()
        self.metadata_bar.set_structure(DEFAULT_STRUCTURE_MODE)
        encoding_choice = next((label for label, codec in ENCODING_CODECS.items() if codec == encoding), "自動")
        self.metadata_bar.set_encoding_choice(encoding_choice)

        self._history = []
        self._history_position = -1
        self._set_editor_text(content)
        # 換過整份內容之後游標會留在最後一行，剛開檔卻應該從頭看起。
        self.editor.moveCursor(QTextCursor.MoveOperation.Start)

        self._mark_synced(content)
        self._rebuild_toc()
        self._autofill_book_metadata()
        try:
            size_mb = os.path.getsize(path) / (1024 * 1024)
        except OSError:      # 讀完之後檔案被移走或改名
            size_mb = len(content.encode(encoding, "replace")) / (1024 * 1024)
        self.metadata_bar.set_filename(os.path.basename(path), f"{size_mb:.2f} MB")
        self.metadata_bar.set_encoding_badge(self.detected_encoding.upper())
        self.encoding_footer_label.setText(
            f"{self.detected_encoding.upper()} · {detect_line_ending(path)}")
        status = i18n.T("已載入：") + os.path.basename(path)
        if damaged:
            status += i18n.T(f"；有 {damaged} 個字元無法以 {encoding.upper()} 解碼（顯示為 �），存檔會永久遺失")
        if removed_boms:
            status += i18n.T(f"；已移除 {removed_boms} 個夾在行首的 BOM 字元（多檔串接留下的，會讓章節辨識失敗）")
        self._show_status(status, translated=True)
        log.info("載入 %s：%.2f MB、編碼 %s、%d 行、目錄 %d 項（推定卷 %d）、移除 BOM %d 個",
                 os.path.basename(path), size_mb, encoding, len(self.raw_lines), len(self.chapter_raw_map),
                 len(self.virtual_volume_items), removed_boms)
        self._set_document_actions_enabled(True)
        if self._pending_side_panel:
            self._open_pending_side_panel()
        self._checkpoint_document()   # 建立復原歷史的第一步（載入後的初始狀態）
        self._document_dirty = False
        self._saved_text_hash = hash(content)

    def _read_document(self, path: str, encoding: str):
        """先嚴格解碼；編碼不符時問過使用者才容錯開啟。

        不預設容錯：解不開的位元組會變成「�」，一旦照這樣編輯、匯出，原本
        的字就真的沒了，而且使用者不一定看得出來。回傳（內容, 壞掉的字元數），
        內容為 None 代表使用者選擇不開啟。"""
        try:
            return read_text(path, encoding), 0
        except (UnicodeError, LookupError) as error:
            log.warning("以 %s 嚴格解碼失敗：%s", encoding, error)
        if not dialogs.confirm(
            self, "編碼可能不符",
            f"以 {encoding.upper()} 解讀「{os.path.basename(path)}」時發現無法解碼的內容。\n\n"
            "可以改用容錯模式開啟（解不開的字會顯示成 �，直接存檔會讓這些字永久遺失），\n"
            "或是取消後在「書籍資料 → 讀取編碼」改選其他編碼再試。\n\n要用容錯模式開啟嗎？",
        ):
            self._show_status("已取消開啟：編碼可能不符")
            return None, 0
        try:
            return read_text_lossy(path, encoding)
        except (UnicodeError, LookupError) as error:
            dialogs.error(self, "讀取失敗", f"無法以 {encoding.upper()} 讀取檔案：\n{error}")
            return None, 0

    def _autofill_book_metadata(self):
        """書名／作者／狀態只在還沒填過時才自動帶入，不覆蓋使用者已輸入的值。"""
        if self.metadata_bar.book_title():
            return
        filename_title, filename_author, filename_status = extract_filename_metadata(self.input_file)
        self.metadata_bar.set_title_if_empty(filename_title)
        self.metadata_bar.set_author_if_empty(filename_author)
        if not self.metadata_bar.author():
            intro_author = extract_author_from_intro(self.raw_lines)
            self.metadata_bar.set_author_if_empty(intro_author)
        self.metadata_bar.set_status(filename_status)

    @action
    def _on_structure_changed(self, value: str):
        if not self.input_file:
            return
        self.structure_mode = value
        self.format_options.structure = value
        self.rescan_toc()

    @action
    def _on_encoding_changed(self, choice: str):
        """換編碼＝整份重新讀檔，會丟掉目前的修改，所以要先問過。"""
        if not self.input_file:
            return
        if not self._confirm_discard_changes():
            # 使用者取消：下拉選單要轉回原本的編碼，不然顯示的跟實際讀的不一致。
            current = next((label for label, codec in ENCODING_CODECS.items()
                            if codec == self.detected_encoding), "自動")
            self.metadata_bar.set_encoding_choice(current)
            return
        encoding = smart_detect_encoding(self.input_file) if choice == "自動" else ENCODING_CODECS[choice]
        self.load_file_path(self.input_file, encoding=encoding)

    def _on_allowed_tail_chars_changed(self, value: str):
        self.allowed_tail_chars = value
        if self.input_file:
            self.rescan_toc()

    def _set_editor_text(self, text: str):
        """換掉整份內容，並在同一個編輯區塊內順便套用行距，避免行距跟內容
        分成肉眼可見的兩階段變化。"""
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(text)
        cursor.select(QTextCursor.SelectionType.Document)
        block_format = QTextBlockFormat()
        block_format.setLineHeight(150, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        cursor.mergeBlockFormat(block_format)
        cursor.endEditBlock()

    # ------------------------------------------------------------------
    # 復原／重做：整份文字＋章節結構狀態的快照
    # ------------------------------------------------------------------

    def _on_editor_text_changed(self):
        """打字時每個按鍵都會觸發；用計時器合併成「停頓 450ms 才存一步」，
        不然復原歷史會被逐字灌爆。結構性操作（合併／整理／連續編號…）自己
        會在動作結束時立刻呼叫 _checkpoint_document，不用等這個計時器。"""
        if self._applying_formats:
            return          # 只是套標題粗體／隱藏標記，正文一個字都沒變
        self._text_version += 1
        self._document_dirty = True
        if self.find_bar.isVisible():
            # 搜尋結果記的是字元位置，正文一變就全部作廢，不能再拿去取代。
            self.find_bar.invalidate()
        if self._restoring_history:
            return
        self._typing_checkpoint_timer.start(TYPING_CHECKPOINT_DELAY_MS)
        self._update_history_buttons()

    @timed
    def _checkpoint_document(self):
        """把目前的正文與章節結構存成一步；跟最新一步完全相同就不重複存。"""
        if self._restoring_history:
            return
        # 這一步已經包含還沒被計時器存起來的輸入，計時器不用再跑。
        self._typing_checkpoint_timer.stop()
        # 先把「記在第幾行」的章節狀態對到目前的文字，否則快照會是「新的文字
        # 配舊的行號」，復原之後強制層級、忽略標記會落在別行（審查報告 C-11）。
        self._sync_raw_lines()
        text = (self._synced_text if self._synced_text_version == self._text_version
                and self._synced_text is not None else self.editor.toPlainText())
        state = (
            text,
            frozenset(self.ignored_chapters),
            frozenset(self.force_lv1_chapters),
            frozenset(self.force_lv2_chapters),
            dict(self.auto_titles),
        )
        if self._history_position >= 0 and self._history[self._history_position] == state:
            self._update_history_buttons()
            return
        del self._history[self._history_position + 1:]
        self._history.append(state)
        self._trim_history()
        self._history_position = len(self._history) - 1
        self._update_history_buttons()

    def _trim_history(self):
        """同時套用步數上限與記憶體預算，從最舊的步驟開始丟棄。"""
        if len(self._history) > MAX_HISTORY_STEPS:
            del self._history[:len(self._history) - MAX_HISTORY_STEPS]
        while len(self._history) > MIN_HISTORY_STEPS:
            total = sum(len(step[0]) for step in self._history)
            if total <= MAX_HISTORY_CHARS:
                break
            del self._history[0]

    @action
    def _undo(self):
        self._restore_document_step(-1)

    @action
    def _redo(self):
        self._restore_document_step(1)

    @timed
    def _restore_document_step(self, direction: int):
        self._typing_checkpoint_timer.stop()
        # 復原／重做前先把「正在輸入到一半、還沒被計時器存檔」的內容存起來，
        # 不然這段修改會直接消失，也跳不回來。
        self._checkpoint_document()
        target = self._history_position + direction
        if not 0 <= target < len(self._history):
            self._show_status("沒有可復原的步驟" if direction < 0 else "沒有可重做的步驟")
            return
        state = self._history[target]
        cursor_position = self.editor.textCursor().position()
        self._restoring_history = True
        try:
            self._set_editor_text(state[0])
            # 先記下行號位移（目錄選取要用），再用快照整組覆蓋章節狀態。
            self._adopt_lines(state[0].split("\n"), remap_state=False)
            self.ignored_chapters = set(state[1])
            self.force_lv1_chapters = set(state[2])
            self.force_lv2_chapters = set(state[3])
            self.auto_titles = dict(state[4])
            self._history_position = target
            self._rebuild_toc()
            new_cursor = self.editor.textCursor()
            new_cursor.setPosition(min(cursor_position, self.editor.document().characterCount() - 1))
            self.editor.setTextCursor(new_cursor)
        finally:
            self._restoring_history = False
        self._update_history_buttons()
        self._show_status("已復原上一步" if direction < 0 else "已重做")

    def _on_editor_zoom(self, step: int):
        """Ctrl＋滾輪：本文字級每格 10%，範圍 50%～300%；只影響顯示。"""
        zoom = 100 if step == 0 else min(EDITOR_ZOOM_MAX, max(EDITOR_ZOOM_MIN, self._editor_zoom + step * 10))
        if zoom == self._editor_zoom:
            return
        self._editor_zoom = zoom
        self._apply_editor_style()
        self.zoom_label.setText(f"{zoom}%")
        self._format_refresh_timer.start()

    def _apply_editor_style(self):
        """本文的字級與文字選取色。

        字級是整份樣式表的 * 規則給的，對元件呼叫 setFont／zoomIn 會被樣式表
        蓋掉；只有元件自己的樣式表比全域的優先，所以用這個方式改。

        文字反白一律是搜尋「目前這一筆」的橘色：搜尋、從對話框跳到某一行、
        平常用滑鼠選字，看起來都是同一種反白。"""
        tokens = DARK if self.dark_mode else LIGHT
        rules = [f"font-size: {round(EDITOR_BASE_FONT_PX * self._editor_zoom / 100)}px;",
                 f"selection-background-color: {tokens.find_current_bg};",
                 f"selection-color: {tokens.text};"]
        self.editor.setStyleSheet(" ".join(rules))

    def _positions(self) -> PositionMap:
        """目前正文的「Python 位置 ↔ Qt 位置」換算表（依文字版本快取）。"""
        if self._position_map_version != self._text_version:
            self._position_map = PositionMap(self.editor.toPlainText())
            self._position_map_version = self._text_version
        return self._position_map

    def _text_version_now(self) -> int:
        return self._text_version

    def _ensure_toc_current(self):
        """正文行數變過之後，目錄記的行號就指到別行了；先重建目錄再動手。

        重建時會依行號位移把原本選取的章節對到新節點（見 _capture_tree_view），
        所以使用者的選取不會跑掉。"""
        if self._toc_text_version != self._text_version:
            self._rebuild_toc()

    def _update_cursor_position_label(self):
        cursor = self.editor.textCursor()
        self.cursor_position_label.setText(i18n.T("第 {line} 行 · 第 {column} 欄").format(
            line=cursor.blockNumber() + 1, column=cursor.positionInBlock() + 1))
        self._update_breadcrumb(cursor.blockNumber())

    @action
    def _on_whitespace_toggled(self, enabled: bool):
        self.editor.set_show_whitespace(enabled)
        self._show_status("已顯示空格：半形 ·、全形 □、Tab →，行尾多餘空白標紅" if enabled else "已隱藏空格")

    # ------------------------------------------------------------------
    # 介面繁／簡
    # ------------------------------------------------------------------

    @action
    def _on_language_toggled(self, simplified: bool):
        """只換介面文字，不動本文與目錄；匯出檔名的繁簡預設跟著換，仍可在
        「書籍資料」裡另外指定。"""
        i18n.set_simplified(simplified)
        i18n.install_qt_translation(QApplication.instance())
        for widget in QApplication.topLevelWidgets():
            if widget.isVisible() or widget is self:
                i18n.retranslate(widget)
        self.metadata_bar.set_filename_script(SCRIPT_SIMP if simplified else SCRIPT_TRAD)
        self.metadata_bar.refresh_language()
        self.chapter_panel.refresh_language()
        self._update_cursor_position_label()
        self._style_virtual_volumes()
        self._show_status("介面已切換為簡體" if simplified else "介面已切換為繁體")

    def open_log_folder(self):
        """Ctrl+Shift+L：打開記錄檔資料夾（回報問題時附上 app.log、faults.log）。"""
        app_log.LOG_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(app_log.LOG_DIR)))

    def _show_unhandled_error(self, summary: str):
        """程式內部錯誤：記錄檔已經寫好了，這裡只告訴使用者去哪裡找。"""
        box = QMessageBox(QMessageBox.Icon.Critical, i18n.T("發生未預期的錯誤"),
                          i18n.T("剛才的操作沒有完成，詳細內容已寫入記錄檔：\n")
                          + f"{app_log.LOG_FILE}\n\n{summary}", parent=self)
        open_button = box.addButton(i18n.T("開啟記錄檔資料夾"), QMessageBox.ButtonRole.ActionRole)
        box.addButton(i18n.T("確定"), QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        if box.clickedButton() is open_button:
            self.open_log_folder()

    def _show_status(self, message: str, *, translated: bool = False):
        """狀態列訊息一律經過這裡，才會跟著介面切換繁簡。"""
        self.statusBar().showMessage(message if translated else i18n.T(message))

    def _update_breadcrumb(self, line_number: int):
        """本文卡片右上角顯示游標目前落在哪一卷／哪一章。"""
        position = bisect.bisect_right(self._breadcrumb_rows, line_number + 1) - 1
        current = self._breadcrumb_items[position] if position >= 0 else None
        if current is self._breadcrumb_current:
            return          # 還在同一章，連文字都不用重設
        self._breadcrumb_current = current
        if current is None:
            self.breadcrumb_label.setText("")
            return
        names = []
        node = current
        while node is not None:
            names.insert(0, self.toc_full_labels.get(node, node.text(0)))
            node = node.parent()
        self.breadcrumb_label.setText(" / ".join(names))

    @action
    def save_file_as(self):
        content = self.editor.toPlainText()
        if not content.strip():
            return False
        default_name = self._suggest_export_filename()
        path, _ = QFileDialog.getSaveFileName(self, i18n.T("另存新檔"), default_name, i18n.T("文字檔 (*.txt)"))
        if not path:
            return False
        stripped = 0
        if self._strip_markers_on_export:
            content, stripped = strip_export_markers(content)
        try:
            # 寫暫存檔、成功才取代目標檔：中途失敗時原本的檔案不會被清空。
            write_text_atomic(path, content)
        except (OSError, UnicodeError) as error:
            dialogs.error(self, "存檔失敗", f"無法寫入檔案：\n{path}\n\n{error}\n\n原本的檔案沒有被更動。")
            return False
        if stripped:
            # 寫出去的內容跟編輯器裡的不一樣（少了標記），所以這次不算「已存檔」：
            # 關閉前還是會提醒一次，要留住目錄狀態的話可以再存一份沒移除的。
            note = i18n.T("（已移除 %d 個章節標記，這份檔案重新開啟時不會保留手動調整過的"
                          "目錄；本文仍算未存檔）")
            self._show_status(i18n.T("已存檔：") + path + note % stripped, translated=True)
            return True
        self._document_dirty = False
        self._saved_text_hash = hash(content)
        self._show_status(i18n.T("已存檔：") + path, translated=True)
        return True

    def _suggest_export_filename(self) -> str:
        title = self.metadata_bar.book_title()
        if not title:
            return os.path.basename(self.input_file) if self.input_file else "未命名.txt"
        has_fanwai = any(
            "番外" in self.raw_lines[row - 1]
            for row in self.chapter_index_map.values() if 0 < row <= len(self.raw_lines)
        )
        name = build_smart_filename(
            title, self.metadata_bar.author(), self.metadata_bar.status(),
            self.metadata_bar.last_vol_text(), self.metadata_bar.last_ch_text(), has_fanwai)
        return convert_script(name, self.metadata_bar.filename_script())

    # ------------------------------------------------------------------
    # 目錄樹
    # ------------------------------------------------------------------

    @action
    def rescan_toc(self):
        """依目前編輯器內容重新辨識章節（含重新學習文件規律）。"""
        if not self.editor.toPlainText().strip():
            return
        self._sync_raw_lines()
        self._force_last_found = True
        try:
            self._rebuild_toc()
        finally:
            self._force_last_found = False
        self._show_status("已依目前內文重新產生目錄")

    @action
    def clear_all(self):
        """清空目前檔案與所有章節標記狀態，回到剛啟動時的樣子。

        復原歷史整個重設（不是疊加一筆「清空後」的快照）：
        清空之後沒有「上一步」可以復原，這是刻意的——清空前的內容已經
        用「另存新檔」或原始檔案保住了，不需要靠復原堆疊撐著。
        """
        if not self.input_file and not self.editor.toPlainText().strip():
            return
        if self._document_dirty:
            if not self._confirm_discard_changes():
                return
        elif not dialogs.confirm(self, "清空重來", "確定要清空目前的內容與所有章節標記狀態嗎？"):
            return
        self.close_find_bar()
        self.input_file = ""
        self.raw_lines = [""]
        self.chapter_index_map = {}
        self.chapter_raw_map = {}
        self.chapter_records = {}
        self.toc_boundary_map = {}
        self.auto_titles = {}
        self.force_lv1_chapters = set()
        self.force_lv2_chapters = set()
        self.ignored_chapters = set()
        self.tree.clear()
        self._cut_state = None
        self._breadcrumb_rows = []
        self._breadcrumb_items = []
        self._breadcrumb_current = None
        self._mark_synced("")
        self.virtual_volume_items = {}
        self._missing_report_active = False
        self.chapter_panel.clear_report()
        self._set_editor_text("")
        self.toc_full_labels = {}
        self.toc_compact_labels = {}
        self._pending_line_maps = []
        self.metadata_bar.reset()
        self.breadcrumb_label.setText("")
        self.encoding_footer_label.setText("")
        self._history = []
        self._history_position = -1
        self._checkpoint_document()
        self._document_dirty = False
        self._saved_text_hash = hash("")
        self._set_document_actions_enabled(False)
        self._show_status("已清空")

    def _build_context(self) -> BuildContext:
        return BuildContext(
            raw_lines=self.raw_lines,
            options=self.format_options,
            user_chapter_rules=self.user_chapter_rules,
            auto_titles=self.auto_titles,
            force_lv1_chapters=self.force_lv1_chapters,
            force_lv2_chapters=self.force_lv2_chapters,
            ignored_chapters=self.ignored_chapters,
            invalid_tail_regex=build_invalid_tail_regex(self.allowed_tail_chars),
        )

    @timed
    def _rebuild_toc(self):
        ctx = self._build_context()
        result = build_document_structure(ctx, apply_format=False, write_text=False)
        self._populate_tree(result)

    def _label_path(self, item) -> tuple:
        path = []
        while item is not None:
            path.append(self.toc_full_labels.get(item, item.text(0)))
            item = item.parent()
        return tuple(reversed(path))

    def _capture_tree_view(self):
        """重畫目錄前記下畫面狀態：哪些節點展開、選了哪些章節、捲到哪裡。

        選取用「正文行號」記（再經過這段期間的行號位移換算），不是用節點
        物件——重畫後節點全是新的。展開狀態用「標題路徑」記，行號位移不影響。
        """
        expanded = {self._label_path(item): item.isExpanded()
                    for item in self.toc_full_labels if item.childCount()}
        current = self.tree.currentItem()
        current_row = self.chapter_raw_map.get(current) if current is not None else None
        selected_rows = [self.chapter_raw_map[item] for item in self.tree.selectedItems()
                         if item in self.chapter_raw_map]
        return {
            "expanded": expanded,
            "current": None if current_row is None else self._map_tree_line(current_row),
            "selected": [self._map_tree_line(row) for row in selected_rows],
            "scroll": self.tree.verticalScrollBar().value(),
        }

    def _restore_tree_view(self, view):
        for item in self.toc_full_labels:
            if item.childCount():
                state = view["expanded"].get(self._label_path(item))
                if state is not None:
                    item.setExpanded(state)
        if not view["selected"] and view["current"] is None:
            return
        ordered = sorted(self.chapter_raw_map.items(), key=lambda pair: pair[1])
        rows = [row for _item, row in ordered]

        def nearest(line):
            # 章節還在就選回同一個；被刪掉了就選它前面最近的那一個。
            position = bisect.bisect_right(rows, line) - 1
            return ordered[max(position, 0)][0] if ordered else None

        current = nearest(view["current"]) if view["current"] is not None else None
        # 設定目前項目時 Qt 會自動捲過去，捲的時候順便把收合的上層展開——
        # 使用者收起的卷就被打開了。還原期間先關掉自動捲動。
        self.tree.setAutoScroll(False)
        try:
            if current is not None:
                self.tree.setCurrentItem(current)
            for line in view["selected"]:
                item = nearest(line)
                if item is not None:
                    item.setSelected(True)
        finally:
            self.tree.setAutoScroll(True)
        self.tree.verticalScrollBar().setValue(view["scroll"])
        # scrollToItem 會把收合的上層自動展開，使用者收起的卷就被打開了；
        # 選到的章節藏在收合的卷底下時，只還原捲動位置就好。
        parent = current.parent() if current is not None else None
        while parent is not None and parent.isExpanded():
            parent = parent.parent()
        if current is not None and parent is None:
            self.tree.scrollToItem(current, QTreeWidget.ScrollHint.EnsureVisible)

    @timed
    def _populate_tree(self, result):
        view = self._capture_tree_view() if self.toc_full_labels else None
        self.tree.clear()
        node_map = {}

        def populate(src_parent, dst_parent):
            for child in result.tree.get_children(src_parent):
                item = QTreeWidgetItem([result.tree.item(child, "text")])
                if dst_parent is None:
                    self.tree.addTopLevelItem(item)
                else:
                    dst_parent.addChild(item)
                item.setExpanded(bool(result.tree.item(child, "open")))
                node_map[child] = item
                populate(child, item)

        populate("", None)
        self.virtual_volume_items = {node_map[n]: dict(info) for n, info in result.virtual_volumes.items()}
        self.chapter_raw_map = {node_map[n]: row for n, row in result.chapter_raw_map.items()}
        self.chapter_index_map = {node_map[n]: row for n, row in result.chapter_index_map.items()}
        self.chapter_records = {node_map[n]: dict(record) for n, record in result.chapter_records.items()}
        self.toc_boundary_map = dict(self.chapter_index_map)

        self._toc_text_version = self._text_version
        # 麵包屑要靠「行號 → 章節」查表；先排好序，游標移動時就只要二分搜尋，
        # 不必每次掃過全部章節（審查報告 C-20）。
        self._breadcrumb_rows = sorted(self.chapter_index_map.values())
        rows_to_item: dict = {}
        for item, row in self.chapter_index_map.items():
            rows_to_item.setdefault(row, item)
        self._breadcrumb_items = [rows_to_item[row] for row in self._breadcrumb_rows]
        self._breadcrumb_current = None
        self.breadcrumb_label.setText("")
        self.toc_full_labels = {item: item.text(0) for item in node_map.values()}
        self.toc_compact_labels = {item: short_toc_label(label) for item, label in self.toc_full_labels.items()}
        self._apply_toc_label_mode()
        self._pending_line_maps = []
        if view is not None:
            self._restore_tree_view(view)
        self._apply_title_formats()
        self._style_virtual_volumes()
        self._style_cut_items()
        self.metadata_bar.set_last_found(result.last_found_vol, result.last_found_ch,
                                         force=self._force_last_found)
        if self._missing_report_active:
            self._refresh_missing_report()
        # 目錄剛重建，行號跟本文一致；麵包屑照游標現在的位置重新顯示一次，
        # 不然改了章名、刪了章節之後還是舊的字，要移動游標才會更新。
        self._update_breadcrumb(self.editor.textCursor().blockNumber())

    def _style_virtual_volumes(self):
        """推定卷用斜體＋琥珀色標示：本文裡沒有這個卷標題，是從卷結尾行推出來的。"""
        tokens = DARK if self.dark_mode else LIGHT
        tooltip = i18n.T("推定卷：本文沒有這個卷標題，是從卷結尾行或章號重新起算推得的。\n"
                         "在目錄按右鍵可以把卷標題寫進本文。")
        for item in self.virtual_volume_items:
            font = item.font(0)
            font.setItalic(True)
            item.setFont(0, font)
            item.setForeground(0, QColor(tokens.virtual_text))
            item.setToolTip(0, tooltip)

    def _refresh_title_formats(self):
        """重畫標題粗體與標記樣式；正文行數變過就先重建目錄（重建時會順便重畫）。

        粗體與隱藏標記都是照「目錄記的行號」畫的。使用者打字多了幾行之後
        直接重畫，粗體會落在錯的行——實測在開頭多打兩行再縮放字級，粗體跑到
        新打的第一行和一行正文上，真正的章節標題反而變細；隱藏的 [::] 也會
        因此露出來。"""
        if self._toc_text_version != self._text_version:
            self._sync_raw_lines()
            self._rebuild_toc()
        else:
            self._apply_title_formats()

    @timed
    def _apply_title_formats(self):
        """把章節標題那幾行加粗放大，本文區塊才看得出層次。

        純粹是顯示用的字元格式，不動到任何一個字：復原快照比對的是文字與
        章節狀態，格式改變不會多存一步歷史。先把整份格式清乾淨再重畫，
        否則使用者把標題改成內文後，那行還會繼續粗體。
        （150,000 行、1,000 章的檔案實測：清除 0.03 秒、重畫 0.01 秒。）
        """
        document = self.editor.document()
        title_format = QTextCharFormat()
        title_format.setFontWeight(QFont.Weight.Bold)
        # 樣式表用 px 指定字級，這時 pointSizeF() 是 -1；要照字型實際用的單位放大。
        base_font = self.editor.font()
        if base_font.pixelSize() > 0:
            title_format.setProperty(QTextFormat.Property.FontPixelSize, round(base_font.pixelSize() * 1.25))
        else:
            title_format.setFontPointSize(base_font.pointSizeF() * 1.25)

        plain_format = QTextCharFormat()
        cursor = QTextCursor(document)
        # 用 try/finally：套格式中途若出錯，旗標留在 True 的話之後打字都不會
        # 再建立復原快照，而且完全沒有跡象（審查報告 C-24）。
        previous_flag = self._applying_formats
        self._applying_formats = True
        cursor.beginEditBlock()
        try:
            cursor.select(QTextCursor.SelectionType.Document)
            cursor.setCharFormat(plain_format)
            for raw_index in sorted(set(self.chapter_raw_map.values())):
                block = document.findBlockByNumber(raw_index)
                if not block.isValid():
                    continue
                cursor.setPosition(block.position())
                cursor.setPosition(block.position() + block.length() - 1, QTextCursor.MoveMode.KeepAnchor)
                cursor.setCharFormat(title_format)
                # 標題的下一行通常是空行，空行沒有文字、格式是跟著前一行繼承的：
                # 不把它壓回一般字重，使用者在標題底下打字會打出一整行粗體。
                following = block.next()
                if following.isValid():
                    cursor.setPosition(following.position())
                    cursor.setBlockCharFormat(plain_format)
            self._hide_title_markers(cursor)
        finally:
            cursor.endEditBlock()
            self._applying_formats = previous_flag

    def _on_markers_toggled(self, shown: bool):
        """切換只影響顯示，一個字都不會動到。"""
        self._show_title_markers = shown
        self._refresh_title_formats()
        self._show_status("已顯示章節標記" if shown else "已隱藏章節標記")

    def _show_marker_help(self):
        """問號按鈕：說明四種標記，並在這裡設定匯出時是否移除。"""
        rows = "".join(
            f"<tr><td style='padding:2px 14px 2px 0'><code>{mark}</code></td>"
            f"<td style='padding:2px 14px 2px 0'>{i18n.T(name)}</td>"
            f"<td style='padding:2px 0'>{i18n.T(detail)}</td></tr>"
            for mark, name, detail in MARKER_GUIDE)
        box = QMessageBox(QMessageBox.Icon.Information, i18n.T("章節標記說明"), "", parent=self)
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(f"<b>{i18n.T('章節標記儲存於檔案中')}</b>")
        box.setInformativeText(
            f"<table>{rows}</table>"
            + "<p>" + i18n.T("標記用於保存目錄的手動調整，重新開啟檔案時據以還原。") + "</p>"
            + "<p>" + i18n.T("標記預設隱藏，可由「顯示章節標記」切換；以其他程式開啟時會直接顯示。"
                             "移除後，匯出的檔案將無法還原手動調整過的目錄。")
            + "</p>")
        # 匯出設定放在說明旁邊：要決定是否移除，正好需要先看懂上面那張表。
        # 這個勾選獨立記住，跟「顯示章節標記」無關。
        checkbox = QCheckBox(i18n.T("匯出時移除章節標記"))
        checkbox.setChecked(self._strip_markers_on_export)
        box.setCheckBox(checkbox)
        box.addButton(i18n.T("知道了"), QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        self._strip_markers_on_export = checkbox.isChecked()

    def _hide_title_markers(self, cursor: QTextCursor):
        """行尾的 [::]／[::X]／[::W]／[::T] 標記只給程式辨識用，閱讀時是雜訊：
        預設縮成 1px 並設成透明，畫面上等於看不到，但字還在文字裡——存檔、
        復原、章節辨識都照舊。

        按下「顯示章節標記」之後改成用強調色加淡底畫出來，一眼就看得出
        「這是工具加的，不是作者寫的」。

        先用 raw_lines 找出「哪幾行有標記」（純字串比對，很快），再只在那幾
        行上比對正則；原本是讓 QTextDocument 掃過整份文件，檔案愈大愈慢，
        而有標記的通常只有幾十行。"""
        hidden = QTextCharFormat()
        if self._show_title_markers:
            tokens = DARK if self.dark_mode else LIGHT
            hidden.setForeground(QColor(tokens.marker_text))
            hidden.setBackground(QColor(tokens.marker_bg))
        else:
            hidden.setForeground(QColor(0, 0, 0, 0))
            hidden.setProperty(QTextFormat.Property.FontPixelSize, 1)
        document = self.editor.document()
        for row, line in enumerate(self.raw_lines):
            if "[::" not in line:
                continue
            block = document.findBlockByNumber(row)
            if not block.isValid():
                continue
            match = _MARKER_REGEX.match(block.text())
            if not match.hasMatch():
                continue
            cursor.setPosition(block.position() + match.capturedStart())
            cursor.setPosition(block.position() + match.capturedEnd(), QTextCursor.MoveMode.KeepAnchor)
            # 顯示時用 merge：保留該行原本的字級與字重，標記才會跟著標題
            # 一起放大，不會變成擠在標題旁邊的一小截。
            if self._show_title_markers:
                cursor.mergeCharFormat(hidden)
            else:
                cursor.setCharFormat(hidden)

    def _on_tree_item_clicked(self, item, _column):
        if item in self.virtual_volume_items and item.childCount():
            item = item.child(0)   # 推定卷本身沒有標題行，跳到卷內第一章
        row = self.chapter_index_map.get(item)
        if not row:
            return
        if self._toc_text_version != self._text_version:
            # 目錄建好之後正文行數變過：換算成目前的行號，不然會跳到別章。
            self._sync_raw_lines()
            row = self._map_tree_line(row - 1) + 1
        block = self.editor.document().findBlockByNumber(row - 1)
        if not block.isValid():
            return
        cursor = self.editor.textCursor()
        cursor.setPosition(block.position())
        self.editor.setTextCursor(cursor)
        self.editor.centerCursor()
        self.editor.setFocus()

    def _apply_toc_label_mode(self):
        labels = self.toc_compact_labels if self.toc_compact_mode else self.toc_full_labels
        for item, label in labels.items():
            item.setText(0, label)

    @action
    def toggle_toc_compact(self):
        if not self.toc_full_labels:
            return
        self.toc_compact_mode = not self.toc_compact_mode
        self._apply_toc_label_mode()
        self._show_status("目錄目前只顯示章號" if self.toc_compact_mode else "目錄目前顯示完整標題")

    # ------------------------------------------------------------------
    # 缺章檢查
    # ------------------------------------------------------------------

    def _find_collection_missing_from_toc(self):
        """單本與合集共用已確認章節資料，作品間不互相延續編號。"""
        mode = self.chapter_panel.missing_mode()
        groups = group_formal_chapters(
            self.chapter_records, lambda node: node.parent(),
            lambda node: self.toc_full_labels.get(node, node.text(0)))
        results, previous = [], {}
        for group in groups:
            report = chapter_gap_report(group["numbers"], group["label"], mode, previous.get(group["work"]))
            report["nodes"] = sorted(
                ((self.chapter_raw_map.get(node, 0), int(self.chapter_records[node]["number"]), node)
                 for node in group["nodes"]), key=lambda entry: entry[0])
            results.append(report)
            previous[group["work"]] = report["last"]
        return results

    def _missing_problems_from_structure(self, result) -> list:
        """直接用 core 的結構結果算缺章，不需要先把目錄畫出來。"""
        tree = result.tree
        groups = group_formal_chapters(
            result.chapter_records, lambda node: tree.parent(node) or None,
            lambda node: tree.item(node, "text"))
        mode = self.chapter_panel.missing_mode()
        results, previous = [], {}
        for group in groups:
            report = chapter_gap_report(group["numbers"], group["label"], mode, previous.get(group["work"]))
            results.append(report)
            previous[group["work"]] = report["last"]
        return self._missing_chapter_problems(results)

    @staticmethod
    def _missing_chapter_problems(results) -> list:
        problems = []
        for result in results:
            details = []
            if result["missing_ranges"]:
                details.append("章號缺口 " + "、".join(
                    str(a) if a == b else f"{a}–{b}" for a, b in result["missing_ranges"]))
            if result["duplicates"]:
                details.append("重複 " + compact_number_ranges(result["duplicates"]))
            if details:
                prefix = "" if result["label"] == "全書" else f"{result['label']}："
                problems.append(prefix + "、".join(details))
        return problems

    @action
    def check_missing_chapters(self):
        """結果顯示在章節管理的結果區；之後目錄每次重建都會自動重算。"""
        if not self.editor.toPlainText().strip():
            return
        self._missing_report_active = True
        self._refresh_missing_report()

    @timed
    def _refresh_missing_report(self):
        if not self._missing_report_active:
            return
        results = self._find_collection_missing_from_toc()
        self._missing_groups = [result["nodes"] for result in results]
        self.chapter_panel.show_missing_report({
            "mode": self.chapter_panel.missing_mode(),
            "total": sum(result["count"] for result in results),
            "start_unverified": any(result["start_unverified"] for result in results),
            "groups": [{"label": result["label"], "missing": result["missing_ranges"],
                        "duplicates": result["duplicates"]} for result in results],
        })

    def _on_missing_report_closed(self):
        self._missing_report_active = False

    def _on_missing_report_link(self, link: str):
        """點結果裡的缺口：跳到缺口前最後一章；點重複：跳到第二次出現的那一章。"""
        try:
            group_index, number, kind = link.split("|")
            nodes = self._missing_groups[int(group_index)]
            number = int(number)
        except (ValueError, IndexError):
            return
        if kind == "dup":
            matches = [node for _row, value, node in nodes if value == number]
            target = matches[1] if len(matches) > 1 else (matches[0] if matches else None)
        else:
            before = [node for _row, value, node in nodes if value < number]
            target = before[-1] if before else (nodes[0][2] if nodes else None)
        if target is None:
            return
        self.tree.setCurrentItem(target)
        self.tree.scrollToItem(target)
        self._on_tree_item_clicked(target, 0)

    # ------------------------------------------------------------------
    # 側邊面板切換（格式選項／章節管理／內容清理）
    # ------------------------------------------------------------------

    def _set_active_side_panel(self, panel: QWidget | None):
        for widget in (self.options_panel, self.chapter_panel, self.find_bar):
            widget.setVisible(widget is panel)
        if panel is not self.find_bar:
            self.editor.setExtraSelections([])      # 關掉搜尋面板就把反白收掉
        self.format_toggle_button.setChecked(panel is self.options_panel)
        self.chapter_toggle_button.setChecked(panel is self.chapter_panel)
        self.side_card.setVisible(panel is not None)

    def _sync_side_panel_widths(self):
        """左側卡片裡的三個面板用同一個最小寬度（以最寬的那個為準）：
        不然切換「排版設定」「目錄功能」時，卡片寬度會跟著跳。
        字型與樣式表會影響寬度，所以每次套用主題後重算。"""
        panels = (self.options_panel, self.chapter_panel, self.find_bar)
        for panel in panels:
            panel.setMinimumWidth(0)
        width = max(panel.minimumSizeHint().width() for panel in panels)
        for panel in panels:
            panel.setMinimumWidth(width)

    def _toggle_side_panel(self, panel: QWidget):
        self._set_active_side_panel(None if panel.isVisible() else panel)

    # ------------------------------------------------------------------
    # 格式選項／一鍵排版
    # ------------------------------------------------------------------

    @action
    def apply_formatting(self):
        if not self.raw_lines:
            return
        self._sync_raw_lines()
        options = self.options_panel.current_options(self.structure_mode)
        self._apply_format_options(options, "已套用格式，可以按 Ctrl+Z 復原")

    @action
    def one_click_format(self):
        """一鍵排版：不管面板目前勾了什麼，直接套用一組固定的常用組合——
        刪除所有空行、章節插入空行、增加縮排、標題前後空行、編號與標題間隔
        用半形空格。刻意不含「合併下行標題」：那項會在章節只有編號、沒有
        標題文字時，把下一個「看起來像標題」的行併進來，屬於啟發式判斷，
        誤判機率較高，需要時由使用者自己在格式選項手動勾選。

        排版前先檢查缺章與高信心廣告：排版會重排整份文字，事後比較難回頭
        確認原本的問題，所以有狀況時先問過再動手。"""
        if not self.editor.toPlainText().strip():
            return
        self._sync_raw_lines()
        options = FormatOptions(
            remove_extra_empty=True,
            add_empty=True,
            auto_indent=True,
            format_title=True,
            sep_style="半形空格",
            structure=self.structure_mode,
        )
        # 排版前的檢查直接用排版那一次的辨識結果，不另外再建一次結構、也不
        # 重畫目錄（原本一次一鍵排版要建三次，大檔每次都要好幾秒）。
        applied = self._apply_format_options(
            options, "一鍵排版已套用，可以按 Ctrl+Z 復原",
            confirm=self._confirm_one_click_warnings)
        if not applied:
            self._show_status("已取消一鍵排版")
            return
        self.options_panel.reset_to_defaults()

    def _confirm_one_click_warnings(self, result) -> bool:
        """有缺章或高信心廣告時彈窗確認；沒有狀況就直接放行。

        result 是排版那一次的辨識結果，直接拿來算缺章。"""
        warnings = []
        problems = self._missing_problems_from_structure(result)
        if problems:
            warnings.append("缺章：" + "；".join(problems))
        ads = self._high_confidence_ads()
        if ads:
            warnings.append(f"高信心廣告：{len(ads)} 處（可先到「排版設定 → 掃描廣告」刪除）")
        if not warnings:
            return True
        return dialogs.confirm(
            self, "一鍵排版前確認",
            "排版前發現以下狀況：\n\n" + "\n".join(f"• {w}" for w in warnings)
            + "\n\n仍要繼續一鍵排版嗎？")

    def _high_confidence_ads(self) -> list:
        """高信心廣告候選；同一份文字只掃一次（掃描本身在大檔要好幾秒）。"""
        if self._ad_scan_version != self._text_version:
            self._ad_scan_cache = [candidate for candidate in scan_ad_candidates(self.raw_lines)
                                   if candidate["confidence"] == "高"]
            self._ad_scan_version = self._text_version
        return self._ad_scan_cache

    @timed
    def _apply_format_options(self, options: FormatOptions, status_message: str, confirm=None) -> bool:
        """套用格式；confirm 會拿到這一次的辨識結果，回傳 False 就整個取消。"""
        self.format_options = options
        ctx = self._build_context()
        result = build_document_structure(ctx, apply_format=True, write_text=True)
        if confirm is not None and not confirm(result):
            return False

        generated = "\n".join(result.processed_render_lines)
        auto_titles = {
            result.chapter_index_map[node] - 1: dict(record)
            for node, record in result.chapter_records.items()
        }
        for attribute in ("ignored_chapters", "force_lv1_chapters", "force_lv2_chapters"):
            old = getattr(self, attribute)
            setattr(self, attribute, {
                result.chapter_index_map[node] - 1
                for node, row in result.chapter_raw_map.items() if row in old
            })
        self.auto_titles = auto_titles
        moved_titles = {row: result.chapter_index_map[node] - 1 for node, row in result.chapter_raw_map.items()}
        self._pending_line_maps.append(_chapter_line_mapper(moved_titles))
        self.raw_lines = generated.split("\n")

        self._set_editor_text(generated)
        self._mark_synced(generated)
        # 排版後的行號跟排版前完全不同：不能直接拿排版結果畫目錄（那份結果
        # 的原始行號指的是排版「前」的文字，標題粗體會落在錯的行），要用
        # 新文字重新辨識一次。
        self._rebuild_toc()
        self._checkpoint_document()
        self._show_status(status_message)
        return True

    @action
    def format_selected_chapters(self):
        """只對目錄選取的章節套用目前的格式選項。

        每一段自己跑一次 build_document_structure，再把結果接回原文；段落
        邊界一律切在章節標題上，所以不會排版到沒選的章。行號對照用每個
        章節標題「從哪一行搬到哪一行」精確算出來，不靠 difflib 猜，
        強制層級與忽略標記才不會跑掉（跟剪下／貼上同一套作法）。"""
        if not self.raw_lines:
            return
        self._sync_raw_lines()
        self._ensure_toc_current()
        spans = self._selected_section_spans()
        if not spans:
            dialogs.info(self, "尚未選取章節", "請先在目錄選取要排版的章節。")
            return
        options = self.options_panel.current_options(self.structure_mode)
        items = describe_options(options)
        if not items:
            dialogs.info(self, "沒有勾選任何項目",
                         "目前的格式選項沒有勾選任何項目，先到「排版設定」勾選要套用的內容。")
            return
        chapter_count = len(self._selected_toc_items())
        lines_count = sum(end - start for start, end in spans)
        if not dialogs.confirm(
            self, "排版選取章節",
            f"將對 {chapter_count} 個章節（共 {lines_count} 行）套用：\n\n"
            + "\n".join(f"• {item}" for item in items)
            + "\n\n沒有選到的章節維持原樣，所以整本書可能看起來不一致"
              "（章節編號樣式、編號與標題間隔這類設定尤其明顯）。\n"
              "此操作算一步，可以用「上一步」完整復原。\n\n是否繼續？",
        ):
            return

        self.format_options = options
        new_lines, moved_titles = self._format_spans(spans, options)
        generated = "\n".join(new_lines)
        self._pending_line_maps.append(_chapter_line_mapper(moved_titles))
        self._remap_chapter_state(moved_titles)
        self.raw_lines = list(new_lines)
        self._set_editor_text(generated)
        self._mark_synced(generated)
        self._rebuild_toc()
        self._checkpoint_document()
        self._show_status(f"已排版 {chapter_count} 個章節，可以按 Ctrl+Z 復原")

    def _format_spans(self, spans, options):
        """逐段排版並接回原文，回傳（新的整份行, 章節標題的新舊行號對照）。"""
        new_lines: list = []
        moved_titles: dict = {}
        cursor_row = 0
        # 先算好一次：它是每次呼叫都重建的集合，放進迴圈裡等於每一行都掃一遍
        # 全部章節（72,000 行、3,000 章實測只排一章就要 5 秒）。
        title_rows = self._title_rows
        for start, end in spans:
            # 沒選到的部分原樣搬過去，順便記下這段裡的章節標題位移。
            for old_row in range(cursor_row, start):
                if old_row in title_rows:
                    moved_titles[old_row] = len(new_lines) + (old_row - cursor_row)
            new_lines.extend(self.raw_lines[cursor_row:start])

            segment_start = len(new_lines)
            result = build_document_structure(
                self._segment_context(start, end, options), apply_format=True, write_text=True)
            new_lines.extend(result.processed_render_lines)
            for node, local_old in result.chapter_raw_map.items():
                moved_titles[start + local_old] = segment_start + result.chapter_index_map[node] - 1
            cursor_row = end

        for old_row in range(cursor_row, len(self.raw_lines)):
            if old_row in title_rows:
                moved_titles[old_row] = len(new_lines) + (old_row - cursor_row)
        new_lines.extend(self.raw_lines[cursor_row:])
        return new_lines, moved_titles

    def _segment_context(self, start: int, end: int, options) -> BuildContext:
        """把整份文件的章節狀態裁成這一段的區域座標。"""
        def shift(rows):
            return {row - start for row in rows if start <= row < end}

        return BuildContext(
            raw_lines=self.raw_lines[start:end],
            options=options,
            user_chapter_rules=self.user_chapter_rules,
            auto_titles={row - start: record for row, record in self.auto_titles.items()
                         if start <= row < end},
            force_lv1_chapters=shift(self.force_lv1_chapters),
            force_lv2_chapters=shift(self.force_lv2_chapters),
            ignored_chapters=shift(self.ignored_chapters),
            invalid_tail_regex=build_invalid_tail_regex(self.allowed_tail_chars),
        )

    @property
    def _title_rows(self) -> set:
        """目前所有章節標題所在的行（0 起算）。"""
        return {row - 1 for row in self.chapter_index_map.values()}

    def _remap_chapter_state(self, moved_titles: dict):
        """排版後行號全變了，把以行號為鍵的章節狀態搬到新行號。"""
        for attribute in ("ignored_chapters", "force_lv1_chapters", "force_lv2_chapters"):
            old = getattr(self, attribute)
            setattr(self, attribute, {moved_titles[row] for row in old if row in moved_titles})
        self.auto_titles = {moved_titles[row]: record
                            for row, record in self.auto_titles.items() if row in moved_titles}

    # ------------------------------------------------------------------
    # 尋找／取代
    # ------------------------------------------------------------------

    def _on_escape(self):
        """Esc：先取消剪下狀態，沒有的話才收起尋找面板。"""
        if self._cut_state is not None:
            self.cancel_cut()
            return
        self.close_find_bar()

    def toggle_find_bar(self):
        if not self.raw_lines:
            return
        if self.find_bar.isVisible():
            self.close_find_bar()
        else:
            self._set_active_side_panel(self.find_bar)
            self.find_bar.refresh()
            self.find_bar.focus_input()

    def close_find_bar(self):
        if self.find_bar.isVisible():
            self._set_active_side_panel(None)
        self.editor.setExtraSelections([])

    # 尋找列算出來的是 Python 字元位置，游標吃的是 Qt（UTF-16）位置：
    # 本文只要出現過一個 emoji 或擴充漢字，後面每個位置就會差一格，取代會
    # 改到前一個字（審查報告 C-04）。所有進出游標的位置都經過這裡換算。

    def _find_on_select(self, start: int, end: int):
        positions = self._positions()
        cursor = self.editor.textCursor()
        cursor.setPosition(positions.to_qt(start))
        cursor.setPosition(positions.to_qt(end), QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()

    def _find_on_replace_one(self, start: int, end: int, new_text: str):
        positions = self._positions()
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        cursor.setPosition(positions.to_qt(start))
        cursor.setPosition(positions.to_qt(end), QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(new_text)
        cursor.endEditBlock()

    @action
    def _find_on_replace_all_text(self, new_content: str, count: int):
        """全部取代直接換掉整份文字：結果清單有筆數上限，逐筆套用會變成
        「只取代前面幾筆」，而且上萬次游標操作也慢。"""
        cursor_position = self.editor.textCursor().position()
        self._set_editor_text(new_content)
        cursor = self.editor.textCursor()
        cursor.setPosition(min(cursor_position, self.editor.document().characterCount() - 1))
        self.editor.setTextCursor(cursor)
        self._sync_raw_lines()
        self._rebuild_toc()
        self._checkpoint_document()
        self._show_status(i18n.T(f"已取代 {count} 處，可以按 Ctrl+Z 復原"))

    def _find_on_matches_changed(self, spans: list[tuple[int, int]], current_index: int):
        tokens = DARK if self.dark_mode else LIGHT
        document = self.editor.document()
        positions = self._positions()
        # 命中太多時只畫目前這一筆：每一筆都是一個 ExtraSelection，上萬筆
        # 會讓每次重繪都很慢（審查報告 C-10）。
        if len(spans) > MAX_HIGHLIGHT_SPANS:
            spans = [spans[current_index]] if 0 <= current_index < len(spans) else []
            current_index = 0
        selections = []
        for index, (start, end) in enumerate(spans):
            selection = QTextEdit.ExtraSelection()
            cursor = QTextCursor(document)
            cursor.setPosition(positions.to_qt(start))
            cursor.setPosition(positions.to_qt(end), QTextCursor.MoveMode.KeepAnchor)
            selection.cursor = cursor
            char_format = selection.format
            char_format.setBackground(QColor(
                tokens.find_current_bg if index == current_index else tokens.find_match_bg))
            char_format.setForeground(QColor(tokens.text))
            selection.format = char_format
            selections.append(selection)
        self.editor.setExtraSelections(selections)

    # ------------------------------------------------------------------
    # 插入章節標題
    # ------------------------------------------------------------------

    @action
    def open_insert_title_dialog(self):
        if not self.editor.toPlainText().strip():
            return
        insert_index = self.editor.textCursor().blockNumber()
        self._sync_raw_lines()
        insert_index = min(insert_index, len(self.raw_lines))
        recognized_indices = sorted(set(self.chapter_raw_map.values()))
        suggestions, default_kind = get_insert_suggestions(self.raw_lines, recognized_indices, insert_index)

        dialog = InsertTitleDialog(suggestions, default_kind, self)
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            generated = dialog.result_text
        finally:
            dialog.deleteLater()
        if not accepted:
            return

        lines = self.raw_lines
        previous_is_blank = insert_index == 0 or not lines[insert_index - 1].strip()
        current_is_blank = insert_index >= len(lines) or not lines[insert_index].strip()
        leading = "" if previous_is_blank else "\n"
        trailing = "\n" if current_is_blank else "\n\n"
        generated_line = insert_index + (0 if previous_is_blank else 1)

        block = self.editor.document().findBlockByNumber(insert_index)
        position = block.position() if block.isValid() else len(self.editor.toPlainText())
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        cursor.setPosition(position)
        cursor.insertText(leading + generated + trailing)
        cursor.endEditBlock()

        self._sync_raw_lines()
        self._rebuild_toc()
        for item, raw_index in self.chapter_raw_map.items():
            if raw_index == generated_line:
                self.tree.setCurrentItem(item)
                self._on_tree_item_clicked(item, 0)
                break
        self._checkpoint_document()
        self._show_status(f"已插入：{generated}")

    # ------------------------------------------------------------------
    # 廣告掃描
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 工具對話框（非模式）
    # ------------------------------------------------------------------

    def _open_tool_dialog(self, key: str, create, reload, on_closed=None):
        """開一個「開著也能編輯本文」的工具對話框。

        以前這些對話框是模式的：看到要手動改的地方，得先關掉對話框、改完再
        打開；想從本文複製一行當範例也一樣。改成非模式之後，本文隨時可以改，
        所以對話框手上的本文快照可能過期——每次切回對話框時（見
        _ToolDialogWatcher）比對文字版本，改過就呼叫 reload 用新的本文重算。

        create()：建立對話框；reload(dialog)：用目前的本文重算；
        on_closed(dialog, accepted)：關閉時要做的事（記住設定、套用結果）。"""
        existing = self._tool_dialogs.get(key)
        if existing is not None:
            existing.showNormal()
            existing.raise_()
            existing.activateWindow()
            return None
        dialog = create()
        dialog._tool_version = self._text_version
        dialog._tool_reload = reload
        dialog.installEventFilter(self._tool_dialog_watcher)
        dialog.finished.connect(
            lambda result, d=dialog: self._on_tool_dialog_finished(key, d, result, on_closed))
        # 在表格上點兩下：跳到本文那一行，並把焦點交給本文，可以直接改。
        for table in dialog.findChildren(QTableWidget):
            table.doubleClicked.connect(self._focus_editor_from_tool)
        self._tool_dialogs[key] = dialog
        dialog.show()
        return dialog

    def _focus_editor_from_tool(self, *_args):
        self.activateWindow()
        self.raise_()
        self.editor.setFocus()

    def _refresh_tool_dialog(self, dialog) -> bool:
        """本文在對話框上次分析之後改過，就讓它重算；有重算回傳 True。"""
        if getattr(dialog, "_tool_version", None) is None or dialog._tool_version == self._text_version:
            return False
        self._sync_raw_lines()
        if self.raw_lines and any(line.strip() for line in self.raw_lines):
            self._ensure_toc_current()
        dialog._tool_reload(dialog)
        dialog._tool_version = self._text_version
        return True

    def _on_tool_dialog_finished(self, key, dialog, result, on_closed):
        self._tool_dialogs.pop(key, None)
        self.editor.setExtraSelections([])
        try:
            if on_closed is not None:
                on_closed(dialog, result == QDialog.DialogCode.Accepted)
        finally:
            # 對話框留著整份 raw_lines 與整張表格；明確釋放，不然一直開一直
            # 累積到主視窗關閉為止（審查報告 C-16）。
            dialog.deleteLater()

    def _replace_text_from_tool(self, lines: list):
        """工具對話框改完的整份本文：拆行、接行、刪行會改變行數，交給
        _sync_raw_lines 比對新舊兩版搬章節狀態。"""
        self._set_editor_text("\n".join(lines))
        self._sync_raw_lines()
        self._rebuild_toc()
        self._checkpoint_document()

    def _tool_scope(self):
        spans = self._selected_section_spans()
        return spans, len(self._selected_toc_items()) if spans else 0

    # ------------------------------------------------------------------
    # 廣告掃描
    # ------------------------------------------------------------------

    @action
    def open_ad_scan_dialog(self):
        if not self.editor.toPlainText().strip():
            return
        self._sync_raw_lines()
        self._ensure_toc_current()
        spans = self._selected_section_spans()
        saved = self._ui_state.get("ad_categories")

        def create():
            dialog = AdScanDialog(self.raw_lines, self, selected_ranges=spans,
                                  selected_count=len(self._selected_toc_items()),
                                  enabled_categories=set(saved) if isinstance(saved, list) else None)
            dialog.candidateHighlighted.connect(self._highlight_ad_candidate)
            dialog.deletionReady.connect(lambda lines, d=dialog: self._apply_ad_deletion(d, lines))
            return dialog

        def reload(dialog):
            ranges, count = self._tool_scope()
            dialog.reload(self.raw_lines, ranges, count)

        def on_closed(dialog, _accepted):
            self._ui_state["ad_categories"] = sorted(dialog.enabled_categories())

        self._open_tool_dialog("ad_scan", create, reload, on_closed)

    @action
    def _apply_ad_deletion(self, dialog, lines: list):
        if self._refresh_tool_dialog(dialog):
            dialogs.info(dialog, "本文已修改", "本文在掃描之後改過了，已經重新掃描，請確認勾選的項目後再按一次。")
            return
        removed = len(self.raw_lines) - len(lines)
        self.editor.setExtraSelections([])
        self._replace_text_from_tool(lines)
        self._refresh_tool_dialog(dialog)
        self._show_status(f"已刪除廣告候選 {removed} 行，可以按 Ctrl+Z 復原")

    def _highlight_ad_candidate(self, start_line: int, end_line: int):
        document = self.editor.document()
        start_block = document.findBlockByNumber(start_line)
        end_block = document.findBlockByNumber(end_line)
        if not start_block.isValid() or not end_block.isValid():
            return
        highlight_cursor = QTextCursor(document)
        highlight_cursor.setPosition(start_block.position())
        highlight_cursor.setPosition(end_block.position() + end_block.length() - 1,
                                     QTextCursor.MoveMode.KeepAnchor)
        tokens = DARK if self.dark_mode else LIGHT
        selection = QTextEdit.ExtraSelection()
        selection.cursor = highlight_cursor
        # 跟文字選取、搜尋目前這一筆同一種橘色，不要另外一種藍紫色。
        char_format = selection.format
        char_format.setBackground(QColor(tokens.find_current_bg))
        char_format.setForeground(QColor(tokens.text))
        selection.format = char_format
        self.editor.setExtraSelections([selection])

        jump_cursor = self.editor.textCursor()
        jump_cursor.setPosition(start_block.position())
        self.editor.setTextCursor(jump_cursor)
        self.editor.centerCursor()

    @action
    def open_quote_check_dialog(self):
        """引號與標點檢查；勾選的項目可以自動修正，其餘開著對話框直接在本文改。"""
        if not self.editor.toPlainText().strip():
            return
        self._sync_raw_lines()
        self._ensure_toc_current()
        spans = self._selected_section_spans()
        saved = self._ui_state.get("quote_kinds")

        def create():
            dialog = QuoteCheckDialog(self.raw_lines, self, selected_ranges=spans,
                                      selected_count=len(self._selected_toc_items()),
                                      enabled_kinds=set(saved) if isinstance(saved, list) else None)
            dialog.problemSelected.connect(self._jump_to_line)
            dialog.fixesReady.connect(lambda lines, count, d=dialog: self._apply_quote_fixes(d, lines, count))
            return dialog

        def reload(dialog):
            ranges, count = self._tool_scope()
            dialog.reload(self.raw_lines, ranges, count)

        def on_closed(dialog, _accepted):
            self._ui_state["quote_kinds"] = sorted(dialog.enabled_kinds())

        self._open_tool_dialog("quote_check", create, reload, on_closed)

    @action
    def _apply_quote_fixes(self, dialog, lines: list, count: int):
        if self._refresh_tool_dialog(dialog):
            dialogs.info(dialog, "本文已修改", "本文在檢查之後改過了，已經重新檢查，請確認勾選的項目後再按一次。")
            return
        self.editor.setExtraSelections([])
        self._replace_text_from_tool(lines)
        # 對話框不關：用修正後的本文重新檢查，剩下要手動處理的繼續列著。
        self._refresh_tool_dialog(dialog)
        self._show_status(f"已修正 {count} 處引號與標點問題，可以按 Ctrl+Z 復原")

    @action
    def open_script_convert_dialog(self, prefer_selected: bool = False):
        """全文（或選取章節）繁簡轉換。

        prefer_selected：從目錄右鍵開的，就算只選一章也預設只轉選取的章節。"""
        if not self.editor.toPlainText().strip():
            return
        if not opencc_available():
            dialogs.info(self, "需要 OpenCC",
                         "繁簡轉換需要 OpenCC 套件，目前的執行環境沒有安裝。\n\n"
                         "安裝指令：pip install opencc-python-reimplemented")
            return
        self._sync_raw_lines()
        self._ensure_toc_current()
        spans = self._selected_section_spans()
        dialog = ScriptConvertDialog(self, selected_count=len(self._selected_toc_items()) if spans else 0,
                                     prefer_selected=prefer_selected,
                                     mode=self._ui_state.get("script_mode"),
                                     convert_metadata=bool(self._ui_state.get("script_metadata", True)))
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            mode = dialog.mode()
            selected_only = dialog.selected_only()
            with_metadata = dialog.convert_metadata()
            self._ui_state["script_mode"] = mode
            self._ui_state["script_metadata"] = with_metadata
        finally:
            dialog.deleteLater()

        lines = list(self.raw_lines)
        if selected_only and spans:
            rows = [row for start, end in spans for row in range(start, min(end, len(lines)))]
            scope_text = f"{len(self._selected_toc_items())} 個章節"
        else:
            rows = range(len(lines))
            scope_text = "全文"
        generated = "\n".join(self._convert_lines_with_progress(lines, rows, mode))

        if with_metadata:
            self.metadata_bar.title_input.setText(
                convert_body_text(self.metadata_bar.book_title(), mode))
            self.metadata_bar.author_input.setText(
                convert_body_text(self.metadata_bar.author(), mode))

        # 逐字轉換，行數與行的順序都沒變，所以 raw_lines 直接換掉就好，
        # 不用跑 _adopt_lines 的 difflib 比對，章節狀態的行號也原封不動。
        self.raw_lines = generated.split("\n")
        self._set_editor_text(generated)
        self._mark_synced(generated)
        # 章節標題的字變了，目錄要重新辨識。
        self._rebuild_toc()
        self._checkpoint_document()
        self._show_status(f"已將{scope_text}做「{mode}」轉換，可以按 Ctrl+Z 復原")

    def _convert_lines_with_progress(self, lines: list, rows, mode: str) -> list:
        """逐塊做繁簡轉換，中間更新狀態列。

        OpenCC 是逐字轉換，2.6 MB 實測要 6 秒、5.8 MB 的檔案十幾秒；
        一口氣轉完的話視窗整個沒反應，看起來就像當掉（卡死監看也會記一筆）。
        分塊之間讓 Qt 重畫一次，並在轉換期間停用視窗，避免中途又按了別的功能。

        OpenCC 不會增減換行，所以把一塊接起來轉、再切回去，行數仍然 1:1。
        """
        rows = list(rows)
        total = len(rows)
        chunk_size = 2000
        self.setEnabled(False)
        self._long_task_running = True
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for index in range(0, total, chunk_size):
                block = rows[index:index + chunk_size]
                converted = convert_body_text("\n".join(lines[row] for row in block), mode)
                for row, text in zip(block, converted.split("\n")):
                    lines[row] = text
                if total > chunk_size:
                    self._show_status(
                        i18n.T("繁簡轉換中…") + f" {min(index + chunk_size, total)} / {total}",
                        translated=True)
                    QApplication.processEvents()
        finally:
            QApplication.restoreOverrideCursor()
            self._long_task_running = False
            self.setEnabled(True)
        return lines

    def _jump_to_line(self, line_number: int):
        """跳到某一行並整行反白（檢查清單點選用，1 起算）。"""
        self._highlight_ad_candidate(line_number - 1, line_number - 1)

    # ------------------------------------------------------------------
    # 自訂章節規則
    # ------------------------------------------------------------------

    @action
    def open_rules_dialog(self):
        """自訂章節規則，含「本文可疑章節」（原本獨立的「檢查未辨識章節」）。
        非模式：開著時可以從本文複製一行貼到「從範例產生」。"""
        self._sync_raw_lines()
        # 目錄在打字之後可能還沒重建，行號會對不上：已經是章節的行被當成
        # 「可疑章節」再列一次，或真正沒辨識到的反而被跳過。
        if self.raw_lines and any(line.strip() for line in self.raw_lines):
            self._ensure_toc_current()

        def create():
            dialog = RulesDialog(self.user_chapter_rules, lambda: list(self.raw_lines), self,
                                 known_rows=set(self.chapter_raw_map.values()),
                                 presets_expanded=bool(self._ui_state.get("rules_presets_expanded", True)))
            dialog.candidateHighlighted.connect(self._highlight_ad_candidate)
            return dialog

        def reload(dialog):
            dialog.reload(self.raw_lines, set(self.chapter_raw_map.values()))

        self._open_tool_dialog("rules", create, reload, self._on_rules_dialog_closed)

    @action
    def _on_rules_dialog_closed(self, dialog, accepted: bool):
        self._ui_state["rules_presets_expanded"] = dialog.presets_expanded()
        result_rules = dialog.result_rules
        result_lines = dialog.result_lines
        volume_rows = set(dialog.result_volume_rows)
        if not accepted or result_rules is None:
            return
        if result_lines is not None and dialog._tool_version != self._text_version:
            # 按下按鈕前本文又改了（理論上切回對話框時就會重算，這裡保險）：
            # 勾選的行號已經對不上，只存規則，不動本文。
            result_lines = None
            self._show_status("本文在勾選之後改過了，只保存規則；要加入的行請重新勾選")
        added = 0
        if result_lines is not None:
            # 只在行尾加 [::]，行數不變，章節狀態的行號也不用搬。
            added = sum(1 for old, new in zip(self.raw_lines, result_lines) if old != new)
            generated = "\n".join(result_lines)
            self.raw_lines = list(result_lines)
            self._set_editor_text(generated)
            self._mark_synced(generated)
            # 卷級格式逐行加入時要設成卷；[::] 本身只代表「這一行是標題」。
            self.force_lv1_chapters |= volume_rows
            self.force_lv2_chapters -= volume_rows
        self.user_chapter_rules = result_rules
        self.rescan_toc()
        if result_lines is not None:
            self._checkpoint_document()
            self._show_status(f"已把 {added} 行加入目錄，並保存 {len(self.user_chapter_rules)} 條自訂章節規則")
        elif dialog._tool_version == self._text_version:
            self._show_status(f"已保存 {len(self.user_chapter_rules)} 條自訂章節規則")

    # ------------------------------------------------------------------
    # 目錄右鍵選單：整理／合併／連續編號／設層級／忽略／刪除
    # ------------------------------------------------------------------

    def _sync_raw_lines(self):
        """把編輯器目前的內容設為新的工作版本（raw_lines）。

        同一個版本只做一次：比對整份文字並不便宜，而點目錄、右鍵操作可能
        連續呼叫好幾次。"""
        if self._synced_text_version == self._text_version:
            return
        # 留著這份字串：快照要用同一份，不必再把整份文字複製一次。
        text = self.editor.toPlainText()
        self._adopt_lines(text.split("\n"))
        self._mark_synced(text)

    def _mark_synced(self, text: str | None):
        """記下「raw_lines 對應的是哪一版文字」，以及那份文字本身。

        text 傳 None 代表「已經同步，但手上沒有現成的字串」，之後要用的時候
        再跟編輯器要一次——不能把舊的字串留著，快照會存到錯的內容。"""
        self._synced_text = text
        self._synced_text_version = self._text_version

    @timed
    def _adopt_lines(self, new_lines: list, *, remap_state: bool = True):
        """換成新的 raw_lines，並把所有「以行號為 key」的章節狀態搬到新行號。

        忽略／強制層級集合、自動標題快取，全都是記「第幾行」。正文只要多一行
        或少一行（手動編輯、刪廣告、插入標題、合併章節…），舊行號就會指到
        別的行：本來是正文的行被當成標題、真正的標題反而消失，而且重新掃描
        也救不回來，因為重掃用的還是這些過期行號。所以每次接手新內容都先
        比對新舊兩版，把行號搬過去。

        remap_state=False 只記錄行號位移（重畫目錄時對回原本的選取用），不動
        任何章節狀態：復原／重做時這些狀態會直接用快照整組覆蓋。
        """
        old_lines = self.raw_lines
        self.raw_lines = new_lines
        self._synced_text = None
        self._synced_text_version = self._text_version
        if new_lines == old_lines:
            return
        # 預設的 autojunk 不能關：小說大量空行會讓比對退化成 O(N²)（實測過
        # 20,000 行關掉要 13.7 秒，開著 150,000 行只要 0.13 秒）。
        opcodes = difflib.SequenceMatcher(None, old_lines, new_lines).get_opcodes()
        mapping = {}
        for tag, a, b, c, d in opcodes:
            # 整段改寫即使行數相同，也不能假定標題還在同一行；只有單行改寫才對應。
            if tag == "equal":
                mapping.update((a + k, c + k) for k in range(b - a))
            elif tag == "replace" and b - a == d - c:
                # 等長改寫：只有「內容完全一樣」的行才對應得起來。重複行多的
                # 文件（大量空行、重複台詞）只改一行也會被整段判成 replace，
                # 中間沒動過的章節標記就這樣被丟掉（審查報告 C-12）。
                mapping.update((a + k, c + k) for k in range(b - a)
                               if old_lines[a + k] == new_lines[c + k])
        self._pending_line_maps.append(_diff_line_mapper(opcodes))
        if not remap_state:
            return
        for attribute in ("ignored_chapters", "force_lv1_chapters", "force_lv2_chapters"):
            setattr(self, attribute, {mapping[i] for i in getattr(self, attribute) if i in mapping})
        self.auto_titles = {mapping[i]: value for i, value in self.auto_titles.items()
                            if i in mapping and old_lines[i] == new_lines[mapping[i]]}

    def _map_tree_line(self, index: int) -> int:
        for map_line in self._pending_line_maps:
            index = map_line(index)
        return index

    def _replace_line(self, cursor: QTextCursor, raw_idx: int, new_text: str):
        """把第 raw_idx 行（0-based）整行內容換成 new_text；呼叫端負責用同一個
        cursor 把多次替換包進同一個 beginEditBlock/endEditBlock，變成一次復原。"""
        block = self.editor.document().findBlockByNumber(raw_idx)
        if not block.isValid():
            return
        cursor.setPosition(block.position())
        # 用 block.length()-1 而不是 len(block.text())：長度是 Qt 的單位，
        # 行裡有 emoji 或擴充漢字時兩者不一樣，行尾會切在字的中間。
        cursor.setPosition(block.position() + block.length() - 1, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(new_text)

    def _chapter_nodes_share_unit(self, nodes: list) -> bool:
        """判斷這些節點是不是同一種章節類型（例如都是「第…章」，
        不能混到「第…集」）。任一節點判斷不出類型，就保守視為不一致。"""
        if len(nodes) < 2:
            return True
        signatures = set()
        for node in nodes:
            raw_idx = self.chapter_raw_map.get(node)
            if raw_idx is None or raw_idx >= len(self.raw_lines):
                return False
            clean, _marker = strip_persistent_title_marker(self.raw_lines[raw_idx].strip())
            signature = chapter_unit_signature(clean)
            if signature is None:
                return False
            signatures.add(signature)
        return len(signatures) == 1

    def _build_toc_context_menu(self, pos):
        """只負責組出選單，不呼叫 exec()——方便測試時不用真的彈出視窗。

        只放「現在按得下去、而且有意義」的項目：灰掉的項目只會讓人猜為什麼
        不能按。剪下之後就不再顯示「剪下」，改成貼上與取消；已經是章的不顯示
        「設為章標題」，卷也一樣。"""
        item = self.tree.itemAt(pos)
        if item is None:
            return None
        if item not in self.tree.selectedItems():
            self.tree.setCurrentItem(item)
        virtual = [node for node in self.tree.selectedItems() if node in self.virtual_volume_items]
        if virtual and len(virtual) == len(self.tree.selectedItems()):
            return self._build_virtual_volume_menu(virtual)
        selected = self._selected_toc_items()
        count = len(selected)
        many = count > 1

        groups = [[("選取這 %d 個章節的全部內容" % count if many else "選取章節全部內容",
                    self.select_chapter_text)]]

        pending = self._cut_state
        if pending is None:
            groups.append([(f"剪下這 {count} 個章節" if many else "剪下章節", self.cut_selected_chapters)])
        else:
            group = []
            if self._paste_target(item) is not None:
                group.append((f"貼到此章之前（{pending['summary']}）",
                              lambda: self.paste_cut_chapters(item, before=True)))
                group.append((f"貼到此章之後（{pending['summary']}）",
                              lambda: self.paste_cut_chapters(item, before=False)))
            group.append(("取消剪下（Esc）", self.cancel_cut))
            groups.append(group)

        group = [
            (f"整理選取的 {count} 個章節（換行與空白）" if many else "整理換行與空白",
             self.repair_selected_chapters),
            (f"套用格式選項到選取的 {count} 個章節" if many else "套用格式選項到這章",
             self.format_selected_chapters),
            (f"套用繁簡轉換到選取的 {count} 個章節" if many else "套用繁簡轉換到這章",
             lambda: self.open_script_convert_dialog(prefer_selected=True)),
        ]
        if many:
            group.append((f"合併選取的 {count} 個章節", self.merge_selected_chapters))
        chapter_nodes = [node for node in selected
                         if self.chapter_records.get(node, {}).get("kind") in ("chapter", "volume")]
        if len(chapter_nodes) > 1 and self._chapter_nodes_share_unit(chapter_nodes):
            group.append((f"連續編號（{len(chapter_nodes)} 個章節）", self.renumber_selected_chapters))
        groups.append(group)

        kinds = {self.chapter_records.get(node, {}).get("kind") for node in selected}
        group = []
        if kinds - {"volume"}:
            group.append(("設為卷標題", lambda: self.set_chapter_level(1)))
        if kinds - {"chapter"}:
            group.append(("設為章標題", lambda: self.set_chapter_level(2)))
        groups.append(group)

        groups.append([("標註為非章節（保留正文，從目錄移除）", self.ignore_selected_chapter),
                       ("刪除選取章節（含正文）", self.delete_selected_chapter)])

        menu = QMenu(self)
        for group in groups:
            if not group:
                continue
            if menu.actions():
                menu.addSeparator()
            for text, slot in group:
                menu.addAction(text).triggered.connect(slot)
        return menu

    def _build_virtual_volume_menu(self, virtual: list):
        menu = QMenu(self)
        names = "、".join(self.virtual_volume_items[node]["title"] for node in virtual[:3])
        write_action = menu.addAction(f"寫入卷標題「{names}」" if len(virtual) <= 3 else f"寫入 {len(virtual)} 個卷標題")
        write_action.triggered.connect(lambda: self.write_virtual_volumes(virtual))
        if len(self.virtual_volume_items) > len(virtual):
            all_action = menu.addAction(f"寫入全部推定卷標題（{len(self.virtual_volume_items)} 個）")
            all_action.triggered.connect(lambda: self.write_virtual_volumes(list(self.virtual_volume_items)))
        return menu

    def _selected_toc_items(self) -> list:
        """目前選取的目錄項目；推定卷沒有對應的標題行，換成它底下的章節。"""
        items = []
        for item in self.tree.selectedItems():
            if item in self.virtual_volume_items:
                items.extend(item.child(index) for index in range(item.childCount()))
            else:
                items.append(item)
        return list(dict.fromkeys(items))

    @action
    def write_virtual_volumes(self, items: list):
        """把推定卷的標題實際寫進本文：插在卷內第一個項目的前面，前後各留
        一個空行。寫入後它就是一般的卷標題，排版、匯出都會帶著它。"""
        self._sync_raw_lines()
        self._ensure_toc_current()
        targets = []
        for item in items:
            info = self.virtual_volume_items.get(item)
            if info is not None:
                targets.append((self._map_tree_line(info["row"]), info["title"]))
        if not targets:
            return
        document = self.editor.document()
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        for row, title in sorted(targets, reverse=True):
            block = document.findBlockByNumber(row)
            if not block.isValid():
                continue
            previous = block.previous()
            leading = "" if not previous.isValid() or not previous.text().strip() else "\n"
            cursor.setPosition(block.position())
            cursor.insertText(f"{leading}{title}\n\n")
        cursor.endEditBlock()
        self._sync_raw_lines()
        self._rebuild_toc()
        self._checkpoint_document()
        names = "、".join(title for _row, title in sorted(targets))
        self._show_status(i18n.T("已寫入卷標題：") + names, translated=True)

    def _show_toc_context_menu(self, pos):
        menu = self._build_toc_context_menu(pos)
        if menu is not None:
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    @action
    def select_chapter_text(self):
        """把選取章節的整段內容（標題＋正文）在本文裡選起來，方便直接複製。

        多選時選取涵蓋範圍的頭到尾——編輯器一次只能有一段選取，硬要分段
        反而看不出選到哪裡。"""
        self._sync_raw_lines()
        self._ensure_toc_current()
        total_lines = len(self.raw_lines)
        spans = []
        for node in self._selected_toc_items():
            span = toc_ops.toc_section_lines(
                self.tree, node, self.chapter_index_map, self.toc_boundary_map, total_lines)
            if span is not None:
                spans.append(span)
        if not spans:
            return
        document = self.editor.document()
        first_row = min(start for start, _end in spans)
        last_row = min(max(end for _start, end in spans), document.blockCount()) - 1
        start_block = document.findBlockByNumber(first_row)
        end_block = document.findBlockByNumber(max(first_row, last_row))
        if not start_block.isValid() or not end_block.isValid():
            return
        cursor = self.editor.textCursor()
        cursor.setPosition(start_block.position())
        cursor.setPosition(end_block.position() + end_block.length() - 1,
                           QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)
        self.editor.setFocus()
        self._show_status(i18n.T(f"已選取 {end_block.blockNumber() - first_row + 1} 行，可以直接複製（Ctrl+C）"))

    # ------------------------------------------------------------------
    # 剪下／貼上章節（調整章節順序）
    # ------------------------------------------------------------------

    def _selected_section_spans(self) -> list:
        """選取章節涵蓋的行範圍（0-based 半開區間），重疊的合併起來。"""
        total_lines = len(self.raw_lines)
        spans = []
        for node in self._selected_toc_items():
            span = toc_ops.toc_section_lines(
                self.tree, node, self.chapter_index_map, self.toc_boundary_map, total_lines)
            if span is not None and span[0] < span[1]:
                spans.append(span)
        spans.sort()
        merged: list = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    @action
    def cut_selected_chapters(self):
        """剪下＝先做記號，按貼上才真的搬動（跟檔案總管一樣）。

        沒有貼上就不會少任何東西；按 Esc 或重新剪下就取消。內容同時放進系統
        剪貼簿，也可以直接貼到其他程式。"""
        self._sync_raw_lines()
        self._ensure_toc_current()
        spans = self._selected_section_spans()
        if not spans:
            return
        labels = [self.toc_full_labels.get(node, node.text(0)) for node in self.tree.selectedItems()]
        summary = "、".join(label[:12] for label in labels[:2])
        if len(labels) > 2:
            summary += f" 等 {len(labels)} 項"
        moved_lines = [line for start, end in spans for line in self.raw_lines[start:end]]
        QApplication.clipboard().setText("\n".join(moved_lines))
        self._cut_state = {
            "spans": spans,
            "text_version": self._text_version,
            "summary": summary,
            "count": len(labels),
        }
        self._style_cut_items()
        self._show_status(i18n.T(f"已剪下 {len(labels)} 個章節（共 {len(moved_lines)} 行）；"
                                 "在目標章節按右鍵選擇貼上，按 Esc 取消"))

    def cancel_cut(self):
        if self._cut_state is None:
            return
        self._cut_state = None
        self._style_cut_items()
        self._show_status("已取消剪下")

    def _style_cut_items(self):
        """已剪下的章節在目錄裡變淡，一眼看得出「等著被搬走」。"""
        tokens = DARK if self.dark_mode else LIGHT
        spans = self._cut_state["spans"] if self._cut_state else []
        for item, row in self.chapter_raw_map.items():
            inside = any(start <= row < end for start, end in spans)
            font = item.font(0)
            if font.italic() != inside or inside:
                font.setItalic(inside or item in self.virtual_volume_items)
                item.setFont(0, font)
            if inside:
                item.setForeground(0, QColor(tokens.text_faint))
                item.setToolTip(0, i18n.T("已剪下，貼上後才會真的移動；按 Esc 取消"))
            elif item not in self.virtual_volume_items:
                item.setData(0, Qt.ItemDataRole.ForegroundRole, None)
                item.setToolTip(0, "")

    def _paste_target(self, item):
        """可以貼到這個項目旁邊嗎？回傳它的行範圍，不行則回傳 None。"""
        if self._cut_state is None or item is None:
            return None
        if self._cut_state["text_version"] != self._text_version:
            return None
        if item in self.virtual_volume_items:
            item = item.child(0) if item.childCount() else None
            if item is None:
                return None
        span = toc_ops.toc_section_lines(
            self.tree, item, self.chapter_index_map, self.toc_boundary_map, len(self.raw_lines))
        if span is None:
            return None
        # 不能貼到自己（或自己底下）：那等於把整段搬進它自己裡面。
        for start, end in self._cut_state["spans"]:
            if start <= span[0] < end:
                return None
        return span

    @action
    def paste_cut_chapters(self, item, before: bool):
        """把剪下的章節搬到指定章節的前面或後面，整個算一步。"""
        if self._cut_state is None:
            return
        if self._cut_state["text_version"] != self._text_version:
            self.cancel_cut()
            dialogs.info(self, "無法貼上", "剪下之後本文有變動，原本的範圍已經對不上，請重新剪下。")
            return
        target = self._paste_target(item)
        if target is None:
            dialogs.info(self, "無法貼上", "不能貼到剪下的章節自己（或它底下的章節）旁邊。")
            return
        spans = self._cut_state["spans"]
        insert_at = target[0] if before else target[1]
        # 目標節點在重建目錄時會被銷毀，標題要先留下來。
        target_label = self.toc_full_labels.get(item, item.text(0))[:16]
        new_lines, mapping = self._move_line_blocks(self.raw_lines, spans, insert_at)
        summary, count = self._cut_state["summary"], self._cut_state["count"]
        self._cut_state = None

        text = "\n".join(new_lines)
        self._set_editor_text(text)
        # 行號是自己算出來的，不必也不能靠 difflib 猜：搬移在比對時會變成
        # 「一處刪掉、一處新增」，被搬走章節的強制層級、忽略標記都會遺失。
        for attribute in ("ignored_chapters", "force_lv1_chapters", "force_lv2_chapters"):
            setattr(self, attribute, {mapping[row] for row in getattr(self, attribute) if row in mapping})
        self.auto_titles = {mapping[row]: value for row, value in self.auto_titles.items() if row in mapping}
        self.raw_lines = new_lines
        self._mark_synced(text)
        self._pending_line_maps.append(_chapter_line_mapper(mapping))
        self._rebuild_toc()
        self._checkpoint_document()
        self._show_status(i18n.T(
            f"已把 {count} 個章節（{summary}）移到「{target_label}」"
            f"{'之前' if before else '之後'}，可以按 Ctrl+Z 復原"))

    @staticmethod
    def _move_line_blocks(lines: list, spans: list, insert_at: int):
        """把 spans 這幾段行搬到 insert_at 之前，回傳（新的行, 舊行號→新行號）。

        接縫處順便整理空行：搬走的位置不留下連續空行，貼上的地方前後各留
        一個空行，這樣章節標題不會黏在上一段的最後一行。"""
        moved_rows = [row for start, end in spans for row in range(start, end)]
        # 搬動的內容去掉前後空行，貼上時再統一補。
        while moved_rows and not lines[moved_rows[0]].strip():
            moved_rows.pop(0)
        while moved_rows and not lines[moved_rows[-1]].strip():
            moved_rows.pop()
        cut_rows = {row for start, end in spans for row in range(start, end)}

        result: list = []
        mapping: dict = {}

        def emit(row):
            mapping[row] = len(result)
            result.append(lines[row])

        def emit_moved():
            if not moved_rows:
                return
            if result and result[-1].strip():
                result.append("")
            for row in moved_rows:
                emit(row)
            result.append("")

        for index, line in enumerate(lines):
            if index == insert_at:
                emit_moved()
            if index in cut_rows:
                continue
            if not line.strip() and result and not result[-1].strip():
                continue          # 搬走之後不要留下連續空行
            emit(index)
        if insert_at >= len(lines):
            emit_moved()
        while result and not result[-1].strip():
            result.pop()
        result.append("")
        return result, mapping

    @action
    def repair_selected_chapters(self):
        """整理選取章節內的硬換行，並清理段落中間的異常空白。只處理目錄中
        選取的章節（支援多選），不觸碰未選取的部分，也不會重新辨識章節結構。"""
        self._sync_raw_lines()
        self._ensure_toc_current()
        selected = self._selected_toc_items()
        source = self.editor.toPlainText()
        try:
            preview = toc_ops.preview_selected_repairs(
                self.tree, self.chapter_index_map, self.toc_boundary_map, source, selected)
        except ValueError as error:
            dialogs.info(self, "整理換行與空白", str(error))
            return
        if not preview.edits:
            dialogs.info(self, "整理換行與空白", "選取範圍內沒有偵測到需要調整的硬換行或異常空白。")
            return

        dialog = RepairDialog(preview, self)
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            result_preview = dialog.result_preview
        finally:
            dialog.deleteLater()
        if not accepted or result_preview is None:
            return
        try:
            count = toc_ops.apply_repair_edits(self.editor, result_preview)
        except ValueError as error:
            dialogs.error(self, "整理換行與空白", str(error))
            return
        self._sync_raw_lines()
        self._rebuild_toc()
        self._checkpoint_document()
        self._show_status(f"已整理 {count} 處，可按 Ctrl+Z 復原")

    @action
    def merge_selected_chapters(self):
        """保留第一個選取章節的標題，把其餘選取章節的正文接到它後面。
        支援不連續多選：例如選取第 2、3、7 章後，第 3 與第 7 章的正文
        會依序搬到第 2 章之後，兩者的標題行一併移除。"""
        self._sync_raw_lines()
        self._ensure_toc_current()
        nodes = [node for node in self._selected_toc_items() if node in self.chapter_index_map]
        if len(nodes) < 2:
            dialogs.info(self, "合併章節", "請先用 Ctrl 點選或 Shift 連續選取兩個以上的章節。")
            return

        lines = self.editor.toPlainText().split("\n")
        sections = []
        for node in nodes:
            span = toc_ops.toc_section_lines(
                self.tree, node, self.chapter_index_map, self.toc_boundary_map, len(lines))
            if span is not None:
                sections.append((span[0], span[1], node))
        if len(sections) < 2:
            dialogs.info(self, "合併章節", "選取的章節無法定位到正文，請先按「重新掃描目錄」再試一次。")
            return
        sections.sort()

        for (_, previous_end, _), (next_start, _, _) in zip(sections, sections[1:]):
            if next_start < previous_end:
                dialogs.info(
                    self, "合併章節",
                    "選取範圍互相重疊（可能同時選到某一卷與它底下的章節）。\n"
                    "請只選取同一層級、彼此獨立的章節。",
                )
                return

        keep_start, keep_end, keep_node = sections[0]
        keep_title = keep_node.text(0)
        others = sections[1:]
        preview_text = "、".join(node.text(0)[:20] for _, _, node in others[:4])
        if len(others) > 4:
            preview_text += f" 等 {len(others)} 章"
        if not dialogs.confirm(
            self, "合併章節",
            f"將把以下章節的正文併入「{keep_title[:30]}」：\n\n{preview_text}\n\n"
            "這些章節的標題行會被移除，正文依原順序接續在後。是否繼續？",
        ):
            return

        moved = []
        for start, end, _ in others:
            body = lines[start + 1:end]           # 跳過標題行，只取正文
            while body and not body[0].strip():    # 去掉正文前後的空行，稍後統一補
                body.pop(0)
            while body and not body[-1].strip():
                body.pop()
            if body:
                moved.append(body)

        new_lines = list(lines)
        for start, end, _ in reversed(others):     # 由後往前刪除，避免行號位移
            del new_lines[start:end]

        block = []
        for body in moved:
            if block:
                block.append("")
            block.extend(body)
        if block:
            if keep_end > 0 and new_lines[keep_end - 1].strip():
                block.insert(0, "")
            if keep_end < len(new_lines) and new_lines[keep_end].strip():
                block.append("")
            new_lines[keep_end:keep_end] = block

        self._set_editor_text("\n".join(new_lines))
        self._sync_raw_lines()
        self._rebuild_toc()

        for item, raw_index in self.chapter_raw_map.items():
            if raw_index == keep_start:
                self.tree.setCurrentItem(item)
                self._on_tree_item_clicked(item, 0)
                break
        self._checkpoint_document()
        self._show_status(f"已將 {len(others)} 個章節的正文併入「{keep_title[:20]}」")

    @action
    def renumber_selected_chapters(self):
        """把選取的章節（或卷／集／篇）改成連續編號：保留第一個（依文件順序）
        的原編號當起點，其餘依序遞增。同一次只能對同一種單位操作。"""
        self._sync_raw_lines()
        self._ensure_toc_current()
        selected_nodes = [node for node in self._selected_toc_items()
                          if self.chapter_records.get(node, {}).get("kind") in ("chapter", "volume")]
        if len(selected_nodes) < 2:
            dialogs.info(self, "連續編號", "請選取兩個以上的章節或卷節點（不含作品層級）。")
            return
        if not self._chapter_nodes_share_unit(selected_nodes):
            dialogs.info(
                self, "連續編號",
                "選取的項目類型不一致（例如同時選到「第…卷」和「第…集」，"
                "或「第…集」和「第…章」），這些是各自獨立的編號系統，"
                "混在一起重排沒有意義，請分開執行。",
            )
            return

        selected_nodes.sort(key=lambda node: self.chapter_raw_map.get(node, 0))

        plans, skipped = [], []
        for node in selected_nodes:
            raw_idx = self.chapter_raw_map.get(node)
            if raw_idx is None or raw_idx >= len(self.raw_lines):
                skipped.append((node, "找不到對應行"))
                continue
            raw_line = self.raw_lines[raw_idx]
            leading = raw_line[:len(raw_line) - len(raw_line.lstrip())]
            clean, marker = strip_persistent_title_marker(raw_line.strip())
            located = locate_chapter_number(clean)
            if located is None:
                skipped.append((node, "無法自動判斷編號位置"))
                continue
            start, end, number_text = located
            current_number = int(round(chinese_to_arabic(number_text)))
            plans.append({
                "node": node, "raw_idx": raw_idx, "leading": leading,
                "clean": clean, "marker": marker, "start": start, "end": end,
                "number_text": number_text, "current_number": current_number,
            })

        if len(plans) < 2:
            detail = "\n".join(f"• {node.text(0)[:24]}：{reason}" for node, reason in skipped)
            dialogs.info(
                self, "連續編號",
                "選取範圍內能自動判斷編號位置的項目不足兩個，無法執行。\n\n" + detail,
            )
            return

        anchor_number = plans[0]["current_number"]
        targets = [anchor_number + offset for offset in range(len(plans))]

        selected_node_set = {plan["node"] for plan in plans}
        reference_signature = chapter_unit_signature(plans[0]["clean"])
        sibling_index = {}
        for parent in {plan["node"].parent() for plan in plans}:
            for sibling in toc_ops.tree_children(self.tree, parent):
                if sibling in selected_node_set:
                    continue
                sibling_row = self.chapter_raw_map.get(sibling)
                if sibling_row is None or sibling_row >= len(self.raw_lines):
                    continue
                sibling_clean, _m = strip_persistent_title_marker(self.raw_lines[sibling_row].strip())
                if chapter_unit_signature(sibling_clean) != reference_signature:
                    continue
                sibling_located = locate_chapter_number(sibling_clean)
                if sibling_located is None:
                    continue
                _s, _e, sibling_number_text = sibling_located
                key = (parent, int(round(chinese_to_arabic(sibling_number_text))))
                sibling_index.setdefault(key, sibling)

        conflicts = []
        for plan, target in zip(plans, targets):
            sibling = sibling_index.get((plan["node"].parent(), target))
            if sibling is not None:
                conflicts.append((plan, target, sibling))

        changes = [(plan, target) for plan, target in zip(plans, targets)
                  if plan["current_number"] != target]
        if not changes:
            dialogs.info(self, "連續編號", "選取的項目編號本來就已經連續，不需要調整。")
            return

        preview_lines = [f"「{plan['node'].text(0)[:22]}」：{plan['current_number']} → {target}"
                         for plan, target in changes[:8]]
        if len(changes) > 8:
            preview_lines.append(f"……等共 {len(changes)} 處")
        message = "將調整以下編號：\n\n" + "\n".join(preview_lines)
        if skipped:
            message += "\n\n以下項目無法判斷編號位置，將維持不變：\n" + "\n".join(
                f"• {node.text(0)[:22]}：{reason}" for node, reason in skipped)
        if conflicts:
            conflict_lines = "\n".join(
                f"• 目標編號 {target} 與「{sibling.text(0)[:22]}」重複"
                for _plan, target, sibling in conflicts[:5])
            message += "\n\n⚠️ 以下目標編號會與未選取的項目重複，繼續的話會出現重複編號：\n" + conflict_lines
        message += "\n\n此操作可用「復原」撤銷。是否繼續？"

        if not dialogs.confirm(self, "連續編號", message):
            return

        marker_suffix = {"exclude": "[::X]", "include": "[::]",
                         "auto_work": "[::W]", "auto_title": "[::T]", "": ""}
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        for plan, target in changes:
            new_number_text = render_chapter_number_like(target, plan["number_text"])
            new_clean = plan["clean"][:plan["start"]] + new_number_text + plan["clean"][plan["end"]:]
            new_line = plan["leading"] + new_clean + marker_suffix[plan["marker"]]
            self._replace_line(cursor, plan["raw_idx"], new_line)
        cursor.endEditBlock()

        self._sync_raw_lines()
        self._rebuild_toc()
        self._checkpoint_document()
        self._show_status(f"已將 {len(changes)} 個章節改為連續編號，可退回上一步")

    def _exclude_marker_for(self, raw_idx: int, line: str) -> str:
        """把某一行移出目錄時，行尾該留什麼？

        只有「使用者自己用 [::] 加進目錄、而且拿掉標記之後不會被自動認成
        章節」的行，才直接把標記清掉——那段正文本來就不是章節，留一個 [::X]
        只是在檔案裡多一個看不懂的符號。

        其他情況一律寫 [::X]：沒有標記、或是 [::W]／[::T] 這類自動標記的行，
        它會在目錄裡是因為別的機制（合集結構、自動標題快取、強制層級…），
        只清標記的話文字根本沒變，重新掃描它又會回來，使用者卻看到「已移除」。"""
        clean, marker = strip_persistent_title_marker(line.strip())
        if marker != "include":
            return "[::X]"
        if raw_idx in self.force_lv1_chapters or raw_idx in self.force_lv2_chapters:
            return "[::X]"
        return "[::X]" if looks_like_auto_chapter(clean, self.user_chapter_rules) else ""

    @action
    def ignore_selected_chapter(self):
        """章節保留在正文中，只是行尾加上 [::X]，從目錄移除。"""
        self._sync_raw_lines()
        self._ensure_toc_current()
        selected = self._selected_toc_items()
        if not selected:
            return
        targets = sorted({raw_idx for node in selected
                          if (raw_idx := self.chapter_raw_map.get(node)) is not None}, reverse=True)
        if not targets:
            return
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        marked_count, last_clean, marked_any = 0, "", False
        for raw_idx in targets:
            raw_str = self.raw_lines[raw_idx]
            clean, marker = strip_persistent_title_marker(raw_str.strip())
            if marker == "exclude":
                continue
            leading = raw_str[:len(raw_str) - len(raw_str.lstrip())]
            suffix = self._exclude_marker_for(raw_idx, raw_str)
            self._replace_line(cursor, raw_idx, leading + clean + suffix)
            marked_any = marked_any or bool(suffix)
            marked_count += 1
            last_clean = clean
        cursor.endEditBlock()
        if not marked_count:
            return
        self._sync_raw_lines()
        self._rebuild_toc()
        self._checkpoint_document()
        note = "（行尾已加上 [::X]）" if marked_any else "（原本就不是章節，已直接清掉標記）"
        if marked_count == 1:
            self._show_status(f"已排除章節：{last_clean[:18]}{note}")
        else:
            self._show_status(f"已排除 {marked_count} 個章節{note}")

    @action
    def set_chapter_level(self, level: int):
        """章節層級（force_lv1/2_chapters）是跟著文件走的結構狀態，不是文字，
        所以就算沒有改到任何一個字，也要單獨存一次復原步驟，不然 Ctrl+Z
        永遠碰不到這個操作。"""
        self._sync_raw_lines()
        self._ensure_toc_current()
        selected = self._selected_toc_items()
        if not selected:
            return
        targets = {raw_idx for node in selected if (raw_idx := self.chapter_raw_map.get(node)) is not None}
        if not targets:
            return
        for raw_idx in targets:
            if level == 1:
                self.force_lv1_chapters.add(raw_idx)
                self.force_lv2_chapters.discard(raw_idx)
            elif level == 2:
                self.force_lv2_chapters.add(raw_idx)
                self.force_lv1_chapters.discard(raw_idx)
        self._rebuild_toc()
        self._checkpoint_document()
        if len(targets) == 1:
            raw_str = self.raw_lines[next(iter(targets))].strip()
            self._show_status(f"已修改層級：{raw_str[:15]}...")
        else:
            self._show_status(f"已將 {len(targets)} 個章節設為層級 {level}")

    @action
    def delete_selected_chapter(self):
        self._sync_raw_lines()
        self._ensure_toc_current()
        selected = self._selected_toc_items()
        if not selected:
            return
        lines = self.editor.toPlainText().split("\n")
        total_lines = len(lines)
        sections = []
        for node in selected:
            span = toc_ops.toc_section_lines(
                self.tree, node, self.chapter_index_map, self.toc_boundary_map, total_lines)
            if span is not None:
                sections.append((span[0], span[1], node))
        if not sections:
            return
        sections.sort()
        merged = [sections[0]]
        for start, end, node in sections[1:]:
            last_start, last_end, last_node = merged[-1]
            if start < last_end:
                merged[-1] = (last_start, max(last_end, end), last_node)
            else:
                merged.append((start, end, node))
        sections = merged

        if len(sections) == 1:
            ch_name = sections[0][2].text(0)
            question = f"確定要刪除「{ch_name}」及其所有正文內容嗎？"
            done_text = f"已刪除：{ch_name}"
        else:
            preview_text = "、".join(node.text(0)[:20] for _, _, node in sections[:4])
            if len(sections) > 4:
                preview_text += f" 等 {len(sections)} 章"
            question = f"確定要刪除以下章節及其所有正文內容嗎？\n\n{preview_text}"
            done_text = f"已刪除 {len(sections)} 個章節"
        if not dialogs.confirm(self, "刪除章節", question):
            return

        document = self.editor.document()
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        for start, end, _ in reversed(sections):    # 由後往前刪除，讓前面區段的行號保持有效
            start_block = document.findBlockByNumber(start)
            end_block = document.findBlockByNumber(end)
            end_of_document = document.characterCount() - 1     # Qt 單位，不能用 len(文字)
            start_pos = start_block.position() if start_block.isValid() else end_of_document
            end_pos = end_block.position() if end_block.isValid() else end_of_document
            cursor.setPosition(start_pos)
            cursor.setPosition(end_pos, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
        cursor.endEditBlock()

        self._sync_raw_lines()
        self._rebuild_toc()
        self._checkpoint_document()
        self._show_status(done_text)

    # ------------------------------------------------------------------
    # 本文右鍵選單：加入目錄／取消「非章節」標記
    # ------------------------------------------------------------------

    def _build_editor_context_menu(self):
        """只負責組出選單，不呼叫 exec()——方便測試時不用真的彈出視窗。

        刻意不用 QPlainTextEdit.createStandardContextMenu()：那組復原／剪下／
        複製／貼上／全選是 Qt 內建、沒套用中文翻譯，混在自己中文選單裡顯得
        突兀，而且復原／剪貼本來就有全域快捷鍵可用，選單裡不需要重複。

        「加入目錄」「移除目錄」一次只會有一個能按：看目標那一行現在是不是
        已經在目錄裡。"""
        menu = QMenu(self)
        insert_action = menu.addAction("新增章節")
        insert_action.triggered.connect(self.open_insert_title_dialog)
        menu.addSeparator()

        target = self._editor_target_line()
        in_toc = target is not None and target[0] in self._current_title_lines()
        add_action = menu.addAction("將所選文字加入目錄")
        add_action.setEnabled(target is not None and not in_toc)
        add_action.triggered.connect(self.mark_selected_as_title)
        remove_action = menu.addAction("將所選文字移除目錄")
        remove_action.setEnabled(target is not None and in_toc)
        remove_action.triggered.connect(self.exclude_selected_line)

        # 下方接上 Qt 自己的剪下／複製／貼上／全選：這些動作的文字由 Qt 的
        # 翻譯檔提供（見 i18n.install_qt_translation），會跟著繁簡切換。
        standard = self.editor.createStandardContextMenu()
        # 去掉 Qt 自己的復原／重做：Qt 的復原已經關掉（復原走自訂快照系統），
        # 留著只會是兩個永遠灰掉的項目。Ctrl+Z／Ctrl+Y 與工具列按鈕照常可用。
        undo_keys = {QKeySequence(QKeySequence.StandardKey.Undo).toString(),
                     QKeySequence(QKeySequence.StandardKey.Redo).toString()}
        # Qt 把快捷鍵直接寫在動作文字裡（「復原(&U)	Ctrl+Z」），shortcut() 是空的，
        # 所以從文字尾端取快捷鍵來比對。
        editing_actions = [action for action in standard.actions()
                           if action.text() and action.text().rpartition("	")[2] not in undo_keys]
        if editing_actions:
            menu.addSeparator()
            # 只把「動作」接過來，改由這個選單持有；標準選單本身要丟掉，
            # 不能拿它當子元件——QMenu 是一個真的視窗元件，掛成子元件會被
            # 整個畫在自訂選單上面，變成兩層選單疊在一起。
            for standard_action in editing_actions:
                standard_action.setParent(menu)
            menu.addActions(editing_actions)
        standard.deleteLater()
        return menu

    def _show_editor_context_menu(self, pos):
        # 在選取範圍以外按右鍵時，先把游標移到按的位置：選單裡的動作（新增
        # 章節、加入／移除目錄）都是針對「游標所在那一行」。
        clicked = self.editor.cursorForPosition(pos)
        current = self.editor.textCursor()
        if not (current.hasSelection()
                and current.selectionStart() <= clicked.position() <= current.selectionEnd()):
            self.editor.setTextCursor(clicked)
        menu = self._build_editor_context_menu()
        menu.exec(self.editor.viewport().mapToGlobal(pos))

    def _current_title_lines(self) -> set:
        """目前目錄裡每個節點在正文的行號（已換算到編輯器現在的內容）。"""
        self._sync_raw_lines()
        return {self._map_tree_line(row) for row in self.chapter_raw_map.values()}

    def _editor_target_line(self):
        """右鍵動作的目標行：有選取時是選取範圍內唯一的非空白行，沒選取時是
        游標所在行。多行或空白行回傳 None——避免把整段正文誤加進目錄。"""
        cursor = self.editor.textCursor()
        document = self.editor.document()
        if not cursor.hasSelection():
            block = cursor.block()
            return (block.blockNumber(), block.text()) if block.text().strip() else None
        start_block = document.findBlock(cursor.selectionStart())
        end_block = document.findBlock(cursor.selectionEnd())
        end_number = end_block.blockNumber()
        if cursor.selectionEnd() == end_block.position() and end_number > start_block.blockNumber():
            end_number -= 1
        lines = []
        for number in range(start_block.blockNumber(), end_number + 1):
            text = document.findBlockByNumber(number).text()
            if text.strip():
                lines.append((number, text))
        return lines[0] if len(lines) == 1 else None

    @action
    def mark_selected_as_title(self):
        """把目標行標成目錄章節（行尾加 [::]；原本若是 [::X] 會一併換掉）。"""
        target = self._editor_target_line()
        if target is None:
            dialogs.info(self, "一次限一個標題", "請選取（或把游標放在）單獨一行章節標題，避免將正文誤加到目錄。")
            return
        clean_title = self._set_line_marker(target[0], "[::]")
        if clean_title is None:
            return
        for item, raw_index in self.chapter_raw_map.items():
            if raw_index == target[0]:
                self.tree.setCurrentItem(item)
                self._on_tree_item_clicked(item, 0)
                break
        self._show_status(f"已加入目錄：{clean_title}")

    @action
    def exclude_selected_line(self):
        """把目標行移出目錄（行尾加 [::X]；原本若是 [::] 會一併換掉）。"""
        target = self._editor_target_line()
        if target is None:
            dialogs.info(self, "一次限一行", "請選取（或把游標放在）單獨一行章節標題。")
            return
        suffix = self._exclude_marker_for(target[0], target[1])
        clean_title = self._set_line_marker(target[0], suffix)
        if clean_title is None:
            return
        self._show_status(f"已移出目錄：{clean_title}" if suffix
                          else f"已移除章節標記：{clean_title}")

    def _set_line_marker(self, line_number: int, marker: str):
        block = self.editor.document().findBlockByNumber(line_number)
        original = block.text()
        clean, _old_marker = strip_persistent_title_marker(original.strip())
        if not clean:
            return None
        leading = original[:len(original) - len(original.lstrip())]
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        self._replace_line(cursor, line_number, leading + clean + marker)
        cursor.endEditBlock()
        self._sync_raw_lines()
        self._rebuild_toc()
        self._checkpoint_document()
        return clean

