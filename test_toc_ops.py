"""ui_qt.toc_ops 的純計算部分（不需要開視窗）。"""

from ui_qt import toc_ops


class FakeTree:
    """只實作 toc_ops 會用到的 QTreeWidget 介面。"""

    def __init__(self):
        self.children: dict = {None: []}

    def topLevelItemCount(self):
        return len(self.children[None])

    def topLevelItem(self, index):
        return self.children[None][index]


class FakeItem:
    def __init__(self, name, tree):
        self.name, self.tree = name, tree

    def childCount(self):
        return len(self.tree.children.get(self, []))

    def child(self, index):
        return self.tree.children[self][index]

    def __repr__(self):
        return self.name


def _volume_with_three_chapters():
    tree = FakeTree()
    volume = FakeItem("卷", tree)
    chapters = [FakeItem(f"第{n}章", tree) for n in (1, 2, 3)]
    tree.children = {None: [volume], volume: chapters}
    source = "第一卷 甲\n第1章\n正文一\n第2章\n正文二\n第3章\n正文三\n"
    positions = {volume: 1, chapters[0]: 2, chapters[1]: 4, chapters[2]: 6}
    return tree, volume, chapters, source, positions


def _body_texts(tree, positions, source, selected):
    ranges = toc_ops.selected_body_ranges(tree, positions, dict(positions), source, selected)
    return sorted(source[start:end].strip() for start, end in ranges)


def test_selecting_volume_covers_every_chapter_body():
    tree, volume, _chapters, source, positions = _volume_with_three_chapters()
    assert _body_texts(tree, positions, source, [volume]) == ["正文一", "正文三", "正文二"]


def test_selecting_volume_and_child_keeps_the_other_chapters():
    """父層與子層的範圍會重疊；沒有先合併的話，卷裡其他章會被整段漏掉。"""
    tree, volume, chapters, source, positions = _volume_with_three_chapters()
    assert (_body_texts(tree, positions, source, [volume, chapters[0]])
            == _body_texts(tree, positions, source, [volume]))


def test_selecting_two_separate_chapters():
    tree, _volume, chapters, source, positions = _volume_with_three_chapters()
    assert _body_texts(tree, positions, source, [chapters[0], chapters[2]]) == ["正文一", "正文三"]
