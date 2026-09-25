"""可以點標題列排序的表格（掃描廣告、引號檢查、本文可疑章節共用）。

「自訂章節規則」的規則清單刻意不用：那張表的順序就是規則的套用優先順序。

點欄位標題依序切換：遞增 → 遞減 → 回到原本順序（文件中的順序）。
第三下回到原本順序很重要：這幾張表的預設順序本身就有意義，排過之後要
回得去。

排序後「第幾列」就不再等於「第幾筆資料」，所以每一列的每一格都存著它在
原始清單裡的索引（INDEX_ROLE）；呼叫端一律用 data_index()／row_of_index()
換算，不能再直接拿 row 去索引資料。

不用 QTableWidget.setSortingEnabled(True)：開著它的時候，程式每填一格表格
就會被重新排序一次，逐列填資料會亂掉；而且它只有遞增／遞減兩種狀態。
"""

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QHeaderView, QTableWidget, QTableWidgetItem

INDEX_ROLE = int(Qt.ItemDataRole.UserRole) + 100
SORT_ROLE = int(Qt.ItemDataRole.UserRole) + 101
_STATE = "_sortState"   # 表格上的 dynamic property：(欄位, 遞增?) 或 None

# 信心欄位要照「高 → 中 → 低」排，不是照字碼。
CONFIDENCE_ORDER = {"高": 0, "中": 1, "低": 2}


class SortableItem(QTableWidgetItem):
    def __lt__(self, other):
        table = self.tableWidget()
        if table is not None and table.property(_STATE) is None:
            return (self.data(INDEX_ROLE) or 0) < (other.data(INDEX_ROLE) or 0)
        mine, theirs = self.data(SORT_ROLE), other.data(SORT_ROLE)
        if mine is not None and theirs is not None and mine != theirs:
            return mine < theirs
        if self.flags() & Qt.ItemFlag.ItemIsUserCheckable and not self.text():
            # 只有勾選框的欄位：依勾選狀態排（未勾在前）。
            if self.checkState() != other.checkState():
                return self.checkState().value < other.checkState().value
        elif self.text() != other.text():
            return self.text() < other.text()
        # 相同的值保持原本的相對順序。
        return (self.data(INDEX_ROLE) or 0) < (other.data(INDEX_ROLE) or 0)


def make_item(text: str = "", index: int = 0, sort_key=None) -> SortableItem:
    item = SortableItem(text)
    item.setData(INDEX_ROLE, index)
    if sort_key is not None:
        item.setData(SORT_ROLE, sort_key)
    return item


# 欄寬初始值：「contents」照內容寬度（有上限，免得一格長文字把表格撐爆）；
# 小數＝表格寬度的比例；整數＝固定像素。
_CONTENT_WIDTH_LIMIT = 320
MIN_COLUMN_WIDTH = 44


class _InitialColumnWidths(QObject):
    """表格第一次顯示時才算欄寬（這時才知道資料與表格寬度），之後不再動，
    使用者拉過的寬度才不會被重新整理蓋掉。"""

    def __init__(self, table, widths):
        super().__init__(table)
        self._widths = widths
        self._done = False

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Show and not self._done:
            self._done = True
            available = watched.viewport().width()
            for column, width in self._widths.items():
                if width == "contents":
                    watched.resizeColumnToContents(column)
                    watched.setColumnWidth(column, min(watched.columnWidth(column), _CONTENT_WIDTH_LIMIT))
                elif isinstance(width, float):
                    watched.setColumnWidth(column, max(MIN_COLUMN_WIDTH, int(available * width)))
                else:
                    watched.setColumnWidth(column, width)
        return False


def setup_columns(table: QTableWidget, widths: dict):
    """每一欄都可以拖拉調整寬度、畫出格線，最後一欄吃掉剩下的空間。

    原本各欄是「照內容」或「平均撐滿」，使用者完全拉不動；長內容的欄位看不完
    也沒辦法。widths 是各欄的初始寬度（最後一欄不用給）。"""
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setStretchLastSection(True)
    header.setMinimumSectionSize(MIN_COLUMN_WIDTH)
    table.setShowGrid(True)
    table.setWordWrap(False)
    table.installEventFilter(_InitialColumnWidths(table, widths))


def enable_sorting(table: QTableWidget, on_sorted=None):
    """on_sorted：排序狀態改變之後要呼叫的函式（例如更新按鈕是否可用）。"""
    header = table.horizontalHeader()
    header.setSectionsClickable(True)
    header.setSortIndicatorShown(False)
    table.setProperty(_STATE, None)

    def on_clicked(column: int):
        state = table.property(_STATE)
        if state is None or state[0] != column:
            state = (column, True)
        elif state[1]:
            state = (column, False)
        else:
            state = None
        table.setProperty(_STATE, state)
        resort(table)
        if on_sorted is not None:
            on_sorted()

    header.sectionClicked.connect(on_clicked)


def is_sorted(table: QTableWidget) -> bool:
    return table.property(_STATE) is not None


def reset_sort(table: QTableWidget):
    table.setProperty(_STATE, None)
    resort(table)


def resort(table: QTableWidget):
    """依目前的排序狀態重排（重新填完表格之後也要呼叫）。"""
    state = table.property(_STATE)
    header = table.horizontalHeader()
    blocked = table.blockSignals(True)
    try:
        if state is None:
            header.setSortIndicatorShown(False)
            table.sortItems(0, Qt.SortOrder.AscendingOrder)   # 無狀態時 __lt__ 依原始索引比較
        else:
            column, ascending = state
            order = Qt.SortOrder.AscendingOrder if ascending else Qt.SortOrder.DescendingOrder
            header.setSortIndicatorShown(True)
            header.setSortIndicator(column, order)
            table.sortItems(column, order)
    finally:
        table.blockSignals(blocked)


def data_index(table: QTableWidget, row: int) -> int:
    item = table.item(row, 0)
    return int(item.data(INDEX_ROLE)) if item is not None and item.data(INDEX_ROLE) is not None else row


def row_of_index(table: QTableWidget, index: int) -> int:
    for row in range(table.rowCount()):
        if data_index(table, row) == index:
            return row
    return -1
