"""目錄章節範圍計算：合併／整理／連續編號／刪除共用的邊界邏輯。

範圍一律以目錄上的標題行為邊界：一個節點的範圍＝從它自己的標題行，到下一個
「不屬於它子樹」的目錄行為止。QTreeWidget 的根節點是 None。
"""

import bisect

from PySide6.QtGui import QTextCursor

from core.reflow import RepairPreview, _line_starts, _plan_body_edits
from .text_positions import PositionMap


def tree_children(tree, item):
    if item is None:
        return [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    return [item.child(i) for i in range(item.childCount())]


def subtree_items(tree, item):
    """item 自己的所有子孫（不含自己）。"""
    own = set()
    for child in tree_children(tree, item):
        own.add(child)
        own |= subtree_items(tree, child)
    return own


def build_chapter_index(tree, chapter_index_map, toc_boundary_map, source):
    positions = dict(chapter_index_map)
    boundaries = dict(toc_boundary_map or positions)
    starts = _line_starts(source)

    def row_offset(row: int) -> int:
        if row < 1:
            raise ValueError("目錄位置無效。")
        return starts[row - 1] if row <= len(starts) else len(source)

    descendants = {item: subtree_items(tree, item) for item in positions}
    return positions, boundaries, row_offset, descendants


def selected_body_ranges(tree, chapter_index_map, toc_boundary_map, source, selected):
    """用現有目錄分割處理範圍。選到作品或卷時，逐個子章節處理，不跨越任何目錄標題。"""
    positions, boundaries, row_offset, descendants = build_chapter_index(
        tree, chapter_index_map, toc_boundary_map, source)

    sorted_boundary_rows = sorted(boundaries.values())
    selected_intervals = []
    for item in selected:
        if item not in positions:
            raise ValueError("選定標題已刪除或無法可靠定位，未執行整理。")
        subtree = descendants.get(item, set())
        if any(other not in positions for other in subtree):
            raise ValueError("選定範圍包含無法定位的子標題，未執行整理。")
        start_row = positions[item]
        subtree_rows = {boundaries[other] for other in subtree if other in boundaries}
        end_row = None
        index = bisect.bisect_right(sorted_boundary_rows, start_row)
        while index < len(sorted_boundary_rows):
            row = sorted_boundary_rows[index]
            if row not in subtree_rows:
                end_row = row
                break
            index += 1
        selected_intervals.append(
            (row_offset(start_row), row_offset(end_row) if end_row is not None else len(source))
        )

    # 先合併重疊的選取區間：同時選到某一卷和它底下的第一章時，兩段區間會
    # 重疊，而下面只用二分搜尋找「最後一個開始的區間」，卷裡其他章就會被
    # 判定成「在子區間之外」而漏掉。
    selected_intervals.sort()
    merged_intervals = []
    for start, end in selected_intervals:
        if merged_intervals and start <= merged_intervals[-1][1]:
            merged_intervals[-1] = (merged_intervals[-1][0], max(merged_intervals[-1][1], end))
        else:
            merged_intervals.append((start, end))
    selected_intervals = merged_intervals
    boundary_offsets = sorted({row_offset(row) for row in boundaries.values()} | {len(source)})
    interval_starts = [start for start, _ in selected_intervals]
    ranges = set()
    for item, row in positions.items():
        title_start = row_offset(row)
        position = bisect.bisect_right(interval_starts, title_start) - 1
        if position < 0 or title_start >= selected_intervals[position][1]:
            continue
        newline = source.find("\n", title_start)
        if newline < 0:
            continue
        body_start = newline + 1
        next_index = bisect.bisect_right(boundary_offsets, title_start)
        body_end = boundary_offsets[next_index] if next_index < len(boundary_offsets) else len(source)
        if body_start < body_end:
            ranges.add((body_start, body_end))
    return sorted(ranges)


def toc_section_rows(tree, item, chapter_index_map, toc_boundary_map):
    """節點自己＋所有子層級涵蓋的行號範圍（1-based 起點，終點或 None）。

    終點＝下一個「不屬於自己子樹」的目錄行。用排序過的邊界行號做二分搜尋，
    再往後找第一個不在子樹裡的：全選 3,000 章時，原本每個節點都要掃過全部
    邊界（3,000 × 3,000 次比較）。"""
    start = chapter_index_map.get(item)
    if start is None:
        return None, None
    subtree = subtree_items(tree, item)
    boundaries = toc_boundary_map or chapter_index_map
    ordered = sorted((row, index) for index, (_node, row) in enumerate(boundaries.items()))
    nodes = list(boundaries)
    rows = [row for row, _index in ordered]
    position = bisect.bisect_right(rows, start)
    while position < len(ordered):
        row, index = ordered[position]
        if nodes[index] not in subtree:
            return start, row
        position += 1
    return start, None


def toc_section_lines(tree, item, chapter_index_map, toc_boundary_map, total_lines):
    """同一範圍換成 0-based 半開行區間 [起, 迄)。"""
    start, end = toc_section_rows(tree, item, chapter_index_map, toc_boundary_map)
    if start is None:
        return None
    end = end if end is not None else total_lines + 1
    return start - 1, min(end - 1, total_lines)


def preview_selected_repairs(tree, chapter_index_map, toc_boundary_map, source, selected,
                              check_spaces=True) -> RepairPreview:
    """產生預覽，不修改正文，也不重新辨識章節。"""
    if not selected:
        raise ValueError("請先在目錄選擇章節。")
    ranges = selected_body_ranges(tree, chapter_index_map, toc_boundary_map, source, selected)
    edits = []
    for start, end in ranges:
        edits.extend(_plan_body_edits(source[start:end], start, check_spaces=check_spaces))
    return RepairPreview(source=source, edits=tuple(sorted(edits, key=lambda edit: edit.start)))


def apply_repair_edits(editor, preview: RepairPreview) -> int:
    """使用者確認後套用；若預覽後正文又被修改，拒絕使用舊位置。"""
    if editor.toPlainText() != preview.source:
        raise ValueError("正文已變更，請重新產生整理預覽。")
    if not preview.edits:
        return 0

    previous_end = 0
    for edit in preview.edits:
        if not previous_end <= edit.start <= edit.end <= len(preview.source):
            raise ValueError("編輯範圍重疊或無效。")
        if preview.source[edit.start:edit.end] != edit.before:
            raise ValueError("預覽內容與編輯範圍不一致。")
        previous_end = edit.end

    # edit.start／end 是 Python 字元位置，游標吃的是 Qt（UTF-16）位置：
    # 正文裡有 emoji 或擴充漢字時兩者會差格，整理會切在字的中間。
    positions = PositionMap(preview.source)
    cursor = editor.textCursor()
    cursor.beginEditBlock()
    for edit in reversed(preview.edits):       # 由後往前套用，前方座標不受影響
        cursor.setPosition(positions.to_qt(edit.start))
        cursor.setPosition(positions.to_qt(edit.end), QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(edit.after)
    cursor.endEditBlock()
    return len(preview.edits)
