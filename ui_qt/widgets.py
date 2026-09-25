"""共用的小型元件：圓角卡片容器、依主題與 hover 狀態換色的圖示按鈕、
攔截 Ctrl+Z/Ctrl+Shift+Z 交給自訂復原系統的編輯器、繁／簡切換鈕。"""

from PySide6.QtCore import (
    QEasingCurve, QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor, QFont, QGuiApplication, QKeySequence, QPainter, QPainterPath, QPen,
)
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QLayout, QPlainTextEdit, QPushButton, QSizePolicy,
    QStyledItemDelegate, QToolButton, QWidget,
)

from . import i18n, icons


class FlowLayout(QLayout):
    """由左到右排，排不下就換行。

    固定格線（例如一列 4 個）在寬視窗會在右邊留一大塊空白、在窄視窗又會
    撐出邊界；這個版面照實際寬度決定一列放幾個。uniform=True 時每一格都用
    最寬那一項的寬度，排出來仍然像對齊的表格，只是欄數會跟著寬度變。"""

    def __init__(self, parent=None, h_spacing: int = 18, v_spacing: int = 6, uniform: bool = False):
        super().__init__(parent)
        self._items: list = []
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self._uniform = uniform
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._visible_items():
            size = size.expandedTo(item.minimumSize())
        if self._uniform:
            size.setWidth(max(size.width(), self._cell_width()))
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _visible_items(self):
        return [item for item in self._items if not item.isEmpty()]

    def _cell_width(self) -> int:
        return max((item.sizeHint().width() for item in self._visible_items()), default=0)

    def _do_layout(self, rect, test_only: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, line_height = area.x(), area.y(), 0
        cell = self._cell_width() if self._uniform else 0
        for item in self._visible_items():
            hint = item.sizeHint()
            width = cell or hint.width()
            if line_height and x + width > area.right() + 1:
                x = area.x()
                y += line_height + self._v_spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), QSize(width, hint.height())))
            x += width + self._h_spacing
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


def flow_container(uniform: bool = False, h_spacing: int = 18, v_spacing: int = 6):
    """回傳（放在一般版面裡的 QWidget, 往裡面加元件用的 FlowLayout）。

    包一層 QWidget 是為了讓外層的 QVBoxLayout 照「這個寬度需要幾列」給高度。"""
    widget = QWidget()
    layout = FlowLayout(widget, h_spacing=h_spacing, v_spacing=v_spacing, uniform=uniform)
    policy = widget.sizePolicy()
    policy.setHeightForWidth(True)
    widget.setSizePolicy(policy)
    return widget, layout


def fit_window_to_screen(window):
    """把視窗的大小與位置夾回它「現在所在的螢幕」的可用範圍（含標題列）。"""
    screen = window.screen() or QGuiApplication.primaryScreen()
    if screen is None:
        return
    available = screen.availableGeometry()
    frame = window.frameGeometry()
    extra_width = frame.width() - window.width()
    extra_height = frame.height() - window.height()
    width = max(window.minimumWidth(), min(window.width(), available.width() - extra_width))
    height = max(window.minimumHeight(), min(window.height(), available.height() - extra_height))
    if (width, height) != (window.width(), window.height()):
        window.resize(width, height)
        frame = window.frameGeometry()
    x = min(max(frame.x(), available.left()), max(available.left(), available.right() - frame.width() + 1))
    y = min(max(frame.y(), available.top()), max(available.top(), available.bottom() - frame.height() + 1))
    if (x, y) != (frame.x(), frame.y()):
        window.move(x, y)


class _KeepOnScreen(QObject):
    """對話框顯示出來之後，照它實際所在的螢幕再夾一次。

    對話框還沒顯示時，Qt 回報的螢幕是「主螢幕」，不是主視窗所在的那一台；
    主視窗在直立螢幕上時，照主螢幕算出來的大小會整個超出去（實際發生過）。
    所以一定要等顯示之後、位置確定了再量一次。"""

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Show:
            QTimer.singleShot(0, lambda: fit_window_to_screen(watched))
        return False


def keep_on_screen(dialog):
    guard = _KeepOnScreen(dialog)
    dialog.installEventFilter(guard)
    dialog._keep_on_screen = guard     # 留住參照，免得被回收


def size_dialog(dialog, width: int, height: int):
    """依對話框想要的大小開，但不超過螢幕可用範圍的九成。

    螢幕用「主視窗所在的那一台」算（對話框自己還沒顯示，問不準）；顯示之後
    再照實際位置夾一次（keep_on_screen）。直立或小螢幕上，寫死的 900×640
    會有一截在畫面外，而且對話框沒有工作列可以把它拖回來。"""
    parent = dialog.parentWidget()
    screen = (parent.window().screen() if parent is not None else None) or dialog.screen() \
        or QGuiApplication.primaryScreen()
    if screen is not None:
        available = screen.availableGeometry()
        width = min(width, int(available.width() * 0.9))
        height = min(height, int(available.height() * 0.9))
    dialog.resize(width, height)
    keep_on_screen(dialog)


class Card(QFrame):
    """帶圓角、細邊框的卡片容器，取代明顯的分隔線。

    刻意不用 QGraphicsDropShadowEffect：那會把卡片整個子樹（含裡面的文字）
    丟進離屏緩衝區做合成，圓角外側沒蓋到的方形區域會被當成不透明範圍去
    投影，變成圓角後面露出方形陰影；文字也會因為多一層合成而變得毛邊。
    只靠背景色深淺加一條極淡的邊框，就足夠跟底色分出層次了。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)


class AppWidgetPolisher(QObject):
    """裝在 QApplication 上，每個元件套完樣式表（Polish 事件）之後補兩件事。
    整個程式（含之後才開的對話框、右鍵選單、下拉選單）都要處理，所以用
    全域事件過濾器，不必在每個建立元件的地方各改一次。

    1. 字型完整微調（hinting）：中文字筆畫才會對齊像素、不糊。只在程式
       啟動時設定 app 字型不夠——樣式表在 Polish 之後還會再發一次字型變更、
       重新計算字型，這個設定就被丟掉，選單、下拉清單、狀態列會退回模糊的
       預設值（實機逐一追過事件）。所以 FontChange 也要攔：只在不是完整
       微調時才補設，補設後的那次 FontChange 就不會再動作，不會無限循環。
    2. 下拉框改用 QStyledItemDelegate：預設的 delegate 會忽略樣式表的
       ::item 規則（列高、圓角、hover 底色都吃不到），選單就只剩系統原生
       那種擠在一起、目前項目外面套一個黑框的樣子。
    3. 下拉框的最小寬度不再由最長的選項決定：否則卡片拉到最窄時，下拉框
       撐不下去就會連同整個面板內容一起超出卡片邊界。放不下時 Qt 會自動
       截斷顯示中的文字，展開的清單仍然完整。"""

    _HINTING = QFont.HintingPreference.PreferFullHinting
    _FONT_EVENTS = (QEvent.Type.Polish, QEvent.Type.FontChange)

    def eventFilter(self, watched, event):
        # 4. 介面切成簡體時，之後才打開的對話框、右鍵選單在顯示前轉換文字。
        if (event.type() == QEvent.Type.Show and i18n.is_simplified()
                and isinstance(watched, QWidget) and watched.isWindow()):
            i18n.retranslate(watched)
        if event.type() in self._FONT_EVENTS and isinstance(watched, QWidget):
            font = watched.font()
            if font.hintingPreference() != self._HINTING:
                font.setHintingPreference(self._HINTING)
                watched.setFont(font)
        if event.type() == QEvent.Type.Polish and isinstance(watched, QComboBox):
            if not watched.property("styledPopup"):
                watched.setProperty("styledPopup", True)
                watched.setItemDelegate(QStyledItemDelegate(watched))
                watched.setSizeAdjustPolicy(
                    QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                watched.setMinimumContentsLength(3)
        return False


# 三張卡片（格式選項／章節管理、目錄、本文）的標題列一律同一個高度：
# 以沒有圖示按鈕的「本文」為準，剛好容得下目錄那排 30px 高的圖示按鈕。
CARD_HEADER_HEIGHT = 44


def make_card_header(title_text: str, with_stretch: bool = True):
    """回傳（標題列 widget, 可以繼續往右塞按鈕的 layout）。

    with_stretch=False 是給「自己要放一個會撐滿剩餘空間的元件」的呼叫端用的
    （本文卡片的麵包屑）：留著這裡的彈簧的話，兩者會平分空間。"""
    header = QWidget()
    header.setObjectName("cardHeader")
    header.setFixedHeight(CARD_HEADER_HEIGHT)
    layout = QHBoxLayout(header)
    layout.setContentsMargins(16, 0, 10, 0)
    layout.setSpacing(4)
    title = QLabel(title_text)
    title.setObjectName("cardTitle")
    layout.addWidget(title)
    if with_stretch:
        layout.addStretch(1)
    return header, layout


class ElidedLabel(QLabel):
    """長文字用「…」截掉，而且絕對不會把版面撐寬。

    本文卡片的麵包屑會放整條「卷 / 章」路徑，遇到很長的標題時，一般的
    QLabel 會把這串文字的寬度回報成自己的最小寬度，整張卡片跟著被撐開、
    擠掉旁邊的目錄；切到別章時卡片寬度還會跳來跳去。這裡把水平尺寸策略
    設成 Ignored（完全不理會文字寬度），文字則照目前實際寬度截斷。"""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._full_text = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, text: str):
        self._full_text = text
        self.setToolTip(text)
        self._apply_elide()

    def text(self) -> str:
        return self._full_text

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_elide()

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())

    def _apply_elide(self):
        metrics = self.fontMetrics()
        super().setText(metrics.elidedText(self._full_text, Qt.TextElideMode.ElideRight,
                                           max(0, self.width())))


class Divider(QFrame):
    """1px 分隔線，顏色吃主題的 border token。

    不用 QFrame 的 HLine：HLine 是 Qt 自己用調色盤畫的陰影線，樣式表
    改不到它的顏色，畫出來會比卡片邊框、標題列底線深一截。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("divider")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(1)


class ClickableLabel(QLabel):
    """可以按兩下的標籤（例如本文右下角的縮放比例：按兩下回到 100%）。"""

    double_clicked = Signal()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class IconButton(QToolButton):
    """圖示按鈕：一般／hover／停用三種狀態各自有對應的線條顏色；
    checkable 時「開啟中」改用 active 顏色。"""

    def __init__(self, icon_name: str, tooltip: str, *, size: int = 20, parent=None):
        super().__init__(parent)
        self._icon_name = icon_name
        self._size = size
        self._color = "#000000"
        self._hover_color = "#000000"
        self._disabled_color = "#000000"
        self._active_color = None
        self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIconSize(QSize(size, size))
        self.toggled.connect(lambda _checked: self._refresh_icon(hovering=self.underMouse()))

    def set_icon_name(self, icon_name: str):
        self._icon_name = icon_name
        self._refresh_icon(hovering=self.underMouse())

    def set_colors(self, color: str, hover_color: str, disabled_color: str, active_color: str | None = None):
        self._color = color
        self._hover_color = hover_color
        self._disabled_color = disabled_color
        self._active_color = active_color
        self._refresh_icon(hovering=self.underMouse())

    def _refresh_icon(self, hovering: bool):
        if not self.isEnabled():
            color = self._disabled_color
        elif self.isChecked() and self._active_color:
            color = self._active_color
        else:
            color = self._hover_color if hovering else self._color
        self.setIcon(icons.make_icon(self._icon_name, color, self._size))

    def setEnabled(self, enabled: bool):
        super().setEnabled(enabled)
        self._refresh_icon(hovering=False)

    def enterEvent(self, event):
        self._refresh_icon(hovering=True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._refresh_icon(hovering=False)
        super().leaveEvent(event)


class IconTextButton(QPushButton):
    """圖示＋文字的按鈕，用在需要比純圖示更容易辨識的動作——尤其是切換
    側邊面板的按鈕：純圖示配 tooltip 的按鈕使用者要 hover 才知道是什麼，
    這裡改成文字直接寫在按鈕上，並支援 checkable 的「目前開啟中」樣式。

    用 QPushButton 而不是 QToolButton：QToolButton 的「文字在圖示旁」模式
    會把圖示＋文字靠左排，按鈕比內容寬時右邊空一截；QPushButton 會把
    整組內容水平、垂直都置中。不接受鍵盤焦點：點工具列按鈕不該把焦點
    從本文搶走，也不會因為取得焦點而多出一圈焦點框。"""

    def __init__(self, icon_name: str, text: str, *, checkable: bool = False,
                 size: int = 18, parent=None):
        super().__init__(text, parent)
        self.setObjectName("toolbarButton")
        # 視窗太窄時只顯示圖示（見 set_compact）。文字仍然記在 _label 裡，
        # text() 回傳的一律是完整文字，繁簡切換才有東西可以轉。
        self._label = text
        self._compact = False
        self._icon_name = icon_name
        self._size = size
        self._color = "#000000"
        self._hover_color = "#000000"
        self._active_color = "#000000"
        self.setCheckable(checkable)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIconSize(QSize(size, size))

    def setText(self, text: str):
        self._label = text
        super().setText("" if self._compact else text)

    def text(self) -> str:
        return self._label

    def set_compact(self, compact: bool):
        """只顯示圖示／圖示加文字。功能與提示文字都不變。"""
        if compact == self._compact:
            return
        self._compact = compact
        super().setText("" if compact else self._label)

    def set_icon_name(self, icon_name: str):
        self._icon_name = icon_name
        self._refresh_icon()

    def set_colors(self, color: str, hover_color: str, active_color: str):
        self._color = color
        self._hover_color = hover_color
        self._active_color = active_color
        self._refresh_icon()

    def _refresh_icon(self):
        color = self._active_color if self.isChecked() else (
            self._hover_color if self.underMouse() else self._color)
        self.setIcon(icons.make_icon(self._icon_name, color, self._size))

    def setChecked(self, checked: bool):
        super().setChecked(checked)
        self._refresh_icon()

    def enterEvent(self, event):
        self._refresh_icon()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._refresh_icon()
        super().leaveEvent(event)


class Editor(QPlainTextEdit):
    """章節結構（force_lv1/2、忽略集合…）跟正文綁在一起，Qt 內建的
    QTextDocument undo 只認得文字、不認得這些——所以 Ctrl+Z／Ctrl+Shift+Z
    在這裡整組攔截下來，交給 MainWindow 自己的快照式復原系統處理，
    不落回 QPlainTextEdit 內建（只復原文字、會讓結構跟正文脫節）的版本。"""

    undo_requested = Signal()
    redo_requested = Signal()
    # Ctrl＋滾輪調整預覽字級：+1 放大、-1 縮小；Ctrl+0 送 0 代表回到 100%。
    zoom_requested = Signal(int)
    # 把 TXT 檔拖進本文：交給主視窗開檔，而不是把檔案路徑當文字插進本文。
    file_dropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Qt 自己的復原堆疊完全用不到（復原走 MainWindow 的快照系統），但它
        # 預設開著，每次編輯、每次套標題格式都會再存一份；實測開一個 5.8MB
        # 的檔就累積上百萬步，記憶體白白被吃掉。
        self.setUndoRedoEnabled(False)
        self._wheel_accum = 0
        self._show_whitespace = False
        self._whitespace_color = QColor("#A9B1BE")
        self._trailing_color = QColor(180, 56, 60, 40)

    # --- 拖曳開檔 -------------------------------------------------------

    @staticmethod
    def _dropped_files(event):
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        return [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]

    def dragEnterEvent(self, event):
        if self._dropped_files(event):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self._dropped_files(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        files = self._dropped_files(event)
        if files:
            event.acceptProposedAction()
            self.file_dropped.emit(files[0])
            return
        super().dropEvent(event)

    # --- 顯示空格 -------------------------------------------------------

    def set_show_whitespace(self, enabled: bool):
        self._show_whitespace = enabled
        self.viewport().update()

    def set_whitespace_colors(self, mark: str, trailing: QColor):
        self._whitespace_color = QColor(mark)
        self._trailing_color = QColor(trailing)
        if self._show_whitespace:
            self.viewport().update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._show_whitespace:
            self._paint_whitespace(event.rect())

    def _paint_whitespace(self, clip):
        """把空白字元畫出來：半形空格「·」、全形空格「□」、Tab「→」，行尾
        多餘的空白再鋪一層淡紅底。Qt 內建的 ShowTabsAndSpaces 看不到全形
        空格，而小說縮排幾乎都是全形空格，所以自己畫；只畫畫面上看得到的
        幾行，檔案再大也不影響捲動速度。"""
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(self._whitespace_color)
        pen.setWidthF(1.0)
        offset = self.contentOffset()
        block = self.firstVisibleBlock()
        bottom = clip.bottom()
        while block.isValid():
            geometry = self.blockBoundingGeometry(block).translated(offset)
            if geometry.top() > bottom:
                break
            if block.isVisible() and geometry.bottom() >= clip.top():
                self._paint_block_whitespace(painter, pen, block, geometry.topLeft(), clip)
            block = block.next()
        painter.end()

    def _paint_block_whitespace(self, painter, pen, block, origin, clip):
        """一個段落可能折成很多視覺行；只處理跟畫面相交的那幾行。

        整段掃描在正常小說看不出差別，但硬換行整理後常出現好幾萬字的單一
        段落，那時每次重繪都要掃完整段（還要問 Qt 每個空白的座標）。"""
        text = block.text()
        if not text or not any(ch in text for ch in " \t\u3000\u00a0"):
            return
        layout = block.layout()
        stripped = len(text.rstrip(" \t\u3000\u00a0"))
        for line_index in range(layout.lineCount()):
            line = layout.lineAt(line_index)
            top = origin.y() + line.y()
            if top + line.height() < clip.top():
                continue
            if top > clip.bottom():
                break
            self._paint_line_whitespace(painter, pen, line, text, stripped, origin)

    def _paint_line_whitespace(self, painter, pen, line, text, stripped, origin):
        start = line.textStart()
        for index in range(start, start + line.textLength()):
            char = text[index] if index < len(text) else ""
            if char not in " \t\u3000\u00a0":
                continue
            left = line.cursorToX(index)[0]
            right = line.cursorToX(index + 1)[0]
            width = right - left
            # 行尾隱藏標記前面的空白被縮成 1px，畫出來只會是雜訊。
            if width < 3:
                continue
            top = origin.y() + line.y()
            height = line.height()
            rect = QRectF(origin.x() + left, top, width, height)
            if index >= stripped:
                painter.fillRect(rect, self._trailing_color)
            center = QPointF(rect.center().x(), top + line.ascent() - line.ascent() * 0.36)
            painter.setPen(pen)
            if char == "\u3000":
                side = min(width * 0.5, line.ascent() * 0.5)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(QRectF(center.x() - side / 2, center.y() - side / 2, side, side), 1.5, 1.5)
            elif char == "\t":
                arrow = QPainterPath()
                arrow.moveTo(rect.left() + 3, center.y())
                arrow.lineTo(rect.right() - 3, center.y())
                arrow.moveTo(rect.right() - 7, center.y() - 3.5)
                arrow.lineTo(rect.right() - 3, center.y())
                arrow.lineTo(rect.right() - 7, center.y() + 3.5)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(arrow)
            else:
                painter.setBrush(self._whitespace_color)
                painter.drawEllipse(center, 1.4, 1.4)

    def wheelEvent(self, event):
        if not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            super().wheelEvent(event)
            return
        # 觸控板一次只送一小段角度，累積到一格（120）才算一步，
        # 不然輕輕一滑就會跳好幾級。
        self._wheel_accum += event.angleDelta().y()
        while abs(self._wheel_accum) >= 120:
            step = 1 if self._wheel_accum > 0 else -1
            self._wheel_accum -= 120 * step
            self.zoom_requested.emit(step)
        event.accept()

    def keyPressEvent(self, event):
        if (event.key() == Qt.Key.Key_0
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.zoom_requested.emit(0)
            return
        if event.matches(QKeySequence.StandardKey.Undo):
            self.undo_requested.emit()
            return
        if event.matches(QKeySequence.StandardKey.Redo):
            self.redo_requested.emit()
            return
        super().keyPressEvent(event)


class LanguageToggle(QWidget):
    """介面文字「繁／简」切換：膠囊形底座上兩個選項，選中的那一邊墊一塊
    白色圓角方塊（依 /icon/繁簡切換 設計稿）。按哪一邊就切到哪一邊，方塊
    滑過去有一小段動畫。字用字型畫，不用設計稿的向量外框，高解析度螢幕
    上筆畫才會跟其他介面文字一樣銳利。"""

    toggled = Signal(bool)   # True＝簡體

    _LABELS = ("繁", "简")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._simplified = False
        self._position = 0.0   # 0＝方塊在「繁」，1＝在「简」
        self._colors = {}
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(80, 36)
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(160)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._on_animation)

    def set_colors(self, track: str, knob: str, active_text: str, inactive_text: str, shadow: str):
        self._colors = {"track": QColor(track), "knob": QColor(knob), "active": QColor(active_text),
                        "inactive": QColor(inactive_text), "shadow": QColor(shadow)}
        self.update()

    def is_simplified(self) -> bool:
        return self._simplified

    def set_simplified(self, value: bool, *, animate: bool = False):
        value = bool(value)
        if value == self._simplified and not animate:
            self._position = 1.0 if value else 0.0
            self.update()
            return
        self._simplified = value
        target = 1.0 if value else 0.0
        if animate:
            self._animation.stop()
            self._animation.setStartValue(self._position)
            self._animation.setEndValue(target)
            self._animation.start()
        else:
            self._position = target
            self.update()

    def _on_animation(self, value):
        self._position = float(value)
        self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self.isEnabled():
            return
        wanted = event.position().x() >= self.width() / 2
        if wanted != self._simplified:
            self.set_simplified(wanted, animate=True)
            self.toggled.emit(wanted)
        event.accept()

    def paintEvent(self, _event):
        if not self._colors:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width, height = self.width(), self.height()
        scale = height / 52.0
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._colors["track"])
        painter.drawRoundedRect(QRectF(0, 0, width, height), 14 * scale, 14 * scale)

        inset_x, inset_y = 8 * scale, 7 * scale
        segment = (width - 2 * inset_x) / 2
        knob = QRectF(inset_x + segment * self._position, inset_y, segment, height - 2 * inset_y)
        if self.isEnabled():
            shadow = QColor(self._colors["shadow"])
            painter.setBrush(shadow)
            painter.drawRoundedRect(knob.translated(0, 1), 8 * scale, 8 * scale)
            painter.setBrush(self._colors["knob"])
            painter.drawRoundedRect(knob, 8 * scale, 8 * scale)

        font = QFont(self.font())
        font.setPixelSize(max(12, round(height * 0.39)))
        font.setWeight(QFont.Weight.DemiBold)
        font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
        painter.setFont(font)
        for index, label in enumerate(self._LABELS):
            rect = QRectF(inset_x + segment * index, 0, segment, height)
            nearness = 1.0 - abs(self._position - index)
            color = QColor(self._colors["inactive"])
            active = self._colors["active"]
            color.setRedF(color.redF() + (active.redF() - color.redF()) * nearness)
            color.setGreenF(color.greenF() + (active.greenF() - color.greenF()) * nearness)
            color.setBlueF(color.blueF() + (active.blueF() - color.blueF()) * nearness)
            painter.setPen(color)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)
        painter.end()
