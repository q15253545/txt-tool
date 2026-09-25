"""與元件無關的最小樹狀結構。

只實作章節結構建立過程中用到的幾個操作（insert／get_children／item／
parent），讓辨識規則可以在完全不碰任何 GUI 元件的情況下建出一棵樹，
也能直接在測試裡檢查。介面那一側（ui_qt 的 QTreeWidget）只需要寫一段
「把 SimpleTree 畫進真正元件」的轉接程式，不用重複辨識邏輯本身。
"""

from dataclasses import dataclass


@dataclass
class SimpleTreeNode:
    id: str
    parent: str
    text: str = ""
    open: bool = False


class SimpleTree:
    """節點 id 是不透明字串，呼叫端只拿來當 key，不解讀內容；根節點是空字串。"""

    def __init__(self):
        self._next_id = 0
        self._nodes: dict[str, SimpleTreeNode] = {}
        self._children: dict[str, list[str]] = {"": []}

    def insert(self, parent: str = "", index=None, text: str = "", open: bool = False) -> str:
        node_id = f"n{self._next_id}"
        self._next_id += 1
        self._nodes[node_id] = SimpleTreeNode(id=node_id, parent=parent, text=text, open=open)
        self._children.setdefault(node_id, [])
        self._children.setdefault(parent, []).append(node_id)
        return node_id

    def get_children(self, node: str = "") -> tuple:
        return tuple(self._children.get(node, ()))

    def item(self, node: str, key=None, **kwargs):
        if "text" in kwargs:
            self._nodes[node].text = kwargs["text"]
            return None
        if key == "text":
            return self._nodes[node].text
        if key == "open":
            return self._nodes[node].open
        raise ValueError(f"SimpleTree.item 不支援的呼叫方式：key={key!r} kwargs={kwargs!r}")

    def parent(self, node: str) -> str:
        return self._nodes[node].parent if node else ""

    def reparent(self, node: str, new_parent: str):
        """把節點（連同子孫）搬到 new_parent 底下的最後面。"""
        old_parent = self._nodes[node].parent
        self._children[old_parent].remove(node)
        self._nodes[node].parent = new_parent
        self._children.setdefault(new_parent, []).append(node)

    def sort_children(self, parent: str, key):
        self._children[parent].sort(key=key)

    def walk(self, parent: str = ""):
        """深度優先走訪，依插入順序逐一回傳節點 id。"""
        for child in self.get_children(parent):
            yield child
            yield from self.walk(child)
