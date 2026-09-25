"""自訂章節規則對話框：管理自訂章節規則，並找出本文裡還沒收進目錄的可疑章節。

兩個分頁：

「規則」：上方「常用格式」是一排可以複選的勾選框（井號數字、數字加點、
括號數字…），一種格式一條規則；勾選就加進規則清單並啟用，取消就從清單
移除。每個選項後面標出本文有幾行符合，旁邊的「未收錄 N 行」可以直接跳到
第二頁，看是哪幾行。

「本文可疑章節」：看起來像章節、但目前不在目錄裡的行（原本獨立的「檢查未
辨識章節」）。每行都有信心度——同格式前後編號連續、兩章之間有夠多正文就
比較可信；編號連續但中間幾乎沒有正文，通常是正文裡的條列。整種格式都是
章節就「存成規則」；只有其中幾行是章節，就勾那幾行「加入目錄」。

編輯區的互動刻意保持簡單：切換選取列前如果有未儲存的修改，不會自動存成
草稿，必須自己按「儲存變更」，否則切換會直接捨棄。
"""

import re

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget,
)

from core.chapter_parse import weak_candidate_to_user_rule
from core.collection import scan_chapter_candidates
from core.persistence import RULES_FILE, _save_json
from core.title_markers import strip_persistent_title_marker
from core.user_rules import (
    PRESET_RULES, is_risky_pattern, match_user_chapter_rule, preset_rule, rule_from_sample,
)
from . import dialogs, i18n
from .sortable_table import CONFIDENCE_ORDER, data_index, enable_sorting, make_item, resort, setup_columns
from .theme import active_tokens
from .widgets import Divider, IconTextButton, fit_window_to_screen, flow_container, size_dialog

_LEVEL_LABELS = {1: "卷", 2: "章"}

# 「快速插入」的片段：名稱 → （插入的正則, 說明）。寫給不熟正則的人看，
# 所以說明一律用白話，不解釋語法本身。
_SNIPPETS = [
    ("章號", r"(?P<number>[0-9０-９]{1,8})", "數字章號（001、12…）；缺章檢查與連續編號都靠它"),
    ("中文章號", r"(?P<number>[一二兩两三四五六七八九十百千零〇]{1,8})", "中文數字章號（一、十二…）"),
    ("章名", r"(?P<title>\S.*?)", "章號後面那段標題文字"),
    ("任意文字", ".*", "任何字都可以，長度不限"),
    ("空白", r"\s*", "可有可無的空白"),
    ("行首", "^", "從這一行的開頭開始比對"),
    ("行尾", "$", "比對到這一行結束"),
]

_RULES_TAB, _CANDIDATES_TAB = 0, 1


class _CompactTable(QTableWidget):
    """建議高度就是最小高度（標題列＋兩列）。表格預設建議約 190px，放在會
    捲動的頁面裡時，頁面就照這個高度排，開窗時表格佔掉一大塊。
    視窗拉高時表格照樣會跟著變高（版面的伸展係數不變）。"""

    def sizeHint(self):
        hint = super().sizeHint()
        if self.minimumHeight() > 0:
            hint.setHeight(self.minimumHeight())
        return hint


class _FitFlowHeight(QObject):
    """寬度改變時，把自動換行區塊的最小高度設成這個寬度實際需要的高度。"""

    def __init__(self, box, flow):
        super().__init__(box)
        self._flow = flow

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize and event.size().width() != event.oldSize().width():
            watched.setMinimumHeight(self._flow.heightForWidth(event.size().width()))
        return False


class RulesDialog(QDialog):
    """建構時吃現有規則清單與目前的本文。

    按「保存並重掃」（或第二頁的「加入目錄」）之後：
      result_rules：新的規則清單
      result_lines：有逐行加入目錄時，是加上 [::] 之後的整份本文；否則 None
      result_volume_rows：逐行加入的行裡，哪些要當成卷（卷級格式）
    """

    candidateHighlighted = Signal(int, int)  # start_line, end_line（0 起算，單行）

    def __init__(self, rules: list, get_document_lines, parent=None, known_rows=None,
                 presets_expanded: bool = True):
        super().__init__(parent)
        self.setWindowTitle("自訂章節規則")
        self._working = [dict(rule) for rule in rules]
        self._selected_index: int | None = None
        self.result_rules: list | None = None
        self.result_lines: list | None = None
        self.result_volume_rows: set = set()

        self._presets_expanded = presets_expanded
        # 計數、可疑章節、測試都用同一份本文，不會前後不一致；對話框開著時
        # 本文改了，主視窗會呼叫 reload() 換成新的一份。
        self._analyze_document(get_document_lines(), known_rows)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(10)

        title = QLabel("自訂章節辨識規則")
        title.setObjectName("appTitle")
        root.addWidget(title)

        self.tabs = QTabWidget()
        # 規則頁東西多（常用格式、表格、編輯區）：螢幕不夠高時整頁捲動，
        # 不要讓對話框的最小高度超出螢幕、下半部被切掉。
        self._rules_scroll = QScrollArea()
        self._rules_scroll.setObjectName("panelScroll")
        self._rules_scroll.setWidgetResizable(True)
        self._rules_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._rules_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rules_page = self._build_rules_tab()
        rules_page.setObjectName("panelScrollContent")
        self._rules_scroll.setWidget(rules_page)
        self._fitted = False
        self.tabs.addTab(self._rules_scroll, "規則")
        self.tabs.addTab(self._build_candidates_tab(), f"本文可疑章節（{len(self._candidates)}）")
        root.addWidget(self.tabs, 1)

        buttons = QDialogButtonBox()
        cancel_button = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        commit_button = buttons.addButton("保存並重掃", QDialogButtonBox.ButtonRole.AcceptRole)
        commit_button.setObjectName("primary")
        cancel_button.clicked.connect(self.reject)
        commit_button.clicked.connect(self._commit)
        root.addWidget(buttons)

        self._refresh_table()
        self._update_buttons()
        self._refresh_candidates()
        self._size_to_content()

    def _analyze_document(self, lines, known_rows):
        self._lines = list(lines)
        self._candidates = scan_chapter_candidates(self._lines, set(known_rows or ()))
        self._checked: set[int] = set()
        self._format_counts: dict[str, int] = {}
        for candidate in self._candidates:
            self._format_counts[candidate["format"]] = self._format_counts.get(candidate["format"], 0) + 1

    def reload(self, lines, known_rows=None):
        """對話框開著時本文被改過（使用者直接在本文編輯、刪行…）：換成新的
        一份重算。規則頁正在編輯的內容不動；第二頁勾選的行號可能已經對不上，
        清掉重來。"""
        self._analyze_document(lines, known_rows)
        self._update_preset_counts()
        self._populate_format_combo()
        self.tabs.setTabText(_CANDIDATES_TAB, i18n.T(f"本文可疑章節（{len(self._candidates)}）"))
        self._refresh_candidates()

    def _size_to_content(self):
        """規則表格看得到兩列就夠了，多的用表格自己的捲軸看。開窗高度在第一次
        顯示時照規則頁實際需要的高度算（見 showEvent），不再把多出來的空間都
        塞給表格；要看更多列，把視窗拉高即可（表格會跟著變高）。"""
        self.ensurePolished()
        row_height = self.table.verticalHeader().defaultSectionSize()
        header_height = self.table.horizontalHeader().sizeHint().height()
        for table in (self.table, self.candidate_table):
            table.setMinimumHeight(header_height + row_height * 2 + 4)
        wanted = getattr(self, "_two_column_width", 0) + self._rules_scroll.verticalScrollBar().sizeHint().width()
        parent = self.parentWidget()
        screen = parent.window().screen() if parent is not None else self.screen()
        if screen is not None:
            wanted = min(wanted, int(screen.availableGeometry().width() * 0.95))
        self.setMinimumWidth(max(self.minimumWidth(), wanted))
        size_dialog(self, 920, 600)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._fitted:
            self._fitted = True
            # 等版面排好（寬度確定、自動換行的區塊算出列數）之後再調高度。
            QTimer.singleShot(0, self._fit_height_to_rules_page)

    def _fit_height_to_rules_page(self):
        """把對話框調成規則頁剛好不用捲動的高度；螢幕不夠高就夾回螢幕內，
        剩下的用捲動。調高度後常用格式的列數不會變，但保險起見重算到穩定。"""
        page = self._rules_scroll.widget()
        layout = page.layout()
        for _attempt in range(3):
            self.layout().activate()
            # 用「沒有捲軸」時的寬度算：調好高度後捲軸就會消失，內容會變寬
            # （常用格式可能從兩欄變三欄、需要的高度變少）。
            width = self._rules_scroll.width() - 2 * self._rules_scroll.frameWidth()
            # 用「建議高度」而不是最小高度：捲動區就是照建議高度排內容的。
            needed = layout.heightForWidth(width) if layout.hasHeightForWidth() else page.sizeHint().height()
            extra = needed - self._rules_scroll.viewport().height()
            if not extra:
                break
            self.resize(self.width(), max(self.minimumHeight(), self.height() + extra))
        fit_window_to_screen(self)

    # ------------------------------------------------------------------
    # 版面

    def _build_rules_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(4, 12, 4, 4)
        root.setSpacing(10)

        # 常用格式可以摺起來（跟「書籍資料」一樣），把高度讓給下面的規則清單；
        # 摺起來時按鈕上仍看得到勾了幾種。
        tokens = active_tokens()
        self.presets_toggle = IconTextButton("chevron-up", "常用格式", checkable=True, size=16)
        self.presets_toggle.setObjectName("barToggle")
        self.presets_toggle.set_colors(tokens.icon, tokens.icon_hover, tokens.accent)
        root.addWidget(self.presets_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self._presets_area = QWidget()
        area_layout = QVBoxLayout(self._presets_area)
        area_layout.setContentsMargins(0, 0, 0, 0)
        area_layout.setSpacing(6)
        # 格子寬度統一、欄數照視窗寬度決定：直立螢幕一欄，寬螢幕兩三欄。
        preset_box, preset_flow = flow_container(uniform=True, h_spacing=18, v_spacing=4)
        self._preset_checks: dict[str, QCheckBox] = {}
        self._preset_view_buttons: dict[str, QPushButton] = {}
        for preset in PRESET_RULES:
            preset_id = preset["preset"]
            cell = QWidget()
            cell_layout = QHBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(8)
            # 括號裡的範例拿掉，視窗才能縮窄；範例改放在滑鼠停留的提示。
            check = QCheckBox(preset["name"])
            check.setToolTip(i18n.T("例如：") + preset["example"])
            check.toggled.connect(lambda checked, pid=preset_id: self._on_preset_toggled(pid, checked))
            self._preset_checks[preset_id] = check
            cell_layout.addWidget(check)
            # 符合的行裡還有不在目錄的，才顯示「未收錄 N 行」連結，點了到第二頁
            # 只看這種格式。沒有的就藏起來：一整排停用的按鈕只是雜訊。
            view = QPushButton("")
            view.setObjectName("inlineLink")
            view.setCursor(Qt.CursorShape.PointingHandCursor)
            view.clicked.connect(lambda _checked=False, pid=preset_id: self.show_candidates(f"preset:{pid}"))
            self._preset_view_buttons[preset_id] = view
            cell_layout.addWidget(view)
            cell_layout.addStretch(1)
            preset_flow.addWidget(cell)
        area_layout.addWidget(preset_box)

        # 常用格式沒涵蓋、但本文裡找得到的寫法（例如「1: 標題」）
        self._other_row = QWidget()
        other_layout = QHBoxLayout(self._other_row)
        other_layout.setContentsMargins(0, 0, 0, 0)
        self._other_label = QLabel("")
        self._other_label.setObjectName("fileLabel")
        self._other_label.setWordWrap(True)
        other_layout.addWidget(self._other_label, 1)
        other_button = QPushButton("查看其他格式")
        other_button.setObjectName("inlineLink")
        other_button.setCursor(Qt.CursorShape.PointingHandCursor)
        other_button.clicked.connect(lambda: self.show_candidates(self._other_formats[0]))
        other_layout.addWidget(other_button)
        area_layout.addWidget(self._other_row)
        self._update_preset_counts()
        root.addWidget(self._presets_area)
        self.presets_toggle.toggled.connect(self._on_presets_toggled)
        self.presets_toggle.setChecked(self._presets_expanded)
        self._on_presets_toggled(self._presets_expanded)
        self._lock_two_column_presets(preset_flow, preset_box)

        self.table = _CompactTable(0, 4)
        self.table.setHorizontalHeaderLabels(["啟用", "名稱", "層級", "正則"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        setup_columns(self.table, {0: "contents", 1: 160, 2: "contents"})
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._load_selected)
        # 刻意不開放點標題列排序：表格的順序就是規則的套用優先順序（由上往下
        # 比對，第一條符合的規則生效），只能用上移／下移調整。
        self.table.horizontalHeader().setSectionsClickable(False)
        root.addWidget(self.table, 1)

        # 表格的操作（對「選取的那條規則」）緊貼在表格下面；編輯區的按鈕放在
        # 編輯區底下。兩組分開，視窗變窄時也不會混成一排再亂換行。
        table_actions = QHBoxLayout()
        table_actions.setSpacing(8)
        self.move_up_button = QPushButton("上移")
        self.move_up_button.clicked.connect(lambda: self._move_rule(-1))
        self.move_down_button = QPushButton("下移")
        self.move_down_button.clicked.connect(lambda: self._move_rule(1))
        self.delete_button = QPushButton("刪除規則")
        self.delete_button.clicked.connect(self._delete_rule)
        for button in (self.move_up_button, self.move_down_button):
            table_actions.addWidget(button)
        table_actions.addStretch(1)
        table_actions.addWidget(self.delete_button)
        root.addLayout(table_actions)
        root.addWidget(Divider())

        editor_grid = QGridLayout()
        editor_grid.setHorizontalSpacing(12)
        editor_grid.setVerticalSpacing(8)
        editor_grid.addWidget(QLabel("名稱"), 0, 0)
        self.name_input = QLineEdit()
        editor_grid.addWidget(self.name_input, 0, 1)
        editor_grid.addWidget(QLabel("層級"), 0, 2)
        self.level_combo = QComboBox()
        self.level_combo.addItems(["卷", "章"])
        i18n.set_combo_value(self.level_combo, "章")
        editor_grid.addWidget(self.level_combo, 0, 3)
        editor_grid.addWidget(QLabel("從範例產生"), 1, 0)
        sample_row = QHBoxLayout()
        sample_row.setSpacing(8)
        self.sample_input = QLineEdit()
        self.sample_input.setPlaceholderText("貼上一行章節標題，例如：正文 001章：初入江湖")
        self.sample_input.returnPressed.connect(self._build_from_sample)
        sample_row.addWidget(self.sample_input, 1)
        sample_button = QPushButton("產生規則")
        sample_button.clicked.connect(self._build_from_sample)
        sample_row.addWidget(sample_button)
        editor_grid.addLayout(sample_row, 1, 1, 1, 3)

        editor_grid.addWidget(QLabel("正則"), 2, 0)
        self.pattern_input = QLineEdit()
        self.pattern_input.textChanged.connect(lambda _: self._validate_pattern(show_error=False))
        editor_grid.addWidget(self.pattern_input, 2, 1, 1, 3)

        snippet_box, snippet_flow = flow_container(h_spacing=6, v_spacing=6)
        snippet_flow.addWidget(QLabel("快速插入"))
        for label, snippet, explanation in _SNIPPETS:
            button = QPushButton(label)
            button.setToolTip(explanation)
            button.clicked.connect(lambda _checked=False, text=snippet: self._insert_snippet(text))
            snippet_flow.addWidget(button)
        editor_grid.addWidget(snippet_box, 3, 1, 1, 3)
        editor_grid.setColumnStretch(1, 1)
        root.addLayout(editor_grid)

        self.result_label = QLabel("")
        self.result_label.setWordWrap(True)
        root.addWidget(self.result_label)

        editor_actions = QHBoxLayout()
        editor_actions.setSpacing(8)
        clear_button = QPushButton("清空欄位")
        clear_button.clicked.connect(self._new_rule)
        editor_actions.addWidget(clear_button)
        editor_actions.addStretch(1)
        test_button = QPushButton("測試目前文件")
        test_button.clicked.connect(self._test_current_document)
        editor_actions.addWidget(test_button)
        self.save_button = QPushButton("儲存變更")
        self.save_button.clicked.connect(lambda: self._save_editor(refresh=True))
        editor_actions.addWidget(self.save_button)
        add_button = QPushButton("新增規則")
        add_button.setObjectName("primary")
        add_button.clicked.connect(self._add_rule)
        editor_actions.addWidget(add_button)
        root.addLayout(editor_actions)
        return page

    def _build_candidates_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(4, 12, 4, 4)
        root.setSpacing(10)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        filter_row.addWidget(QLabel("格式"))
        self.format_combo = QComboBox()
        self._populate_format_combo()
        i18n.skip(self.format_combo)   # 內容是算出來的，切換繁簡時由 _format_label 重組
        self.format_combo.currentIndexChanged.connect(lambda _index: self._refresh_candidates())
        filter_row.addWidget(self.format_combo, 1)
        for label, slot in (("勾選高信心", self._check_high_confidence), ("全部取消", self._uncheck_all)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            filter_row.addWidget(button)
        root.addLayout(filter_row)

        self.candidate_status = QLabel("")
        self.candidate_status.setObjectName("fileLabel")
        root.addWidget(self.candidate_status)

        self.candidate_table = _CompactTable(0, 4)
        self.candidate_table.setHorizontalHeaderLabels(["加入", "信心", "格式", "內容"])
        self.candidate_table.verticalHeader().setVisible(False)
        self.candidate_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.candidate_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        setup_columns(self.candidate_table, {0: "contents", 1: "contents", 2: "contents"})
        self.candidate_table.itemChanged.connect(self._on_candidate_changed)
        self.candidate_table.itemSelectionChanged.connect(self._on_candidate_selected)
        enable_sorting(self.candidate_table)
        root.addWidget(self.candidate_table, 1)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        # 存的是上方「格式」選的那一種（不是勾選的行），按鈕上直接寫出格式名稱；
        # 選「全部格式」時沒有對象，整顆藏起來。
        self.save_format_button = QPushButton("")
        i18n.skip(self.save_format_button)
        self.save_format_button.clicked.connect(self._save_format_as_rule)
        action_row.addWidget(self.save_format_button)
        action_row.addStretch(1)
        self.add_lines_button = QPushButton("把勾選的行加入目錄")
        self.add_lines_button.setObjectName("primary")
        self.add_lines_button.setToolTip("只處理勾選的行（行尾加上 [::]），同時保存規則並關閉視窗")
        self.add_lines_button.clicked.connect(self._add_checked_lines)
        action_row.addWidget(self.add_lines_button)
        root.addLayout(action_row)
        return page

    # ------------------------------------------------------------------
    # 常用格式

    def _count_preset_matches(self) -> dict:
        counts = {preset["preset"]: 0 for preset in PRESET_RULES}
        rules = {preset["preset"]: [preset_rule(preset["preset"])] for preset in PRESET_RULES}
        for line in self._lines:
            clean, marker = strip_persistent_title_marker(line.strip())
            if not clean or marker == "exclude" or len(clean) > 60:
                continue
            for preset_id, rule in rules.items():
                if match_user_chapter_rule(clean, rule):
                    counts[preset_id] += 1
        return counts

    def _update_preset_counts(self):
        counts = self._count_preset_matches()
        names = {preset["preset"]: preset["name"] for preset in PRESET_RULES}
        for preset_id, check in self._preset_checks.items():
            i18n.set_text(check, f"{names[preset_id]} · 本文 {counts.get(preset_id, 0)} 行")
            pending = self._format_counts.get(f"preset:{preset_id}", 0)
            view = self._preset_view_buttons[preset_id]
            view.setVisible(bool(pending))
            if pending:
                i18n.set_text(view, f"未收錄 {pending} 行")
        other = [(fmt, count) for fmt, count in self._format_counts.items() if fmt.startswith("weak:")]
        self._other_formats = [fmt for fmt, _count in other]
        self._other_row.setVisible(bool(other))
        if other:
            labels = "、".join(f"{self._format_label(fmt)}（{count} 行）" for fmt, count in other[:4])
            i18n.set_text(self._other_label, f"常用格式沒涵蓋、但本文偵測到：{labels}")

    def _preset_index(self, preset_id: str):
        return next((index for index, rule in enumerate(self._working) if rule.get("preset") == preset_id), None)

    def _on_preset_toggled(self, preset_id: str, checked: bool):
        index = self._preset_index(preset_id)
        if checked:
            if index is None:
                self._working.append(preset_rule(preset_id))
            else:
                self._working[index]["enabled"] = True
        elif index is not None:
            del self._working[index]
            if self._selected_index is not None:
                self._new_rule()
        self._refresh_table()

    def _sync_preset_checks(self):
        for preset_id, check in self._preset_checks.items():
            index = self._preset_index(preset_id)
            check.blockSignals(True)
            check.setChecked(index is not None and self._working[index].get("enabled", True))
            check.blockSignals(False)
        checked = sum(1 for check in self._preset_checks.values() if check.isChecked())
        i18n.set_text(self.presets_toggle, f"常用格式（已勾選 {checked} 種）" if checked else "常用格式")

    def _lock_two_column_presets(self, flow, box):
        """視窗最窄就是「常用格式剛好排兩欄」。

        摺起來時視窗可以縮得比較窄，但一展開就只剩一欄、下半部被擠出視窗外
        （實際發生過）。所以不管摺不摺，最小寬度都對齊兩欄。
        高度：外層版面不會自己照寬度問「要排幾列」，所以先保留兩欄需要的列數，
        之後每次寬度改變再照實際列數更新（寬的時候排成三欄，不留一截空白）。"""
        cells = [flow.itemAt(index) for index in range(flow.count())]
        if not cells:
            return
        cell_width = max(cell.sizeHint().width() for cell in cells)
        row_height = max(cell.sizeHint().height() for cell in cells)
        rows = (len(cells) + 1) // 2
        box.setMinimumHeight(rows * row_height + (rows - 1) * 4)
        box.installEventFilter(_FitFlowHeight(box, flow))
        # 20＋20 是對話框左右邊界，4＋4 是分頁內容的邊界，再留一點分頁框線。
        # 規則頁可以捲動，捲軸出現時會再吃掉一條寬度，在 _size_to_content 補上。
        self._two_column_width = cell_width * 2 + 18 + 20 * 2 + 4 * 2 + 6

    def _on_presets_toggled(self, expanded: bool):
        self._presets_area.setVisible(expanded)
        self.presets_toggle.set_icon_name("chevron-up" if expanded else "chevron-down")

    def presets_expanded(self) -> bool:
        return self.presets_toggle.isChecked()

    # ------------------------------------------------------------------
    # 本文可疑章節

    def _populate_format_combo(self):
        """格式清單（重新分析本文後也用這個重建；原本選的格式還在就留著）。"""
        current = self.format_combo.currentData() if self.format_combo.count() else None
        self.format_combo.blockSignals(True)
        self.format_combo.clear()
        self.format_combo.addItem(i18n.T(f"全部格式（{len(self._candidates)} 行）"), None)
        for fmt, count in self._format_counts.items():
            kind = "常用格式" if fmt.startswith("preset:") else "其他格式"
            self.format_combo.addItem(i18n.T(f"{self._format_label(fmt)}（{count} 行）· {kind}"), fmt)
        index = self.format_combo.findData(current) if current is not None else 0
        self.format_combo.setCurrentIndex(max(index, 0))
        self.format_combo.blockSignals(False)

    def _format_label(self, fmt: str) -> str:
        return next((candidate["label"] for candidate in self._candidates if candidate["format"] == fmt), fmt)

    def _current_format(self):
        return self.format_combo.currentData()

    def show_candidates(self, fmt=None):
        """切到「本文可疑章節」分頁，只看某一種格式（None＝全部）。"""
        index = self.format_combo.findData(fmt) if fmt is not None else 0
        self.format_combo.setCurrentIndex(max(index, 0))
        self.tabs.setCurrentIndex(_CANDIDATES_TAB)

    def _visible_candidates(self) -> list:
        fmt = self._current_format()
        return [index for index, candidate in enumerate(self._candidates)
                if fmt is None or candidate["format"] == fmt]

    def _refresh_candidates(self):
        visible = self._visible_candidates()
        table = self.candidate_table
        table.blockSignals(True)
        table.setRowCount(len(visible))
        for row, index in enumerate(visible):
            candidate = self._candidates[index]
            check_item = make_item("", index)
            check_item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            check_item.setCheckState(Qt.CheckState.Checked if index in self._checked else Qt.CheckState.Unchecked)
            table.setItem(row, 0, check_item)
            table.setItem(row, 1, make_item(candidate["confidence"], index,
                                            CONFIDENCE_ORDER.get(candidate["confidence"], 9)))
            level_note = "（卷）" if candidate["level"] == 1 else ""
            table.setItem(row, 2, make_item(i18n.T(candidate["label"]) + level_note, index))
            preview = re.sub(r"\s+", " ", candidate["text"]).strip()
            if len(preview) > 160:
                preview = preview[:157] + "…"
            table.setItem(row, 3, make_item(preview, index, candidate["index"]))
        table.blockSignals(False)
        resort(table)
        fmt = self._current_format()
        self.save_format_button.setVisible(fmt is not None)
        if fmt is not None:
            self.save_format_button.setText(
                i18n.T("把「{name}」這種格式存成規則").format(name=i18n.T(self._format_label(fmt))))
        self._update_candidate_status()

    def _update_candidate_status(self):
        visible = self._visible_candidates()
        fmt = self._current_format()
        checked = len(self._checked)
        if not self._candidates:
            text = "目前沒有可疑章節"
        elif fmt is None:
            text = f"共 {len(visible)} 行；已勾選 {checked} 行"
        else:
            # 分頁標題是全部格式的總數；篩選中只看得到其中一種，要講清楚，
            # 不然會以為標題的數字跟清單對不上。
            text = (f"目前只顯示「{self._format_label(fmt)}」{len(visible)} 行，"
                    f"全部共 {len(self._candidates)} 行（格式選「全部格式」可以看全部）；"
                    f"已勾選 {checked} 行")
        i18n.set_text(self.candidate_status, text)
        self.add_lines_button.setEnabled(bool(self._checked))

    def _on_candidate_changed(self, item: QTableWidgetItem):
        if item.column() != 0:
            return
        index = data_index(self.candidate_table, item.row())
        if item.checkState() == Qt.CheckState.Checked:
            self._checked.add(index)
        else:
            self._checked.discard(index)
        self._update_candidate_status()

    def _on_candidate_selected(self):
        rows = self.candidate_table.selectionModel().selectedRows()
        if not rows:
            return
        candidate = self._candidates[data_index(self.candidate_table, rows[0].row())]
        self.candidateHighlighted.emit(candidate["index"], candidate["index"])

    def _check_high_confidence(self):
        self._checked |= {index for index in self._visible_candidates()
                          if self._candidates[index]["confidence"] == "高"}
        self._refresh_candidates()

    def _uncheck_all(self):
        self._checked -= set(self._visible_candidates())
        self._refresh_candidates()

    def _save_format_as_rule(self):
        """目前選的那種格式整個存成規則（常用格式就是把它勾起來）。

        跟規則頁的其他修改一樣，按「保存並重掃」才真的寫檔、套用到本文。"""
        fmt = self._current_format()
        if fmt is None:
            dialogs.info(self, "先選一種格式", "請先在上方「格式」選擇要存成規則的格式。")
            return
        if fmt.startswith("preset:"):
            preset_id = fmt.split(":", 1)[1]
            self._preset_checks[preset_id].setChecked(True)
            name = self._preset_checks[preset_id].text().split("（")[0]
            i18n.set_text(self.candidate_status, f"已勾選常用格式「{name}」，按「保存並重掃」後生效")
            return
        candidate = next(c for c in self._candidates if c["format"] == fmt)
        rule = weak_candidate_to_user_rule(candidate)
        rule["name"] = candidate["label"]
        if any(existing.get("pattern") == rule["pattern"] for existing in self._working):
            dialogs.info(self, "規則已存在", "相同格式的章節規則已經在清單裡了。")
            return
        self._working.append(rule)
        self._refresh_table()
        matches = len(self._document_matches(rule))
        i18n.set_text(self.candidate_status,
                      f"已加入規則「{rule['name']}」（本文符合 {matches} 行），按「保存並重掃」後生效")

    def _checked_lines_result(self):
        """把勾選的行加上 [::]，回傳（新的整份本文, 要當成卷的行）。"""
        lines = list(self._lines)
        volume_rows = set()
        for index in sorted(self._checked):
            candidate = self._candidates[index]
            row = candidate["index"]
            clean, _marker = strip_persistent_title_marker(lines[row].rstrip())
            lines[row] = clean + "[::]"
            # [::] 只代表「這一行是標題」，層級照格式本身判斷時一律當成章；
            # 卷級格式（例如「卷一 風起」）要另外記下來設成卷。
            if candidate["level"] == 1:
                volume_rows.add(row)
        return lines, volume_rows

    def _add_checked_lines(self):
        if not self._checked:
            dialogs.info(self, "尚未勾選", "請先勾選要加入目錄的行。")
            return
        self.result_lines, self.result_volume_rows = self._checked_lines_result()
        self._commit(ask_about_checked=False)

    # ------------------------------------------------------------------
    # 規則清單

    def _editor_values(self):
        return self.name_input.text(), self.pattern_input.text(), i18n.combo_value(self.level_combo)

    def _refresh_table(self, select: int | None = None):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._working))
        for row, rule in enumerate(self._working):
            enabled_item = QTableWidgetItem()
            enabled_item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            enabled_item.setCheckState(Qt.CheckState.Checked if rule["enabled"] else Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, enabled_item)
            self.table.setItem(row, 1, QTableWidgetItem(rule["name"]))
            self.table.setItem(row, 2, QTableWidgetItem(i18n.T(_LEVEL_LABELS.get(rule["level"], "章"))))
            self.table.setItem(row, 3, QTableWidgetItem(rule["pattern"]))
        self.table.blockSignals(False)
        if select is not None and 0 <= select < self.table.rowCount():
            self.table.selectRow(select)
        self._sync_preset_checks()

    def _on_item_changed(self, item: QTableWidgetItem):
        if item.column() != 0:
            return
        self._working[item.row()]["enabled"] = item.checkState() == Qt.CheckState.Checked
        self._sync_preset_checks()

    def _load_selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        index = rows[0].row()
        self._selected_index = index
        self._update_buttons()
        rule = self._working[index]
        self.name_input.setText(rule["name"])
        self.pattern_input.setText(rule["pattern"])
        i18n.set_combo_value(self.level_combo, _LEVEL_LABELS.get(rule["level"], "章"))

    def _insert_snippet(self, snippet: str):
        """把片段插在正則欄游標的位置，插完游標停在片段後面。"""
        self.pattern_input.insert(snippet)
        self.pattern_input.setFocus()

    def _build_from_sample(self):
        """貼一行真正的章節標題，直接推出規則，不用自己寫正則。"""
        sample = self.sample_input.text().strip()
        if not sample:
            return
        rule = rule_from_sample(sample)
        if rule is None:
            i18n.set_text(self.result_label,
                          "這一行找不到章號（數字或中文數字），無法自動產生規則")
            return
        self._selected_index = None
        self.table.clearSelection()
        self.name_input.setText(rule["name"])
        self.pattern_input.setText(rule["pattern"])
        i18n.set_combo_value(self.level_combo, _LEVEL_LABELS.get(rule["level"], "章"))
        self._update_buttons()
        matches = self._document_matches(rule)
        preview = "\n".join(matches[:3])
        i18n.set_text(self.result_label,
                      f"已依範例產生規則，目前本文符合 {len(matches)} 行\n{preview}\n"
                      "確認沒問題就按「新增規則」")

    def _document_matches(self, rule) -> list:
        matches = []
        for number, line in enumerate(self._lines, 1):
            clean, marker = strip_persistent_title_marker(line.strip())
            if marker != "exclude" and match_user_chapter_rule(clean, [rule]):
                matches.append(f"第 {number} 行：{clean}")
        return matches

    def _validate_pattern(self, show_error: bool):
        pattern = self.pattern_input.text().strip()
        if not pattern:
            i18n.set_text(self.result_label, "正則錯誤：不可空白")
            return None
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as error:
            i18n.set_text(self.result_label, f"正則錯誤：{error}")
            if show_error:
                dialogs.error(self, "正則錯誤", str(error))
            return None
        # 規則會在每次重建目錄時對「每一行」比對一次；巢狀量詞在長段落上會
        # 指數成長，寫錯一次就是之後每次掃描都卡住。不硬擋，但要講清楚。
        if is_risky_pattern(pattern):
            warning = ("⚠ 這個正則有巢狀量詞（例如 (a+)+、(.*)*），在長段落上可能跑很久；"
                       "它會在每次重建目錄時對每一行執行，可能讓程式卡住")
            i18n.set_text(self.result_label, warning)
            if show_error and not dialogs.confirm(self, "正則可能很慢", warning + "。\n\n仍要使用嗎？"):
                return None
            if not show_error:
                return pattern
        i18n.set_text(self.result_label, "正則有效，按「測試目前文件」可以看本文有幾行符合")
        return pattern

    def _add_rule(self):
        """把目前編輯區的內容新增成一條規則（要從頭填寫改用「清空欄位」）。"""
        self._selected_index = None
        self.table.clearSelection()
        if self._save_editor(refresh=True):
            name = self._working[self._selected_index]["name"]
            i18n.set_text(self.result_label, f"已新增規則「{name}」，按「保存並重掃」才會套用到本文")

    def _default_rule_name(self) -> str:
        existing = {rule["name"] for rule in self._working}
        number = len(self._working) + 1
        while f"自訂規則 {number}" in existing:
            number += 1
        return f"自訂規則 {number}"

    def _save_editor(self, refresh: bool) -> bool:
        name = self.name_input.text().strip()
        pattern = self._validate_pattern(show_error=True)
        if pattern is None:
            return False
        if not name:
            # 沒填名稱就自動給一個，不要因為這樣擋住使用者剛寫好的正則。
            name = self._default_rule_name()
            self.name_input.setText(name)
        index = self._selected_index
        enabled = self._working[index].get("enabled", True) if index is not None else True
        rule = {"name": name, "pattern": pattern,
                "level": 1 if i18n.combo_value(self.level_combo) == "卷" else 2, "enabled": enabled}
        if index is not None and "preset" in self._working[index]:
            rule["preset"] = self._working[index]["preset"]   # 改過的常用格式仍對應同一個勾選框
        if index is None:
            self._working.append(rule)
            index = len(self._working) - 1
        else:
            self._working[index] = rule
        self._selected_index = index
        self._refresh_table(select=index)
        self._update_buttons()
        return True

    def _new_rule(self):
        self._selected_index = None
        self.table.clearSelection()
        self.name_input.clear()
        self.pattern_input.clear()
        i18n.set_combo_value(self.level_combo, "章")
        i18n.set_text(self.result_label, "")
        self._update_buttons()

    def _update_buttons(self):
        """沒有選取任何一列時，「儲存變更」與表格操作都沒有對象。"""
        index = self._selected_index
        selected = index is not None
        self.save_button.setEnabled(selected)
        self.delete_button.setEnabled(selected)
        self.move_up_button.setEnabled(selected and index > 0)
        self.move_down_button.setEnabled(selected and index < len(self._working) - 1)
        self.save_button.setToolTip("" if selected else i18n.T("請先在上方清單選取要修改的規則；新的規則請按「新增規則」"))

    def _delete_rule(self):
        index = self._selected_index
        if index is None:
            return
        del self._working[index]
        self._selected_index = None
        self._new_rule()
        self._refresh_table()

    def _move_rule(self, offset: int):
        index = self._selected_index
        if index is None:
            return
        target = index + offset
        if not 0 <= target < len(self._working):
            return
        self._working[index], self._working[target] = self._working[target], self._working[index]
        self._selected_index = target
        self._refresh_table(select=target)
        self._update_buttons()

    def _test_current_document(self):
        pattern = self._validate_pattern(show_error=True)
        if pattern is None:
            return
        probe = {"name": "測試", "pattern": pattern, "enabled": True,
                 "level": 1 if i18n.combo_value(self.level_combo) == "卷" else 2}
        matches = self._document_matches(probe)
        preview = "\n".join(matches[:3])
        i18n.set_text(self.result_label, f"目前本文符合 {len(matches)} 行\n{preview}")

    # ------------------------------------------------------------------

    def _commit(self, ask_about_checked: bool = True):
        # 在第二頁勾了行卻直接按「保存並重掃」：多半是想一起加入，先問清楚，
        # 不要默默丟掉，也不要默默改本文。
        if ask_about_checked and self._checked and self.result_lines is None:
            box = QMessageBox(QMessageBox.Icon.Question, i18n.T("一起加入目錄？"),
                              i18n.T(f"你在「本文可疑章節」勾了 {len(self._checked)} 行，要一起加入目錄嗎？"),
                              parent=self)
            add_button = box.addButton(i18n.T("一起加入"), QMessageBox.ButtonRole.AcceptRole)
            rules_only = box.addButton(i18n.T("只保存規則"), QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(i18n.T("返回"), QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(add_button)
            box.exec()
            clicked = box.clickedButton()
            if clicked is add_button:
                self.result_lines, self.result_volume_rows = self._checked_lines_result()
            elif clicked is not rules_only:
                return
        if not _save_json(RULES_FILE, self._working):
            dialogs.error(self, "無法保存", "章節規則無法寫入設定檔。")
            self.result_lines, self.result_volume_rows = None, set()
            return
        self.result_rules = [dict(rule) for rule in self._working]
        self.accept()
