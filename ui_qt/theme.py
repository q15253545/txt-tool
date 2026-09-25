"""色彩 token 與樣式表產生。

淺色／深色兩份配色都是從使用者提供的介面設計圖（/theme/light.png、
/theme/dark.png）逐一取色訂出來的，藍色強調色兩個模式共用同一組數值。
UI 元件一律讀 token、不直接寫死色碼，切換主題只是換一份表再重新套用
樣式表。大量留白、柔和圓角、以底色深淺區分層次而不是明顯的框線，這個
方向維持不變。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Tokens:
    bg: str            # 視窗最底層背景
    surface: str        # 卡片／面板背景
    surface_hover: str   # 卡片內可互動元件的 hover 背景
    surface_active: str  # 按下／選取狀態
    border: str          # 極淡的分隔線，只在必要處使用
    text: str            # 主要文字（深灰，不用純黑）
    text_muted: str      # 次要文字／說明文字
    text_faint: str      # 佔位文字、停用狀態
    accent: str          # 強調色（主要按鈕、選取狀態、焦點框）
    accent_hover: str
    accent_text: str     # 強調色按鈕上的文字顏色
    selection_bg: str     # 目錄樹選取列、文字選取背景
    shadow: str           # 卡片陰影顏色（含透明度）
    icon: str             # 圖示線條顏色——跟 text_muted 分開，深色模式要更亮才看得清楚
    icon_hover: str
    warn_bg: str          # 「未完結」這類提醒用徽章的底色
    warn_text: str
    diff_text: str       # 自動修正預覽裡被改掉的字（紅色，深色模式要更亮）
    ok_bg: str            # 「已完結」這類完成狀態徽章的底色
    ok_text: str
    virtual_text: str     # 目錄裡的推定卷（本文沒有卷標題）：醒目但不跟選取色、警告色撞
    find_match_bg: str    # 搜尋命中的底色（其餘筆數）
    find_current_bg: str  # 目前這一筆命中的底色
    marker_text: str      # 顯示中的章節標記（[::] 這類）：看得清楚但不像正文
    marker_bg: str


LIGHT = Tokens(
    bg="#F5F7FA",
    surface="#FFFFFF",
    surface_hover="#F6F9FF",
    surface_active="#F1F5FF",
    border="#E2E7EE",
    text="#243044",
    text_muted="#647084",
    text_faint="#A9B1BE",
    accent="#3869D8",
    accent_hover="#315CBE",
    accent_text="#FFFFFF",
    selection_bg="#EDF3FF",
    shadow="rgba(36, 48, 68, 40)",
    icon="#243044",
    icon_hover="#3869D8",
    warn_bg="#FBE7E7",
    warn_text="#B4383C",
    diff_text="#D92D20",
    ok_bg="#E4F4EA",
    ok_text="#1F7A4C",
    virtual_text="#B45309",
    marker_text="#3869D8",
    marker_bg="#EDF3FF",
    find_match_bg="#FFE9A8",
    find_current_bg="#FFB224",
)

DARK = Tokens(
    bg="#111821",
    surface="#1B2330",
    surface_hover="#202C42",
    surface_active="#23334E",
    border="#344254",
    text="#E6EDF7",
    text_muted="#A4B1C3",
    text_faint="#667486",
    accent="#3869D8",
    accent_hover="#5680DE",
    accent_text="#FFFFFF",
    selection_bg="#243653",
    shadow="rgba(0, 0, 0, 120)",
    icon="#E6EDF7",
    icon_hover="#3869D8",
    warn_bg="#4A2328",
    warn_text="#F0A3A3",
    diff_text="#FF7B72",
    ok_bg="#1D3B2C",
    ok_text="#7FD1A1",
    virtual_text="#F5B455",
    # 深色模式不能直接用 accent：#3869D8 配深藍底幾乎看不見，要更亮才讀得出來。
    marker_text="#8FB4FF",
    marker_bg="#243653",
    find_match_bg="#5A4614",
    find_current_bg="#B9832A",
)


_active_tokens = LIGHT


def set_active_tokens(tokens: Tokens):
    """主視窗套用主題時記下來；對話框自己畫的圖示照這套配色。"""
    global _active_tokens
    _active_tokens = tokens


def active_tokens() -> Tokens:
    return _active_tokens


def build_stylesheet(t: Tokens, chevron_closed_path: str = "", chevron_open_path: str = "",
                     check_path: str = "") -> str:
    """chevron_*_path 是目錄樹展開／收合箭頭的暫存 PNG 路徑（見 icons.icon_file_path）。

    Qt 的 QSS 有個容易踩到的坑：只要對 ::branch 定義任何一條規則，整個
    branch 的繪製就會完全交給樣式表接管，不會再退回原生主題的預設箭頭。
    所以「有子節點」的展開／收合狀態必須自己提供圖片，不然一碰 ::branch
    就會連原生箭頭都不見，使用者完全看不出章節底下還有子章節。
    """
    return f"""
    * {{
        font-family: "Microsoft JhengHei UI", "Segoe UI", sans-serif;
        font-size: 14px;
        color: {t.text};
    }}

    QMainWindow, #centralWidget, QSplitter {{
        background: {t.bg};
    }}

    #headerBar {{
        background: {t.bg};
        border: none;
        border-bottom: 1px solid {t.border};
    }}

    #appTitle {{
        font-size: 15px;
        font-weight: 600;
        color: {t.text};
    }}

    #fileLabel {{
        font-size: 12px;
        color: {t.text_muted};
    }}

    #metadataBar {{
        background: {t.bg};
        border-bottom: 1px solid {t.border};
    }}
    #badge {{
        background: {t.surface};
        border: 1px solid {t.border};
        color: {t.text_muted};
        font-size: 11px;
        padding: 3px 9px;
        border-radius: 8px;
    }}
    #badgeWarn {{
        background: {t.warn_bg};
        color: {t.warn_text};
        font-size: 11px;
        padding: 3px 9px;
        border-radius: 8px;
    }}
    #badgeOk {{
        background: {t.ok_bg};
        color: {t.ok_text};
        font-size: 11px;
        padding: 3px 9px;
        border-radius: 8px;
    }}

    #card {{
        background: {t.surface};
        border: 1px solid {t.border};
        border-radius: 14px;
    }}
    /* 每張卡片上緣的標題列：標題＋該卡片自己的動作圖示，用一條底線跟
       內容區分開，讓三張卡片看起來是同一套結構。 */
    #cardHeader {{
        background: transparent;
        border: none;
        border-bottom: 1px solid {t.border};
    }}
    #cardTitle {{
        font-size: 15px;
        font-weight: 600;
        color: {t.text};
    }}
    #divider {{
        background: {t.border};
        border: none;
    }}
    #cardFooter {{
        background: transparent;
        border: none;
        border-top: 1px solid {t.border};
    }}
    #footerLabel {{
        font-size: 12px;
        color: {t.text_muted};
    }}
    /* 章節管理裡「檢查缺章」的結果區：比卡片底色深一階的小區塊。 */
    #reportPane {{
        background: {t.bg};
        border: 1px solid {t.border};
        border-radius: 10px;
    }}
    #reportTitle {{
        font-size: 12px;
        font-weight: 600;
        color: {t.text_muted};
    }}
    /* 尋找／取代面板（與格式選項共用左側卡片） */
    #findPanel {{
        background: transparent;
    }}
    QListWidget#findResults {{
        background: {t.bg};
        border: 1px solid {t.border};
        border-radius: 10px;
        outline: none;
        padding: 4px;
    }}
    QListWidget#findResults::item {{
        border-radius: 6px;
        margin: 1px 0px;
    }}
    QListWidget#findResults::item:selected {{
        background: {t.selection_bg};
        color: {t.text};
    }}
    #reportBody {{
        font-size: 13px;
        color: {t.text};
        background: transparent;
    }}

    QSplitter::handle {{
        background: transparent;
        width: 10px;
    }}

    QTreeWidget {{
        background: transparent;
        border: none;
        outline: none;
        padding: 6px;
        show-decoration-selected: 0;
    }}

    QTreeWidget::item {{
        padding: 7px 8px;
        border-radius: 8px;
        margin: 1px 0px;
    }}

    QTreeWidget::item:hover {{
        color: {t.accent};
    }}

    QTreeWidget::item:selected {{
        background: {t.selection_bg};
        color: {t.text};
    }}

    QTreeWidget::branch {{
        background: transparent;
        image: none;
        border: none;
    }}
    QTreeWidget::branch:selected {{
        /* 縮排／摺疊箭頭那一格不是章節標題本身，選取色只留給文字那顆圓角
           藥丸，這格要看起來沒被選到。

           這裡一定要指定實際色碼、不能寫 background: transparent——transparent
           會被 Qt 當成「這個屬性沒設定」而退回原生的整列選取填色，畫面上
           就會在縮排欄位多出一塊方角色塊，跟右邊的圓角藥丸接不起來。
           直接填卡片底色才是真的把它蓋掉。 */
        background: {t.surface};
        image: none;
        border: none;
    }}
    QTreeWidget::branch:has-children:closed {{
        image: url({chevron_closed_path});
    }}
    QTreeWidget::branch:has-children:closed:selected {{
        image: url({chevron_closed_path});
    }}
    QTreeWidget::branch:has-children:open {{
        image: url({chevron_open_path});
    }}
    QTreeWidget::branch:has-children:open:selected {{
        image: url({chevron_open_path});
    }}

    QPlainTextEdit {{
        background: transparent;
        border: none;
        padding: 18px 22px;
        selection-background-color: {t.find_current_bg};
        selection-color: {t.text};
    }}

    QScrollBar:vertical {{
        background: transparent;
        width: 12px;
        margin: 4px;
    }}
    QScrollBar::handle:vertical {{
        background: {t.border};
        border-radius: 5px;
        min-height: 32px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {t.text_faint};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
    }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 12px;
        margin: 4px;
    }}
    QScrollBar::handle:horizontal {{
        background: {t.border};
        border-radius: 5px;
        min-width: 32px;
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0px;
    }}

    QToolButton {{
        background: transparent;
        border: none;
        border-radius: 10px;
        padding: 7px;
    }}
    QToolButton:hover {{
        background: {t.surface_hover};
    }}
    QToolButton:pressed {{
        background: {t.surface_active};
    }}
    /* 開關型的圖示按鈕（例如「顯示空格」）開啟中：淡藍底，不只靠圖示變色。 */
    QToolButton:checked {{
        background: {t.selection_bg};
    }}
    QToolButton:disabled {{
        opacity: 0.4;
    }}
    /* 工具列上的按鈕都畫成有框線的方塊，跟設計圖一致：使用者一眼就看得出
       哪些是可以按的區塊，不必靠 hover 才浮出背景。有文字的按鈕是
       QPushButton（圖示＋文字會自動水平、垂直置中），純圖示的是 QToolButton，
       兩種都要涵蓋。 */
    QToolButton#toolbarButton, QPushButton#toolbarButton {{
        background: {t.surface};
        color: {t.text};
        border: 1px solid {t.border};
        border-radius: 10px;
        padding: 8px 14px;
        font-weight: 500;
    }}
    QToolButton#toolbarButton:hover, QPushButton#toolbarButton:hover {{
        background: {t.surface_hover};
        border-color: {t.accent};
    }}
    QToolButton#toolbarButton:pressed, QPushButton#toolbarButton:pressed {{
        background: {t.surface_active};
    }}
    QToolButton#toolbarButton:checked, QPushButton#toolbarButton:checked {{
        background: {t.selection_bg};
        border-color: {t.accent};
        color: {t.accent};
    }}
    QToolButton#toolbarButton:disabled, QPushButton#toolbarButton:disabled {{
        color: {t.text_faint};
        border-color: {t.border};
        background: {t.surface};
    }}
    /* 檔案資訊列上的「書籍資料」展開鈕：跟工具列不同，不畫框、只用文字＋箭頭。 */
    QPushButton#barToggle {{
        background: transparent;
        border: none;
        border-radius: 8px;
        padding: 6px 10px;
    }}
    QPushButton#barToggle:hover {{
        background: {t.surface_hover};
    }}
    QPushButton#barToggle:checked {{
        color: {t.accent};
    }}

    QPushButton {{
        background: {t.surface};
        color: {t.text};
        border: 1px solid {t.border};
        border-radius: 10px;
        padding: 8px 16px;
    }}
    QPushButton:hover {{
        background: {t.surface_hover};
    }}
    QPushButton:pressed {{
        background: {t.surface_active};
    }}
    QPushButton:focus {{
        border: 1px solid {t.accent};
    }}
    QPushButton#primary {{
        background: {t.accent};
        color: {t.accent_text};
        border: 1px solid {t.accent};
        border-radius: 10px;
        padding: 8px 14px;
        font-weight: 500;
    }}
    QPushButton#primary:hover {{
        background: {t.accent_hover};
        border-color: {t.accent_hover};
    }}
    QPushButton#primary:focus {{
        border: 2px solid {t.text};
    }}
    QPushButton#primary:disabled {{
        background: {t.surface};
        border-color: {t.border};
        color: {t.text_faint};
    }}

    QStatusBar {{
        background: {t.bg};
        color: {t.text_muted};
        border-top: 1px solid {t.border};
        font-size: 12px;
        padding: 2px 4px;
    }}
    QStatusBar::item {{
        border: none;
    }}

    QLineEdit {{
        background: {t.surface};
        border: 1px solid {t.border};
        border-radius: 9px;
        padding: 6px 10px;
        selection-background-color: {t.find_current_bg};
        selection-color: {t.text};
    }}
    QLineEdit:focus {{
        border: 1px solid {t.accent};
    }}

    QMenu {{
        background: {t.surface};
        border: 1px solid {t.border};
        border-radius: 10px;
        padding: 6px;
    }}
    QMenu::item {{
        padding: 6px 14px;
        border-radius: 7px;
    }}
    QMenu::item:selected {{
        background: {t.surface_hover};
    }}
    QMenu::item:disabled {{
        color: {t.text_faint};
    }}
    QMenu::separator {{
        height: 1px;
        background: {t.text_faint};
        margin: 6px 10px;
    }}

    QDialog {{
        background: {t.bg};
    }}

    /* 分頁：不套樣式的話 Fusion 會把分頁內容畫成灰黑底，字幾乎看不見。
       分頁本身做成底線式，跟工具列的「開著」狀態同一種強調色。 */
    QTabWidget::pane {{
        background: transparent;
        border: none;
        border-top: 1px solid {t.border};
        top: -1px;
    }}
    QTabBar::tab {{
        background: transparent;
        color: {t.text_muted};
        border: none;
        border-bottom: 2px solid transparent;
        padding: 8px 16px;
        margin-right: 4px;
    }}
    QTabBar::tab:selected {{
        color: {t.accent};
        border-bottom: 2px solid {t.accent};
        font-weight: 600;
    }}
    QTabBar::tab:hover:!selected {{
        color: {t.text};
    }}

    /* 行內的文字連結式按鈕（例如「未收錄 5 行」）：不佔一般按鈕的高度與外框。 */
    QPushButton#inlineLink {{
        background: transparent;
        border: none;
        color: {t.accent};
        padding: 0 4px;
        min-height: 0;
    }}
    QPushButton#inlineLink:hover {{
        color: {t.accent_hover};
        text-decoration: underline;
    }}

    QTableWidget {{
        background: {t.surface};
        gridline-color: {t.border};
        border: 1px solid {t.border};
        border-radius: 10px;
        outline: none;
    }}
    QTableWidget::item {{
        padding: 6px 8px;
        border: none;
    }}
    QTableWidget::item:selected {{
        background: {t.selection_bg};
        color: {t.text};
    }}
    QTableWidget::indicator {{
        width: 16px;
        height: 16px;
    }}
    QTableWidget::indicator:unchecked {{
        border: 1.5px solid {t.border};
        border-radius: 4px;
        background: {t.surface};
    }}
    QTableWidget::indicator:checked {{
        border: 1.5px solid {t.accent};
        border-radius: 4px;
        background: {t.accent};
        image: url({check_path});
    }}
    QHeaderView::section {{
        background: {t.surface};
        color: {t.text_muted};
        border: none;
        border-bottom: 1px solid {t.border};
        border-right: 1px solid {t.border};
        padding: 6px 8px;
        font-weight: 600;
    }}
    QTableCornerButton::section {{
        background: {t.surface};
        border: none;
    }}

    QCheckBox {{
        spacing: 8px;
    }}
    QCheckBox::indicator {{
        width: 17px;
        height: 17px;
        border-radius: 5px;
        border: 1.5px solid {t.border};
        background: {t.surface};
    }}
    QCheckBox::indicator:hover {{
        border-color: {t.accent};
    }}
    QCheckBox::indicator:checked {{
        background: {t.accent};
        border-color: {t.accent};
        image: url({check_path});
    }}

    QComboBox {{
        /* combobox-popup: 0 讓選單從下拉框正下方展開；Fusion 預設的彈出方式
           會把選單疊在下拉框上面、蓋住目前選項，還會壓到上方的欄位標籤。 */
        combobox-popup: 0;
        background: {t.surface};
        border: 1px solid {t.border};
        border-radius: 9px;
        padding: 6px 30px 6px 10px;
        min-height: 22px;
    }}
    QComboBox:hover {{
        border-color: {t.accent};
    }}
    QComboBox:focus {{
        border: 1px solid {t.accent};
    }}
    QComboBox:disabled {{
        color: {t.text_faint};
    }}
    /* 只要改了 ::drop-down，Qt 就不再畫原生的下拉箭頭（跟目錄 ::branch 同一個
       坑），箭頭圖片必須自己給，不然下拉框看起來跟一般輸入框一模一樣。 */
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: center right;
        border: none;
        width: 28px;
    }}
    QComboBox::down-arrow {{
        image: url({chevron_open_path});
        width: 12px;
        height: 12px;
    }}
    QComboBox QAbstractItemView {{
        background: {t.surface};
        border: 1px solid {t.border};
        padding: 4px;
        outline: 0;
    }}
    QComboBox QAbstractItemView::item {{
        min-height: 30px;
        padding: 0px 10px;
        border: none;
        border-radius: 6px;
        color: {t.text};
    }}
    QComboBox QAbstractItemView::item:hover {{
        background: {t.surface_hover};
    }}
    QComboBox QAbstractItemView::item:selected {{
        background: {t.selection_bg};
        color: {t.accent};
    }}

    #panelScroll, #panelScrollContent {{
        background: transparent;
        border: none;
    }}

    #findBar {{
        background: {t.surface};
        border: 1px solid {t.border};
        border-radius: 12px;
    }}
    QListWidget#findResults {{
        background: transparent;
        border: 1px solid {t.border};
        border-radius: 10px;
        padding: 4px;
        outline: none;
    }}
    QListWidget#findResults::item {{
        padding: 6px 8px;
        border-radius: 7px;
        margin: 1px 0px;
    }}
    QListWidget#findResults::item:hover {{
        background: {t.surface_hover};
    }}
    QListWidget#findResults::item:selected {{
        background: {t.selection_bg};
        color: {t.text};
    }}

    QToolTip {{
        background: {t.text};
        color: {t.bg};
        border: none;
        border-radius: 6px;
        padding: 4px 8px;
    }}
    """
