"""core.structure_builder 回歸測試。

這裡的測試值當初是跟抽離前的舊版實作逐項比對過（tree 結構、開合狀態、
chapter_raw_map、chapter_index_map、last_found_vol/ch、套用格式後的
最終文字）完全一致後才寫下來的，用來鎖住章節辨識的行為；之後刻意改變
的規則（例如預設不再辨識「卷一」「Chapter 1」）會在對應的測試旁註明。
"""

from core.structure_builder import BuildContext, build_document_structure
from core.format_options import FormatOptions
from core.chapter_parse import build_invalid_tail_regex


def tree_to_nested(tree, node=""):
    return [(tree.item(child, "text"), tree.item(child, "open"), tree_to_nested(tree, child))
            for child in tree.get_children(node)]


def make_ctx(raw_lines, **option_overrides):
    return BuildContext(
        raw_lines=raw_lines,
        options=FormatOptions(structure=option_overrides.pop("structure", "單本小說"), **option_overrides),
        invalid_tail_regex=build_invalid_tail_regex(""),
    )


def test_simple_single_book_toc():
    doc = ["第一卷 開端", "", "第一章 序幕", "　　正文一。", "", "第二章 出發", "　　正文二。"]
    result = build_document_structure(make_ctx(doc), apply_format=False, write_text=False)
    assert tree_to_nested(result.tree) == [
        ("第一卷 開端", False, [("第一章 序幕", False, []), ("第二章 出發", False, [])]),
    ]
    raw_by_text = dict((result.tree.item(n, "text"), r) for n, r in result.chapter_raw_map.items())
    assert raw_by_text == {"第一卷 開端": 0, "第一章 序幕": 2, "第二章 出發": 5}
    # apply_format=False：章節位置直接對應原始行號（raw_index + 1）。
    idx_by_text = dict((result.tree.item(n, "text"), r) for n, r in result.chapter_index_map.items())
    assert idx_by_text == {"第一卷 開端": 1, "第一章 序幕": 3, "第二章 出發": 6}


def test_simple_single_book_apply_format_output():
    doc = ["第一卷 開端", "", "第一章 序幕", "　　正文一。", "", "第二章 出發", "　　正文二。"]
    result = build_document_structure(make_ctx(doc), apply_format=True, write_text=True)
    assert "\n".join(result.processed_render_lines) == (
        "第一卷 開端\n\n第一章 序幕\n　　正文一。\n\n第二章 出發\n　　正文二。"
    )


def test_phantom_title_is_dropped_and_duplicate_is_merged():
    """『第1章』單獨一行、下一行才是真正標題：前者是幽靈標題，應被丟棄，
    真正的標題留在原本 raw_index=1 的位置（不是 raw_index=0）。"""
    doc = ["第1章", "第1章 真正的標題", "　　正文一。", "第2章 開始", "　　正文二。"]
    result = build_document_structure(make_ctx(doc), apply_format=False, write_text=False)
    assert tree_to_nested(result.tree) == [
        ("第1章 真正的標題", False, []),
        ("第2章 開始", False, []),
    ]
    raw_by_text = dict((result.tree.item(n, "text"), r) for n, r in result.chapter_raw_map.items())
    assert raw_by_text == {"第1章 真正的標題": 1, "第2章 開始": 3}


def test_mixed_volume_chapter_header_splits_into_two_levels():
    doc = ["第一卷 青雲篇 第01章 初入山門（修）", "　　正文。",
           "第一卷 青雲篇 第02章 突破", "　　正文二。"]
    result = build_document_structure(make_ctx(doc), apply_format=False, write_text=False)
    assert tree_to_nested(result.tree) == [
        ("第一卷 青雲篇", False, [("第01章 初入山門", False, []), ("第02章 突破", False, [])]),
    ]


def test_single_book_volume_and_chapter_nodes_start_collapsed():
    """單本小說（非合集）裡卷節點沒有父層作品節點，維持舊版預設不展開。"""
    doc = ["第一卷 開端", "第一章 序幕", "　　正文。"]
    result = build_document_structure(make_ctx(doc), apply_format=False, write_text=False)
    volume_node = result.tree.get_children()[0]
    chapter_node = result.tree.get_children(volume_node)[0]
    assert result.tree.item(volume_node, "open") is False
    assert result.tree.item(chapter_node, "open") is False


def test_special_title_without_number():
    doc = ["序章", "　　楔子正文。", "第1章 起源", "　　正文一。"]
    result = build_document_structure(make_ctx(doc), apply_format=False, write_text=False)
    texts = [result.tree.item(n, "text") for n in result.tree.get_children()]
    assert texts == ["序章", "第1章 起源"]


def test_apply_format_num_style_and_sep_style():
    doc = ["第一章：起源", "　　正文。"]
    ctx = make_ctx(doc, num_style="阿拉伯數字", sep_style="冒號")
    result = build_document_structure(ctx, apply_format=True, write_text=True)
    title = result.tree.item(result.tree.get_children()[0], "text")
    assert title == "第1章：起源"


def test_apply_format_false_keeps_raw_text_untouched():
    """未套用格式時，標題與正文都應保持原始文字，不受排版選項影響。"""
    doc = ["第一章:起源", "　　正文  有多餘空白。"]
    ctx = make_ctx(doc, remove_extra_spaces=True, normalize_punct=True)
    result = build_document_structure(ctx, apply_format=False, write_text=False)
    assert result.processed_render_lines == doc


def test_ignored_chapter_is_skipped():
    doc = ["第一章 起源", "　　正文。", "第二章 出發", "　　正文二。"]
    ctx = make_ctx(doc)
    ctx.ignored_chapters = {2}   # raw index of "第二章 出發"
    result = build_document_structure(ctx, apply_format=False, write_text=False)
    texts = [result.tree.item(n, "text") for n in result.tree.get_children()]
    assert texts == ["第一章 起源"]


def test_manual_marker_forces_recognition_despite_invalid_tail():
    """句末標點結尾的行本不會被自動辨識為標題，但人工標記 [::] 一定收錄；
    標記本身會從顯示文字中移除。"""
    doc = ["這是一段正文結尾是句號。[::]", "　　接下來的內容。"]
    result = build_document_structure(make_ctx(doc), apply_format=False, write_text=False)
    texts = [result.tree.item(n, "text") for n in result.tree.get_children()]
    assert texts == ["這是一段正文結尾是句號。"]


def test_last_found_vol_and_ch_track_most_recent_titles():
    """last_found_vol/ch 是狀態列摘要用的簡短標籤，一律以阿拉伯數字呈現
    （因為呼叫時沒有提供原始編號文字），跟章節標題本身的顯示樣式無關。"""
    doc = ["第一卷 開端", "第一章 序幕", "　　正文。", "第二章 出發", "　　正文二。"]
    result = build_document_structure(make_ctx(doc), apply_format=False, write_text=False)
    assert result.last_found_vol == "第1卷"
    assert result.last_found_ch == "第2章"


def test_remove_extra_spaces_strips_trailing_and_blank_line_spaces():
    doc = ["第1章 开始", "　　正文一段。  　", "　 　", "\t", "　　正文二段。 ", "第2章 再来 　", "　　正文。"]
    result = build_document_structure(make_ctx(doc, remove_extra_spaces=True), apply_format=True, write_text=True)
    lines = result.processed_render_lines
    assert lines[1] == "　　正文一段。"          # 段尾半形＋全形空白全部刪除
    assert lines[2] == "" and lines[3] == ""  # 只有空白的行清成真正的空行
    assert lines[4] == "　　正文二段。"
    assert lines[5] == "第2章 再来"


def test_remove_extra_spaces_keeps_paragraph_indent():
    # 段首縮排要留著；兩個中文字之間的空白（不管幾個）一律刪掉。
    doc = ["第1章 开始", "　　正文  中間   空白。"]
    result = build_document_structure(make_ctx(doc, remove_extra_spaces=True), apply_format=True, write_text=True)
    assert result.processed_render_lines[1] == "　　正文中間空白。"


def test_remove_extra_spaces_keeps_latin_spacing():
    # 中英數之間的空格是有意義的排版，不能跟著刪掉。
    doc = ["第1章 开始", "　　我用 Notepad 排版 的 檔案。"]
    result = build_document_structure(make_ctx(doc, remove_extra_spaces=True), apply_format=True, write_text=True)
    assert result.processed_render_lines[1] == "　　我用 Notepad 排版的檔案。"


def test_remove_extra_spaces_leaves_chapter_title_alone():
    # 標題間隔由「編號與標題間隔」決定，不能被這個選項吃掉。
    doc = ["第1章 山間 來信", "　　正文。"]
    result = build_document_structure(make_ctx(doc, remove_extra_spaces=True), apply_format=True, write_text=True)
    assert result.processed_render_lines[0] == "第1章 山間 來信"


def test_remove_extra_spaces_with_indent_options():
    doc = ["第1章 开始", "  正文。 　"]
    indented = build_document_structure(
        make_ctx(list(doc), remove_extra_spaces=True, auto_indent=True), apply_format=True, write_text=True)
    assert indented.processed_render_lines[1] == "　　正文。"
    flat = build_document_structure(
        make_ctx(list(doc), remove_extra_spaces=True, remove_indent=True), apply_format=True, write_text=True)
    assert flat.processed_render_lines[1] == "正文。"


# ----------------------------------------------------------------------
# 卷／章結尾行與推定卷
# ----------------------------------------------------------------------

def _chapters(numbers):
    lines = []
    for number in numbers:
        lines += [f"第{number}章 標題", "　　正文內容。" * 8, ""]
    return lines


def _outline(result):
    """[(卷標題, 是否推定, 子節點數)]，沒有子節點的根節點子節點數為 0。"""
    return [(result.tree.item(node, "text"), node in result.virtual_volumes, len(result.tree.get_children(node)))
            for node in result.tree.get_children("")]


def _build(doc):
    return build_document_structure(make_ctx(doc), apply_format=False, write_text=False)


def test_end_mark_variants_are_not_titles():
    from core.title_markers import parse_end_mark
    for text in ("第一卷終", "【第二卷终】", "第一卷 完", "（第一卷完）", "第一卷（完）", "卷一終",
                 "本卷完", "第一卷結束", "第三卷完本", "第一章 完。"):
        assert parse_end_mark(text) is not None, text
    for text in ("第十章 終章", "第三卷 完結篇", "第一卷 初見", "全書完"):
        assert parse_end_mark(text) is None, text


def test_volume_end_without_volume_heading_infers_volume():
    doc = (["簡介", "", "第一卷 初見", ""] + _chapters(range(1, 6)) + ["【第一卷终】", ""]
           + _chapters(range(1, 5)) + ["【第二卷终】", "", "第三卷 歸途", ""] + _chapters(range(1, 4)))
    result = _build(doc)
    # 開頭的「簡介」是獨立的一個項目（沒有標題文字的特殊標題不再被當成
    # 拆行標題刪掉，見 render_special_title 的說明）。
    assert _outline(result) == [("簡介", False, 0), ("第一卷 初見", False, 5),
                                ("第二卷", True, 4), ("第三卷 歸途", False, 3)]
    info = next(iter(result.virtual_volumes.values()))
    assert doc[info["row"]] == "第1章 標題" and info["row"] > doc.index("【第一卷终】")
    assert not any("终" in result.tree.item(n, "text") for n in result.tree.walk())


def test_chapter_number_reset_splits_volume_before_end_mark():
    doc = (["第一卷 初見", ""] + _chapters(range(1, 6)) + _chapters(range(1, 5)) + ["【第二卷终】", ""]
           + ["第三卷 歸途", ""] + _chapters(range(1, 4)))
    assert _outline(_build(doc)) == [("第一卷 初見", False, 5), ("第二卷", True, 4), ("第三卷 歸途", False, 3)]


def test_continuing_numbers_after_volume_end():
    doc = ["第一卷 初見", ""] + _chapters(range(88, 91)) + ["第一卷終", ""] + _chapters(range(91, 94))
    result = _build(doc)
    assert _outline(result) == [("第一卷 初見", False, 3), ("第二卷", True, 3)]
    assert result.last_found_vol == "第2卷"


def test_first_volume_inferred_only_with_volume_hierarchy():
    with_volumes = ["序章 開始", "正文", ""] + _chapters(range(1, 4)) + ["第二卷 風起", ""] + _chapters(range(4, 7))
    assert _outline(_build(with_volumes)) == [
        ("序章 開始", False, 0), ("第一卷", True, 3), ("第二卷 風起", False, 3)]
    without_volumes = _chapters(range(1, 4)) + _chapters(range(1, 3))
    assert not _build(without_volumes).virtual_volumes


def test_chapter_end_mark_needs_matching_number():
    doc = _chapters([1, 2]) + ["第二章完", "", "第三章 終", "正文"]
    titles = [text for text, _virtual, _count in _outline(_build(doc))]
    assert titles == ["第1章 標題", "第2章 標題", "第三章 終"]


# ----------------------------------------------------------------------
# 標題不可以被自動刪掉（審查報告 C-06）
# ----------------------------------------------------------------------

def test_empty_chapter_before_volume_title_is_kept():
    """「第1章」後面直接接卷標題是合法的空章，不是被拆行的標題。"""
    result = _build(["第1章", "第二卷 新卷", "第2章 開始", "正文。"])
    titles = [result.tree.item(node, "text") for node in result.tree.walk()]
    assert "第1章" in titles and "第2章 開始" in titles, titles


def test_special_title_without_body_is_kept():
    doc = ["序章", "第1章 開始", "　　正文。"]
    result = _build(doc)
    assert [result.tree.item(node, "text") for node in result.tree.walk()] == ["序章", "第1章 開始"]


def test_mixed_header_does_not_swallow_decimal_chapter():
    """同行卷章表頭只能跟「同一個編號、同一個單位」的下一行合併；
    int(1.5) == 1 會把「第1.5章 插曲」整個吃掉。"""
    result = _build(["第一卷 山川 第1章 開始", "第1.5章 插曲", "　　正文。"])
    titles = [result.tree.item(node, "text") for node in result.tree.walk()]
    assert titles == ["第一卷 山川", "第1章 開始", "第1.5章 插曲"]


def test_zero_chapter_keeps_its_number_in_last_found():
    assert _build(["第0章 序", "　　正文。"]).last_found_ch == "第0章"
    assert _build(["第12章 甲", "　　正文。"]).last_found_ch == "第12章"
