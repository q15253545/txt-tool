"""章節結構辨識：把整份文件解析成一棵與 UI 元件無關的目錄樹。

用 SimpleTree 取代真正的樹狀元件，讓辨識規則可以獨立測試；介面那一側
只需要寫一段「把 SimpleTree 畫進真正元件」的轉接程式。

呼叫方式：
    ctx = BuildContext(raw_lines=..., options=..., user_chapter_rules=...,
                        auto_titles=..., force_lv1_chapters=..., ...,
                        invalid_tail_regex=...)
    result = build_document_structure(ctx, apply_format=True)

呼叫端負責：套用 apply_format=True 時是否要把 result.processed_render_lines
寫回文件、更新 auto_titles、重新映射 ignored/force 章節集合，以及把
result.tree 畫進真正的目錄元件——這些都牽涉具體的 UI／文件狀態，
不屬於「辨識」本身。
"""

import re
from dataclasses import dataclass, field

from .simple_tree import SimpleTree
from .format_options import FormatOptions
from .title_markers import strip_persistent_title_marker, END_MARK_REGEX, parse_end_mark
from .text_format import (
    PUNCT_TRANS, HALF_PUNCT_TRANS, FULLWIDTH_DIGIT_TRANS, HALFWIDTH_DIGIT_TRANS,
    normalize_dialogue_quotes,
)
from .cn_numerals import chinese_to_arabic, arabic_to_chinese
from .chapter_parse import (
    CN_NUM_FLOAT_PATTERN, INLINE_SPACE_REGEX,
    parse_lv1, parse_lv2, parse_special, parse_mixed_volume_chapter_header,
    is_weak_numbered_title, is_valid_auto_title, strip_title_body,
    preserve_title_separator, original_number_text,
)
from .collection import analyze_collection_structure
from .reflow import collapse_inline_spaces
from .user_rules import match_user_chapter_rule


def format_custom_title(options: FormatOptions, extra_prefix: str, prefix_tag: str, num_val: float,
                         unit_tag: str, body_title: str, apply_format: bool, number_text=None) -> str:
    num_style, sep_style = options.num_style, options.sep_style
    # num_val == 0 同時被當成「這個標題沒有編號」的哨兵值（例如外傳、
    # 不帶編號的番外），所以不能單純放寬成 >= 0；只有原始文字確實寫著
    # 0／零時，才視為真正的第 0 章，避免「第0章 序幕」被輸出成「第章 序幕」。
    has_number = num_val > 0 or (
        number_text is not None
        and str(number_text).strip() != ""
        and chinese_to_arabic(str(number_text)) == 0
        and re.search(r"[0０零〇]", str(number_text))
    )
    if has_number:
        if not float(num_val).is_integer():
            final_num = str(num_val)
        else:
            num_int = int(num_val)
            if num_style == "中文數字": final_num = arabic_to_chinese(num_int)
            elif num_style == "阿拉伯數字": final_num = str(num_int)
            else: final_num = number_text or str(num_int)
        tag = f"{prefix_tag}{final_num}{unit_tag}"
    else:
        tag = f"{prefix_tag}{unit_tag}".strip()

    tag = f"{extra_prefix}{tag}".strip()

    # 開頭分隔符的清理已統一由 strip_title_body 負責。
    body = strip_title_body(body_title)

    if apply_format and options.remove_extra_spaces:
        body = INLINE_SPACE_REGEX.sub(' ', body)

    if not body: return tag
    if not tag: return body

    if sep_style == "半形空格": return f"{tag} {body}"
    elif sep_style == "全形空格": return f"{tag}　{body}"
    elif sep_style == "冒號": return f"{tag}：{body}"
    else: return f"{tag} {body}"


def format_punctuation_and_dialogue(options: FormatOptions, text: str, dialogue_depth: int,
                                     carry_depth: bool = True):
    """回傳 (格式化後文字, 新的對話引號巢狀深度)。"""
    text, marker = strip_persistent_title_marker(text)
    if options.normalize_punct:
        text = text.translate(PUNCT_TRANS)
    elif options.halfwidth_punct:
        text = text.translate(HALF_PUNCT_TRANS)
    if options.format_dialogue:
        # 對話常跨行，把上一行結束時的巢狀深度帶進來，避免第二行的內層
        # 引號被誤判成外層。標題與中繼資料是獨立的行，不參與這個狀態。
        if carry_depth:
            text, dialogue_depth = normalize_dialogue_quotes(
                text, dialogue_depth, return_depth=True)
        else:
            text = normalize_dialogue_quotes(text)
    if options.fullwidth_digits:
        text = text.translate(FULLWIDTH_DIGIT_TRANS)
    elif options.halfwidth_digits:
        text = text.translate(HALFWIDTH_DIGIT_TRANS)
    return text + ({"include": "[::]", "exclude": "[::X]",
                    "auto_work": "[::W]", "auto_title": "[::T]"}.get(marker, "")), dialogue_depth


def find_merge_subtitle(raw_lines, start_index, total, invalid_tail_regex):
    """尋找獨立章號後的拆行章名，回傳清理後章名與其原始行號。"""
    from .reflow import PARAGRAPH_INDENT, DIALOGUE_OR_SENTENCE_REGEX
    from .chapter_parse import clean_merged_subtitle, is_valid_title

    peek = start_index + 1
    while peek < total and not raw_lines[peek].strip():
        peek += 1
    if peek >= total:
        return "", None

    raw_candidate = raw_lines[peek]
    # 段落縮排是「這行是正文，不是標題」最強的證據，必須在 strip 之前判斷。
    if PARAGRAPH_INDENT.match(raw_candidate):
        return "", None

    candidate, candidate_marker = strip_persistent_title_marker(raw_candidate.strip())
    if candidate_marker == "exclude":
        return "", None
    cleaned, has_number_prefix = clean_merged_subtitle(candidate)
    if not has_number_prefix and (parse_lv2(candidate) or parse_lv1(candidate) or parse_special(candidate)):
        return "", None
    if not cleaned:
        return "", None
    # 帶有對話引號或句末標點的行幾乎必定是正文；標題極少長這樣。
    if not has_number_prefix and DIALOGUE_OR_SENTENCE_REGEX.search(candidate):
        return "", None

    # 有明確小節編號的拆行章名可較長；其他格式沿用原本的保守判定。
    if has_number_prefix:
        if len(cleaned) > 180:
            return "", None
    elif not is_valid_title(candidate, invalid_tail_regex):
        return "", None

    cleaned = re.sub(r"^[\s，,、:：\-—]+", "", cleaned).strip()
    cleaned = strip_title_body(cleaned)
    return cleaned, peek


@dataclass
class BuildContext:
    """辨識一份文件所需的全部輸入，以及過程中累積的輸出。"""
    raw_lines: list
    options: FormatOptions
    user_chapter_rules: list = field(default_factory=list)
    auto_titles: dict = field(default_factory=dict)
    force_lv1_chapters: set = field(default_factory=set)
    force_lv2_chapters: set = field(default_factory=set)
    ignored_chapters: set = field(default_factory=set)
    invalid_tail_regex: object = None
    tree: SimpleTree = field(default_factory=SimpleTree)
    chapter_raw_map: dict = field(default_factory=dict)
    chapter_index_map: dict = field(default_factory=dict)
    chapter_records: dict = field(default_factory=dict)
    dialogue_depth: int = 0

    def match_custom_title(self, text):
        return match_user_chapter_rule(text, self.user_chapter_rules)

    def format_custom_title(self, extra_prefix, prefix_tag, num_val, unit_tag, body_title,
                             apply_format, number_text=None):
        return format_custom_title(self.options, extra_prefix, prefix_tag, num_val, unit_tag,
                                    body_title, apply_format, number_text)

    def format_punctuation_and_dialogue(self, text, carry_depth=True):
        result, self.dialogue_depth = format_punctuation_and_dialogue(
            self.options, text, self.dialogue_depth, carry_depth)
        return result

    def find_merge_subtitle(self, start_index, total):
        return find_merge_subtitle(self.raw_lines, start_index, total, self.invalid_tail_regex)


@dataclass
class RenderState:
    """單次解析的狀態；由作品、卷、章與附錄處理函式共用。"""
    idx: int = 0
    total: int = 0
    current_lv1_node: str = ""
    current_work_node: str = ""
    current_work_name: str = ""
    last_found_vol: str = ""
    last_found_ch: str = ""
    collection_active: bool = False
    invalid_tail_regex: object = None
    volume_nodes: dict = field(default_factory=dict)
    work_nodes: dict = field(default_factory=dict)
    work_volume_nodes: dict = field(default_factory=dict)
    collection_info: dict = field(default_factory=dict)
    collection_combined: dict = field(default_factory=dict)
    collection_work_lines: dict = field(default_factory=dict)
    processed_render_lines: list = field(default_factory=list)
    opts: FormatOptions = field(default_factory=FormatOptions)
    # 卷結尾行（第一卷終、【第二卷终】…）與推定卷需要的資料
    last_chapter_number: float = None
    current_volume_number: int = None
    real_volume_numbers: dict = field(default_factory=dict)
    volume_end_marks: list = field(default_factory=list)
    arabic_volume_numbers: bool = False


@dataclass
class StructureResult:
    tree: SimpleTree
    chapter_raw_map: dict
    chapter_index_map: dict
    chapter_records: dict
    processed_render_lines: list
    last_found_vol: str
    last_found_ch: str
    # 推定卷：本文沒有卷標題，但從卷結尾行／章號重新起算推得出來的卷。
    # 只存在目錄樹裡（不在 chapter_raw_map），值為 {"title", "number", "row"}，
    # row 是卷內第一個項目的原始行號——要寫回本文時卷標題就插在那一行前面。
    virtual_volumes: dict = field(default_factory=dict)


def build_chapter_records(ctx: BuildContext, collection_info):
    """從已確認目錄建立統一資料；缺章不再自行用另一套正則判斷。"""
    ctx.chapter_records = {}
    for node, row in ctx.chapter_raw_map.items():
        text, marker = strip_persistent_title_marker(ctx.raw_lines[row].strip())
        stored = ctx.auto_titles.get(row)
        combined = collection_info["combined"].get(row)
        source = combined["remainder"] if combined else text
        mixed = parse_mixed_volume_chapter_header(source)
        custom = ctx.match_custom_title(source)
        chapter = parse_lv2(source)
        volume = parse_lv1(source)
        title = ctx.tree.item(node, "text")
        if row in ctx.force_lv1_chapters:
            kind = "volume"
        elif row in ctx.force_lv2_chapters:
            kind = "chapter"
        elif collection_info["active"] and title == collection_info["work_lines"].get(row):
            kind = "work"
        elif combined and title == combined["work"]:
            kind = "work"
        elif ctx.tree.get_children(node) or (volume and not mixed and not combined and not custom):
            kind = "volume"
        elif custom and custom["level"] == 1:
            kind = "volume"
        else:
            kind = "chapter"
        number, prefix = 0, ""
        if kind == "chapter":
            if custom:
                number = custom["number"]
                prefix = "第"
            elif mixed:
                number, prefix = mixed["chapter_number"], "第"
            elif chapter:
                number, prefix = chapter[3], chapter[2]
            elif stored:
                number, prefix = stored.get("number", 0), stored.get("prefix", "")
            if chapter and chapter[2] == "番外":
                prefix = "番外"
        source_kind = ("manual" if marker == "include" or row in ctx.force_lv1_chapters
                       or row in ctx.force_lv2_chapters else "rule" if custom else "auto")
        ctx.chapter_records[node] = {"title": title, "kind": kind,
                                      "number": number, "prefix": prefix, "source": source_kind}


def record_title(ctx: BuildContext, state: RenderState, item_id, title_text, processed_render_lines,
                  apply_format, raw_idx, manual_marked=False, auto_marker=""):
    options = state.opts
    # 未閉合的引號不該跨章節延續——若某章漏了一個閉引號，之後所有章節的
    # 內外層都會反過來。在章節邊界歸零，把影響侷限在單一章節內。
    ctx.dialogue_depth = 0
    ctx.chapter_raw_map[item_id] = raw_idx
    original_marker = strip_persistent_title_marker(ctx.raw_lines[raw_idx].strip())[1]
    if not auto_marker and original_marker in {"auto_work", "auto_title"}:
        auto_marker = original_marker
    suffix = "[::]" if manual_marked else {"auto_work": "[::W]", "auto_title": "[::T]"}.get(auto_marker, "")
    if apply_format:
        if options.keep_separator:
            source_title = strip_persistent_title_marker(ctx.raw_lines[raw_idx].strip())[0]
            title_text = preserve_title_separator(title_text, source_title)
        title_text = ctx.format_punctuation_and_dialogue(title_text, carry_depth=False)
        ctx.tree.item(item_id, text=title_text)
        manage_spacing = (
            options.remove_extra_empty
            or options.add_empty
            or options.format_title
        )
        if manage_spacing:
            while processed_render_lines and processed_render_lines[-1] == "":
                processed_render_lines.pop()

        empty_before = 0
        if options.add_empty:
            empty_before = 2
        elif options.format_title:
            empty_before = 1

        if processed_render_lines:
            processed_render_lines.extend([""] * empty_before)

        render_title = title_text + suffix
        processed_render_lines.append(render_title)

        empty_after = 1 if options.format_title else 0
        processed_render_lines.extend([""] * empty_after)

        ctx.chapter_index_map[item_id] = len(processed_render_lines) - empty_after
    else:
        render_title = title_text + suffix
        processed_render_lines.append(render_title)
        ctx.chapter_index_map[item_id] = len(processed_render_lines)


def render_collection_title(ctx: BuildContext, state: RenderState, apply_format, collection_record,
                             line_str, raw_line, title_raw_idx, manual_marked):
    """建立作品／卷父層，移除章標題中的重複作品前綴。"""
    state.current_work_name = collection_record['work']
    volume_title = collection_record['volume']
    display_row = len(state.processed_render_lines) + 1
    if state.current_work_name not in state.work_nodes:
        state.current_work_node = ctx.tree.insert('', 'end', text=state.current_work_name, open=True)
        state.work_nodes[state.current_work_name] = state.current_work_node
        if apply_format:
            record_title(ctx, state, state.current_work_node, state.current_work_name,
                          state.processed_render_lines, True, title_raw_idx, manual_marked,
                          auto_marker='auto_work')
        else:
            ctx.chapter_raw_map[state.current_work_node] = title_raw_idx
            ctx.chapter_index_map[state.current_work_node] = display_row
    else:
        state.current_work_node = state.work_nodes[state.current_work_name]
    volume_key = (state.current_work_name, re.sub(r'\s+', '', volume_title))
    if volume_key not in state.work_volume_nodes:
        state.current_lv1_node = ctx.tree.insert(state.current_work_node, 'end', text=volume_title, open=True)
        state.work_volume_nodes[volume_key] = state.current_lv1_node
        if apply_format:
            record_title(ctx, state, state.current_lv1_node, volume_title,
                          state.processed_render_lines, True, title_raw_idx, False)
        else:
            ctx.chapter_raw_map[state.current_lv1_node] = title_raw_idx
            ctx.chapter_index_map[state.current_lv1_node] = display_row
    else:
        state.current_lv1_node = state.work_volume_nodes[volume_key]
    data = collection_record['data']
    if collection_record['kind'] == 'chapter':
        _, _, prefix, number, unit, body_title = data
        body_title = strip_title_body(body_title)
        if state.opts.keep_number:
            number_match = re.search(r'第\s*(' + CN_NUM_FLOAT_PATTERN + r')\s*' + re.escape(unit), line_str)
            number_text = number_match.group(1) if number_match else str(int(number))
            chapter_title = f'第{number_text}{unit} {body_title}'.strip()
        else:
            chapter_title = ctx.format_custom_title('', prefix, number, unit, body_title, apply_format)
        if prefix != '番外':
            state.last_found_ch = ctx.format_custom_title('', prefix, number, unit, '', apply_format).strip()
    elif collection_record['kind'] == 'special':
        _, _, special_tag, body_title = data
        body_title = re.sub(r'^[\s，,、:：\-—]+', '', body_title).strip()
        chapter_title = f'{special_tag} {body_title}'.strip()
    else:
        chapter_title = collection_record['remainder'].strip()
    chapter_node = ctx.tree.insert(state.current_lv1_node, 'end', text=chapter_title)
    if apply_format:
        record_title(ctx, state, chapter_node, chapter_title, state.processed_render_lines, True,
                      title_raw_idx, manual_marked,
                      auto_marker='auto_title' if collection_record['kind'] != 'chapter' else '')
    else:
        state.processed_render_lines.append(raw_line)
        ctx.chapter_raw_map[chapter_node] = title_raw_idx
        ctx.chapter_index_map[chapter_node] = display_row
    state.last_found_vol = volume_title
    state.idx += 1


def render_mixed_title(ctx: BuildContext, state: RenderState, apply_format, mixed_data, title_raw_idx,
                        manual_marked):
    """拆開同行卷章表頭，並與隨後的正式章標題去重。"""
    volume_key = (mixed_data['volume_unit'], mixed_data['volume_number'],
                  re.sub(r'\s+', '', mixed_data['volume_body']))
    if state.opts.keep_number:
        volume_tag = f"第{mixed_data['volume_number_text']}{mixed_data['volume_unit']}"
    else:
        volume_tag = ctx.format_custom_title('', '第', mixed_data['volume_number'],
                                              mixed_data['volume_unit'], '', apply_format)
    volume_title = f"{volume_tag} {mixed_data['volume_body']}".strip()
    chapter_body = mixed_data['chapter_body']
    chosen_raw_idx = title_raw_idx
    consume_end = state.idx + 1
    peek = state.idx + 1
    while peek < state.total and (not ctx.raw_lines[peek].strip()):
        peek += 1
    if peek < state.total:
        next_clean, next_marker = strip_persistent_title_marker(ctx.raw_lines[peek].strip())
        next_data = parse_lv2(next_clean)
        # 必須是同一個編號、同一個單位才算重複：int() 會把「第1.5章」截成 1，
        # 把小數章、番外整個吞掉（第1.5章 插曲 → 第1章 插曲）。
        same_chapter = bool(next_data) and (
            next_data[3] == mixed_data['chapter_number']
            and next_data[4] == mixed_data['chapter_unit']
            and next_data[2] == '第')
        if next_marker != 'exclude' and next_data and (not is_weak_numbered_title(next_clean)) and same_chapter:
            chapter_body = next_data[5].strip()
            chosen_raw_idx = peek
            consume_end = peek + 1
    if state.opts.keep_number:
        chapter_tag = f"第{mixed_data['chapter_number_text']}{mixed_data['chapter_unit']}"
    else:
        chapter_tag = ctx.format_custom_title('', '第', mixed_data['chapter_number'],
                                               mixed_data['chapter_unit'], '', apply_format)
    chapter_title = f'{chapter_tag} {chapter_body}'.strip()
    volume_is_new = volume_key not in state.volume_nodes
    if volume_is_new:
        state.current_lv1_node = ctx.tree.insert('', 'end', text=volume_title)
        state.volume_nodes[volume_key] = state.current_lv1_node
        if apply_format:
            record_title(ctx, state, state.current_lv1_node, volume_title, state.processed_render_lines,
                          True, title_raw_idx, manual_marked)
        else:
            ctx.chapter_raw_map[state.current_lv1_node] = title_raw_idx
    else:
        state.current_lv1_node = state.volume_nodes[volume_key]
    _note_volume(state, state.current_lv1_node, mixed_data['volume_number'], mixed_data['volume_number_text'])
    state.last_chapter_number = mixed_data['chapter_number']
    chapter_node = ctx.tree.insert(state.current_lv1_node, 'end', text=chapter_title)
    if apply_format:
        record_title(ctx, state, chapter_node, chapter_title, state.processed_render_lines, True,
                      chosen_raw_idx, manual_marked)
    else:
        first_render_row = len(state.processed_render_lines) + 1
        state.processed_render_lines.extend(ctx.raw_lines[state.idx:consume_end])
        ctx.chapter_raw_map[chapter_node] = chosen_raw_idx
        ctx.chapter_index_map[chapter_node] = first_render_row + (chosen_raw_idx - state.idx)
        if volume_is_new:
            ctx.chapter_index_map[state.current_lv1_node] = first_render_row
    state.last_found_vol = volume_tag
    state.last_found_ch = chapter_tag
    state.idx = consume_end


def render_volume_title(ctx: BuildContext, state: RenderState, apply_format, custom_title, forced_level,
                         m_lv1, m_lv2, line_str, raw_line, title_raw_idx, manual_marked):
    """處理卷級標題及人工層級，保留既有父層與卷去重規則。"""
    if custom_title and (not forced_level):
        arc, p_fix, v_num, v_unit, v_body = (
            '', '第' if custom_title['number'] else '', custom_title['number'],
            '卷' if custom_title['number'] else '', custom_title['title'])
    elif m_lv1:
        arc, p_fix, v_num, v_unit, v_body = m_lv1
    elif m_lv2:
        arc, vol, p_fix, v_num, v_unit, v_body = m_lv2
        arc = f'{arc} {vol}'.strip()
    else:
        arc, p_fix, v_num, v_unit, v_body = ('', '', 0, '', line_str)
    merged = False
    if not v_body and apply_format and state.opts.merge_title:
        merged_title, merged_index = ctx.find_merge_subtitle(state.idx, state.total)
        if merged_title:
            v_body = merged_title
            merged = True
            state.idx = merged_index
    extra_prefix = f'{arc} ' if arc else ''
    if apply_format and (merged or not state.opts.keep_number or not state.opts.keep_separator):
        title = ctx.format_custom_title(extra_prefix, p_fix, v_num, v_unit, v_body, apply_format,
                                         original_number_text(line_str, v_unit))
    else:
        title = line_str
    state.last_found_vol = ctx.format_custom_title(extra_prefix, p_fix, v_num, v_unit, '', apply_format).strip()
    if state.collection_active and state.current_work_node and (not arc):
        volume_key = (state.current_work_name, re.sub(r'\s+', '', title))
        if volume_key in state.work_volume_nodes:
            state.current_lv1_node = state.work_volume_nodes[volume_key]
            if not apply_format:
                state.processed_render_lines.append(raw_line)
        else:
            state.current_lv1_node = ctx.tree.insert(state.current_work_node, 'end', text=title, open=True)
            state.work_volume_nodes[volume_key] = state.current_lv1_node
            record_title(ctx, state, state.current_lv1_node, title, state.processed_render_lines,
                          apply_format, title_raw_idx, manual_marked)
    else:
        volume_key = (v_unit, v_num, re.sub(r'\s+', '', v_body))
        if v_num and volume_key in state.volume_nodes and (not forced_level):
            state.current_lv1_node = state.volume_nodes[volume_key]
            if not apply_format:
                state.processed_render_lines.append(raw_line)
        else:
            state.current_lv1_node = ctx.tree.insert('', 'end', text=title)
            if v_num:
                state.volume_nodes[volume_key] = state.current_lv1_node
            record_title(ctx, state, state.current_lv1_node, title, state.processed_render_lines,
                          apply_format, title_raw_idx, manual_marked)
        _note_volume(state, state.current_lv1_node, v_num,
                     original_number_text(line_str, v_unit) if v_unit else '')
    state.idx += 1


def render_chapter_title(ctx: BuildContext, state: RenderState, apply_format, custom_title, forced_level,
                          m_lv1, m_lv2, line_str, title_raw_idx, manual_marked):
    """處理章級標題、下行合併與相鄰重複章名。"""
    if custom_title and (not forced_level):
        arc, vol, ch_prefix, ch_num, ch_unit, ch_body = (
            '', '', '第' if custom_title['number'] else '', custom_title['number'],
            '章' if custom_title['number'] else '', custom_title['title'])
    elif m_lv2:
        arc, vol, ch_prefix, ch_num, ch_unit, ch_body = m_lv2
    elif m_lv1:
        arc, ch_prefix, ch_num, ch_unit, ch_body = m_lv1
        vol = ''
    else:
        arc, vol, ch_prefix, ch_num, ch_unit, ch_body = ('', '', '', 0.0, '', line_str)
    ch_body = strip_title_body(ch_body)
    merged = False
    if not ch_body and apply_format and state.opts.merge_title:
        merged_title, merged_index = ctx.find_merge_subtitle(state.idx, state.total)
        if merged_title:
            ch_body = merged_title
            merged = True
            state.idx = merged_index
    is_phantom = False
    # 幽靈標題＝同一個標題被拆成兩行（例如「第1章」單獨一行、下一行才是
    # 真正的完整標題）。「本章沒有標題文字」不足以證明該刪除——「第1章」
    # 後面直接接「第2章 開始」是完全合法的空章。判定為幽靈需同時滿足：
    #   1. 本行沒有被人工收錄（[::] 是使用者明確表達「這是章節」）
    #   2. 下一個標題的「編號」與本行相同——真正被拆行的標題編號會一致，
    #      而「第1章／第2章」這種相鄰空章編號不同，應予保留。
    # 「下一行是卷或特殊標題就刪掉空章」曾經也算幽靈，但那會把合法的空章
    #  整個刪掉（例如「第1章」後面就接「第二卷」），標題與正文一起消失，
    #  使用者事後完全看不出少了什麼，所以不再這樣判斷。
    if not ch_body and not manual_marked:
        peek = state.idx + 1
        while peek < state.total:
            nxt = ctx.raw_lines[peek].strip()
            if not nxt:
                peek += 1
                continue
            next_clean, next_marker = strip_persistent_title_marker(nxt)
            if next_marker != 'exclude' and is_valid_auto_title(next_clean, state.invalid_tail_regex):
                next_lv2 = parse_lv2(next_clean)
                if next_lv2 and not is_weak_numbered_title(next_clean):
                    if next_lv2[3] == ch_num and next_lv2[4] == ch_unit:
                        is_phantom = True
            break
    if is_phantom:
        state.idx += 1
        return
    dup_cands, peek = ([(line_str, ch_body, title_raw_idx)], state.idx + 1)
    while peek < state.total:
        nxt, nxt_marker = strip_persistent_title_marker(ctx.raw_lines[peek].strip())
        if not nxt:
            peek += 1
            continue
        if nxt_marker != 'exclude' and is_valid_auto_title(nxt, state.invalid_tail_regex):
            p_data = parse_lv2(nxt)
            if p_data and (not is_weak_numbered_title(nxt)):
                p_arc, p_vol, p_prefix, p_num, p_unit, p_body = p_data
                # 只比數字不足以證明是同一章：「第1章 開始」與「第1節 插曲」
                # 是兩個不同的標題。單位與前綴也必須一致才算重複。
                if (p_num == ch_num and ch_num > 0
                        and p_unit == ch_unit and p_prefix == ch_prefix):
                    dup_cands.append((nxt, p_body, peek))
                    peek += 1
                    continue
        break
    if len(dup_cands) > 1:
        chosen_raw, ch_body, chosen_raw_idx = max(dup_cands, key=lambda x: len(x[1]))
        state.idx = peek
        best_data = parse_lv2(chosen_raw)
        if best_data:
            arc, vol, ch_prefix, ch_num, ch_unit, ch_body = best_data
            ch_body = strip_title_body(ch_body)
    else:
        chosen_raw = line_str
        chosen_raw_idx = title_raw_idx
        state.idx += 1
    extra_prefix = ''
    if arc:
        extra_prefix += arc.strip() + ' '
    if vol:
        extra_prefix += vol.strip() + ' '
    if apply_format and (merged or not state.opts.keep_number or not state.opts.keep_separator):
        chosen_title = ctx.format_custom_title(extra_prefix, ch_prefix, ch_num, ch_unit, ch_body,
                                                apply_format, original_number_text(chosen_raw, ch_unit))
    else:
        chosen_title = chosen_raw
    if ch_prefix != '番外':
        # 「第0章」的 0 會被當成「沒有編號」，最新章變成「第章」。只有這種
        # 情況才把原始編號文字傳進去，其餘維持原本的阿拉伯數字寫法（最新章
        # 會用在建議檔名上，「第2章」比「第二章」好排序）。
        zero_number_text = original_number_text(chosen_raw, ch_unit) if not ch_num else None
        state.last_found_ch = ctx.format_custom_title(
            extra_prefix, ch_prefix, ch_num, ch_unit, '', apply_format, zero_number_text).strip()
    if ch_num:
        state.last_chapter_number = ch_num
    parent = state.current_lv1_node if state.current_lv1_node else ''
    item_id = ctx.tree.insert(parent, 'end', text=chosen_title)
    record_title(ctx, state, item_id, chosen_title, state.processed_render_lines, apply_format,
                  chosen_raw_idx, manual_marked)


def render_special_title(ctx: BuildContext, state: RenderState, apply_format, m_spec, line_str,
                          title_raw_idx, manual_marked):
    """處理簡介、序言及後記等沒有章號的標題。"""
    arc, vol, spec_tag, spec_body = m_spec
    spec_body = re.sub(r'^[\s，,、:：\-—]+', '', spec_body).strip()
    spec_body = strip_title_body(spec_body)
    if not spec_body and apply_format and state.opts.merge_title:
        merged_title, merged_index = ctx.find_merge_subtitle(state.idx, state.total)
        if merged_title:
            spec_body = merged_title
            state.idx = merged_index
    # 沒有標題文字的序章／楔子照樣是一個章節：舊版只要下一行是任何標題就把
    # 它當成「被拆行的標題」刪掉，結果「序章」接「第1章 開始」時，序章連同
    # 這個位置一起從目錄消失。拆行的情況由上面的 merge_title 處理就夠了。
    extra_prefix = ''
    if arc:
        extra_prefix += arc.strip() + ' '
    if vol:
        extra_prefix += vol.strip() + ' '
    if apply_format and not state.opts.keep_separator:
        title = ctx.format_custom_title(extra_prefix, '', 0.0, spec_tag, spec_body, apply_format)
    else:
        title = f'{extra_prefix}{spec_tag} {spec_body}'.strip()
    if state.collection_active and state.current_work_node:
        special_parent = state.current_lv1_node or state.current_work_node
    else:
        special_parent = ''
    item_id = ctx.tree.insert(special_parent, 'end', text=title)
    record_title(ctx, state, item_id, title, state.processed_render_lines, apply_format, title_raw_idx,
                 manual_marked)
    state.idx += 1


def _note_volume(state: RenderState, node, number, number_text):
    """記下真正的卷標題：卷號（推定卷避開重複卷號用）與編號寫法。"""
    number = int(number) if number and float(number).is_integer() else None
    state.current_volume_number = number
    if node and not state.collection_active:
        state.real_volume_numbers[node] = number
    if number_text and re.search(r'[0-9０-９]', str(number_text)):
        state.arabic_volume_numbers = True


def _is_end_line(state: RenderState, end_mark) -> bool:
    """卷結尾行一律視為結尾；章結尾行（第一章完）要跟上一章同號才算——
    「第100章 終」緊接在第99章後面，是一個叫「終」的真正章節。"""
    if end_mark['level'] == 'volume':
        return True
    number = end_mark['number']
    return number is None or state.last_chapter_number is None or number == state.last_chapter_number


def _close_volume(state: RenderState, row, end_mark, line_str):
    """卷結尾行：目前這一卷到此為止，後面的章節不再掛在它底下。"""
    number = end_mark['number'] if end_mark['number'] is not None else state.current_volume_number
    state.volume_end_marks.append((row, number, end_mark['unit']))
    if re.search(r'[0-9０-９]', line_str):
        state.arabic_volume_numbers = True
    state.current_lv1_node = ''
    state.current_volume_number = None


def infer_virtual_volumes(ctx: BuildContext, state: RenderState) -> dict:
    """補上本文沒寫、但推得出來的卷（只加在目錄樹，不改本文）。

    有些作者卷首不寫「第二卷」，只在卷尾寫【第二卷终】；或者整本只有卷尾
    沒有卷首。以「卷標題」和「卷結尾行」當分界，把目錄切成一段一段：
      - 段落後面接著「第N卷終」→ 這段是第N卷；
      - 段落前面是「第M卷終」→ 這段是第M+1卷；
      - 全書開頭、後面接著真正的「第K卷」（K>1）→ 這段是第K-1卷（補第一卷）。
    同一段內章號又從第1章起算，代表中間換了卷，再往前（或往後）切開一卷。
    完全沒有卷標題也沒有卷結尾行的文件，不推定任何卷。
    """
    if state.collection_active or not (state.real_volume_numbers or state.volume_end_marks):
        return {}
    tree = ctx.tree
    real = state.real_volume_numbers
    raw_map = ctx.chapter_raw_map

    def formal_number(node):
        record = ctx.chapter_records.get(node)
        if not record or record['kind'] != 'chapter' or record['prefix'] == '番外':
            return None
        number = record['number']
        return int(number) if number and number > 0 and float(number).is_integer() else None

    events = []
    for node in tree.get_children(''):
        if node in real:
            events.append((raw_map.get(node, 0), 0, 'volume', node))
            events.extend((raw_map.get(child, 0), 1, 'item', child) for child in tree.get_children(node))
        elif node in raw_map:
            events.append((raw_map[node], 1, 'item', node))
    events.extend((row, 0, 'end', number) for row, number, _unit in state.volume_end_marks)
    events.sort(key=lambda event: (event[0], event[1]))

    blocks = []
    block = {'container': None, 'items': [], 'prev': None}
    for _row, _order, kind, payload in events:
        if kind == 'item':
            block['items'].append(payload)
            continue
        boundary = ('volume', real[payload]) if kind == 'volume' else ('end', payload)
        block['next'] = boundary
        blocks.append(block)
        block = {'container': payload if kind == 'volume' else None, 'items': [], 'prev': boundary}
    block['next'] = None
    blocks.append(block)

    unit = state.volume_end_marks[0][2] if state.volume_end_marks else '卷'
    if state.opts.num_style == '阿拉伯數字':
        arabic = True
    elif state.opts.num_style == '中文數字':
        arabic = False
    else:
        arabic = state.arabic_volume_numbers
    used = {number for number in real.values() if number}
    virtual = {}

    def segments(items):
        # 章號從第1章重新起算（前面已經超過第1章）就是換卷的位置。
        result, last = [[]], None
        for node in items:
            number = formal_number(node)
            if number is not None:
                if number == 1 and last is not None and last > 1 and result[-1]:
                    result.append([])
                last = number
            result[-1].append(node)
        return result

    def assign(seg, number):
        title = f"第{number if arabic else arabic_to_chinese(number)}{unit}"
        node = tree.insert('', 'end', text=title)
        for child in seg:
            tree.reparent(child, node)
        used.add(number)
        virtual[node] = {'title': title, 'number': number, 'row': raw_map[seg[0]]}

    for block in blocks:
        items, prev, nxt, container = block['items'], block['prev'], block['next'], block['container']
        formal = [i for i, node in enumerate(items) if formal_number(node) is not None]
        if not formal:
            continue
        # 全書開頭的簡介／序章、全書結尾的後記不屬於任何一卷。
        start = formal[0] if prev is None else 0
        end = formal[-1] + 1 if nxt is None else len(items)
        segs = segments(items[start:end])
        if container is not None:
            # 真正的第V卷底下章號重新起算、後面接著第N卷終（N>V）：
            # 後半段其實是沒寫卷標題的下一卷。
            current = real.get(container)
            if not (current and nxt and nxt[0] == 'end' and nxt[1] and nxt[1] > current):
                continue
            for offset, seg in enumerate(reversed(segs[1:])):
                number = nxt[1] - offset
                if number <= current or number in used:
                    break
                assign(seg, number)
            continue
        if nxt and nxt[0] == 'end' and nxt[1]:
            numbers = [nxt[1] - offset for offset in range(len(segs))][::-1]
        elif prev and prev[0] == 'end' and prev[1]:
            numbers = [prev[1] + 1 + offset for offset in range(len(segs))]
        elif nxt and nxt[0] == 'volume' and nxt[1] and nxt[1] > 1:
            numbers = [nxt[1] - 1 - offset for offset in range(len(segs))][::-1]
        else:
            continue
        limit = nxt[1] if nxt and nxt[0] == 'volume' and nxt[1] else None
        for seg, number in zip(segs, numbers):
            if number < 1 or number in used or (limit is not None and number >= limit):
                continue
            assign(seg, number)

    if virtual:
        order = dict(raw_map)
        order.update((node, info['row']) for node, info in virtual.items())
        tree.sort_children('', key=lambda node: order.get(node, 0))
        # 最後一個正式章節落在推定卷裡，檔名的「最新卷」就是這一卷。
        last_chapter = max((node for node in raw_map if formal_number(node) is not None),
                           key=lambda node: raw_map[node], default=None)
        if last_chapter is not None and tree.parent(last_chapter) in virtual:
            # 跟真正卷標題的「最新卷」同一種寫法（format_custom_title），檔名才一致。
            number = virtual[tree.parent(last_chapter)]['number']
            state.last_found_vol = ctx.format_custom_title('', '第', number, unit, '', False)
    return virtual


def build_document_structure(ctx: BuildContext, apply_format: bool = False,
                              write_text: bool = True) -> StructureResult:
    """協調辨識與輸出；各類標題由獨立處理函式更新 RenderState。

    write_text 只影響 chapter_index_map 的意義：write_text 為 False（或
    apply_format 為 False）時不會真的產生格式化文字，此時 chapter_index_map
    改為直接對應原始行號（raw_index + 1），而不是迴圈過程中暫時算出的
    「假設要重新輸出」位置——呼叫端若真的要重新輸出格式化文字，也必須自行
    重新以 apply_format=True、write_text=True 呼叫一次，那一輪算出的位置
    才是實際輸出後的正確位置。
    """
    state = RenderState()
    ctx.dialogue_depth = 0
    ctx.tree = SimpleTree()
    ctx.chapter_index_map = {}
    ctx.chapter_raw_map = {}
    state.opts = ctx.options
    opts = state.opts
    state.total = len(ctx.raw_lines)
    state.idx = 0
    state.current_lv1_node, state.last_found_vol, state.last_found_ch = ('', '', '')
    state.volume_nodes = {}
    state.collection_info = analyze_collection_structure(ctx.raw_lines, opts.structure)
    if opts.structure != '單本小說':
        for row, record in ctx.auto_titles.items():
            if record.get('kind') == 'work':
                state.collection_info['work_lines'][row] = record['title']
                state.collection_info['active'] = True
    state.collection_active = state.collection_info['active']
    state.collection_combined = state.collection_info['combined']
    state.collection_work_lines = state.collection_info['work_lines']
    state.work_nodes, state.work_volume_nodes = ({}, {})
    state.current_work_node, state.current_work_name = ('', '')
    state.processed_render_lines = []
    state.invalid_tail_regex = ctx.invalid_tail_regex
    while state.idx < state.total:
        title_raw_idx = state.idx
        raw_line = ctx.raw_lines[state.idx]
        line_str, persistent_marker = strip_persistent_title_marker(raw_line.strip())
        manual_marked = persistent_marker == 'include' and bool(line_str)
        excluded_marked = persistent_marker == 'exclude'
        mixed_data = None if excluded_marked else parse_mixed_volume_chapter_header(line_str)
        collection_record = state.collection_combined.get(state.idx) if not excluded_marked else None
        collection_work_name = state.collection_work_lines.get(state.idx) if not excluded_marked else None
        custom_title = None if excluded_marked else ctx.match_custom_title(line_str)
        stored_title = ctx.auto_titles.get(state.idx)
        if not stored_title and persistent_marker in {'auto_work', 'auto_title'}:
            stored_title = {'kind': 'work' if persistent_marker == 'auto_work' else 'chapter', 'title': line_str}
        forced_level = 1 if state.idx in ctx.force_lv1_chapters else 2 if state.idx in ctx.force_lv2_chapters else 0
        if forced_level:
            mixed_data = collection_record = collection_work_name = None
        is_title = not excluded_marked and (manual_marked or forced_level or stored_title or (mixed_data is not None) or (collection_record is not None) or (collection_work_name is not None) or (custom_title is not None) or is_valid_auto_title(line_str, state.invalid_tail_regex))
        end_mark = None
        if line_str and len(line_str) <= 40 and not (manual_marked or forced_level or custom_title):
            end_mark = parse_end_mark(line_str)
        if end_mark:
            if _is_end_line(state, end_mark):
                is_title = False
                if end_mark['level'] == 'volume' and not state.collection_active:
                    _close_volume(state, title_raw_idx, end_mark, line_str)
        elif is_title and (not (manual_marked or forced_level or custom_title)) and END_MARK_REGEX.search(line_str):
            is_title = False
        if is_title and title_raw_idx in ctx.ignored_chapters:
            is_title = False
        if not line_str:
            if apply_format:
                if not opts.remove_extra_empty:
                    state.processed_render_lines.append('')
            else:
                state.processed_render_lines.append('')
            state.idx += 1
            continue
        if apply_format and opts.remove_indent:
            line_str = line_str.lstrip()
        if is_title:
            if state.collection_active and collection_work_name:
                state.current_work_name = collection_work_name
                if state.current_work_name not in state.work_nodes:
                    state.current_work_node = ctx.tree.insert('', 'end', text=state.current_work_name, open=True)
                    state.work_nodes[state.current_work_name] = state.current_work_node
                    record_title(ctx, state, state.current_work_node, state.current_work_name,
                                 state.processed_render_lines, apply_format, title_raw_idx,
                                 manual_marked, auto_marker='auto_work')
                else:
                    state.current_work_node = state.work_nodes[state.current_work_name]
                    if not apply_format:
                        state.processed_render_lines.append(raw_line)
                state.current_lv1_node = ''
                state.idx += 1
                continue
            if state.collection_active and collection_record:
                render_collection_title(ctx, state, apply_format, collection_record, line_str, raw_line,
                                        title_raw_idx, manual_marked)
                continue
            if mixed_data:
                render_mixed_title(ctx, state, apply_format, mixed_data, title_raw_idx, manual_marked)
                continue
            m_spec = parse_special(line_str)
            m_lv1 = parse_lv1(line_str)
            m_lv2 = parse_lv2(line_str)
            is_lv1 = False
            is_lv2 = False
            if title_raw_idx in ctx.force_lv1_chapters:
                is_lv1 = True
            elif title_raw_idx in ctx.force_lv2_chapters:
                is_lv2 = True
            elif custom_title:
                is_lv1 = custom_title['level'] == 1
                is_lv2 = custom_title['level'] == 2
            elif stored_title:
                is_lv1 = stored_title['kind'] == 'volume'
                is_lv2 = not is_lv1
            elif m_lv1:
                is_lv1 = True
            elif m_lv2 and (manual_marked or not is_weak_numbered_title(line_str)):
                is_lv2 = True
            elif manual_marked and (not m_spec):
                is_lv2 = True
            if is_lv1:
                render_volume_title(ctx, state, apply_format, custom_title, forced_level, m_lv1, m_lv2,
                                    line_str, raw_line, title_raw_idx, manual_marked)
                continue
            if is_lv2:
                render_chapter_title(ctx, state, apply_format, custom_title, forced_level, m_lv1, m_lv2,
                                     line_str, title_raw_idx, manual_marked)
                continue
            if m_spec:
                render_special_title(ctx, state, apply_format, m_spec, line_str, title_raw_idx, manual_marked)
                continue
        body = raw_line
        if apply_format:
            body = ctx.format_punctuation_and_dialogue(body)
            if opts.remove_extra_spaces:
                # 段尾（半形／全形）空白全部刪掉；段落中間兩個中文字之間的空白
                # 直接刪掉，中英數之間收斂成一個半形空格（collapse_inline_spaces）。
                # 段首縮排不算「多餘」空格，要留著——要不要縮排交給「增加縮排」
                # ／「去除縮排」決定，不能因為勾了這項就把縮排也一併吃掉。
                content = body.strip()
                indent = body[:len(body) - len(body.lstrip())]
                body = indent + collapse_inline_spaces(content) if content else ''
            elif opts.auto_indent or opts.remove_indent:
                body = body.lstrip()
            if opts.auto_indent:
                body = '　　' + body.lstrip()
            elif opts.remove_indent:
                body = body.lstrip()
            state.processed_render_lines.append(body)
            if opts.add_paragraph_empty:
                state.processed_render_lines.append('')
        else:
            state.processed_render_lines.append(ctx.raw_lines[state.idx])
        state.idx += 1
    if apply_format:
        state.last_found_vol = ctx.format_punctuation_and_dialogue(state.last_found_vol, carry_depth=False)
        state.last_found_ch = ctx.format_punctuation_and_dialogue(state.last_found_ch, carry_depth=False)
    build_chapter_records(ctx, state.collection_info)
    virtual_volumes = infer_virtual_volumes(ctx, state)
    if not (write_text and apply_format):
        for item_id, raw_index in ctx.chapter_raw_map.items():
            ctx.chapter_index_map[item_id] = raw_index + 1
    return StructureResult(
        tree=ctx.tree,
        chapter_raw_map=ctx.chapter_raw_map,
        chapter_index_map=ctx.chapter_index_map,
        chapter_records=ctx.chapter_records,
        processed_render_lines=state.processed_render_lines,
        last_found_vol=state.last_found_vol,
        last_found_ch=state.last_found_ch,
        virtual_volumes=virtual_volumes,
    )
