"""Python 字元位置 ↔ Qt 文件位置的換算。

Python 的字串位置以「字元」計算，Qt 的 QTextCursor 位置以 UTF-16 的單位
計算。基本多文種平面以外的字元（emoji、擴充漢字如 𠀀）在 Python 算 1、在
Qt 算 2，所以只要文件前面出現過一個這種字，後面每一個位置都會差 1 格：
搜尋到的「甲」會取代到前一個字，修復換行會切在字的中間。

一般小說完全沒有這種字，這時候 needed 是 False，換算直接原樣回傳，不會有
任何額外成本。真的有的時候才建立索引表，用二分搜尋換算。
"""

import re
from bisect import bisect_left

_ASTRAL = re.compile(r"[\U00010000-\U0010FFFF]")


class PositionMap:
    """建立一次、重複使用；文字一變就要重建（呼叫端用文字版本判斷）。"""

    def __init__(self, text: str):
        # 先用 C 層級的快速判斷篩掉絕大多數文件，再做一次正則掃描取得位置。
        if text.isascii() or len(text.encode("utf-16-le", "surrogatepass")) == len(text) * 2:
            self._wide: list[int] = []
        else:
            self._wide = [match.start() for match in _ASTRAL.finditer(text)]

    @property
    def needed(self) -> bool:
        """False 代表這份文字裡兩種位置完全一致。"""
        return bool(self._wide)

    def to_qt(self, position: int) -> int:
        if not self._wide:
            return position
        return position + bisect_left(self._wide, position)

    def to_python(self, position: int) -> int:
        if not self._wide:
            return position
        low, high = 0, position
        while low < high:                      # 找出最小的 python 位置使 to_qt(p) >= position
            middle = (low + high) // 2
            if self.to_qt(middle) < position:
                low = middle + 1
            else:
                high = middle
        return low
