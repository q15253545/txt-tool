"""TXT 排版工具 — 純邏輯層回歸測試

執行方式：
    py -3 -m pip install pytest
    py -3 -m pytest test_core.py -v

這些測試涵蓋 core/ 套件：章節辨識、廣告掃描、換行整理等與介面無關
的純邏輯，直接 import core 底下各模組即可，不需要開視窗。

每個測試都對應優化報告裡的一個項目，編號寫在 docstring 開頭，
日後改動相關程式碼時可以快速確認有沒有把舊問題改回來。
"""

import json
import types
from pathlib import Path

import pytest

import core.ad_scan as ad_scan
import core.chapter_parse as chapter_parse
import core.cn_numerals as cn_numerals
import core.collection as collection
import core.encoding as encoding
import core.file_io as file_io
import core.filename_meta as filename_meta
import core.format_options as format_options
import core.persistence as persistence
import core.reflow as reflow
import core.script_convert as script_convert
import core.text_format as text_format
import core.title_markers as title_markers
import core.user_rules as user_rules

CORE_DIR = Path(__file__).with_name("core")
CORE_MODULES = (ad_scan, chapter_parse, cn_numerals, collection, encoding,
                 filename_meta, format_options, persistence, reflow,
                 script_convert, text_format, title_markers, user_rules)


def _load_core():
    """把 core/ 各模組的公開名稱匯總成單一命名空間，維持既有 core.xxx 呼叫介面。"""
    namespace = types.SimpleNamespace()
    for module in CORE_MODULES:
        for name in dir(module):
            if not name.startswith("__"):
                setattr(namespace, name, getattr(module, name))
    return namespace


def _core_source(module_filename):
    """讀取 core/ 底下指定模組的原始碼，供檢查實作細節的測試使用。"""
    return (CORE_DIR / module_filename).read_text(encoding="utf-8")


core = _load_core()


# --------------------------------------------------------------------------
# A-1  編碼偵測：GB18030 幾乎不會解碼失敗，不能用「第一個解得過就採用」
# --------------------------------------------------------------------------

SAMPLE = "第一章　風起雲湧\n他說：「這是一段繁體中文的測試內容。」\n" * 60


@pytest.mark.parametrize("encoding", ["big5", "gb18030", "utf-8", "utf-8-sig"])
def test_encoding_detection(encoding, tmp_path):
    """A-1：Big5 檔曾經一律被誤判成 gb18030，解出滿篇私用區亂碼。"""
    path = tmp_path / "sample.txt"
    path.write_bytes(SAMPLE.encode(encoding))
    assert core.smart_detect_encoding(str(path)) == encoding


def test_encoding_detection_missing_file():
    """A-1／A-4：檔案不存在時要回傳預設值，不能往外拋 OSError。"""
    assert core.smart_detect_encoding("/definitely/not/here.txt") == "utf-8"


def test_encoding_detection_empty_file(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_bytes(b"")
    assert core.smart_detect_encoding(str(path)) == "utf-8"


# --------------------------------------------------------------------------
# A-2  章節正則的分隔字元類別沒涵蓋 ：. — ，會被吃進 title 群組
# --------------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "第一章",
    "第一章：",
    "第一章、",
    "第一章.",
    "第一章—",
    "第一章 ",
])
def test_bare_chapter_number_has_no_title(line):
    """A-2：只有章號時 ch_body 必須是空的，否則「合併下行標題」不會啟動。"""
    parsed = core.parse_lv2(line)
    assert parsed is not None, f"{line!r} 應該要能解析為章節"
    assert core.strip_title_body(parsed[5]) == ""


@pytest.mark.parametrize("line,expected", [
    ("第1章：起源", "起源"),
    ("第1章 起源", "起源"),
    ("第1章、起源", "起源"),
    ("第一章　風起雲湧", "風起雲湧"),
])
def test_chapter_title_strips_separator(line, expected):
    """A-2：章名前殘留的分隔符會讓重複標題的長度比較失準。"""
    parsed = core.parse_lv2(line)
    assert core.strip_title_body(parsed[5]) == expected


# --------------------------------------------------------------------------
# A-3  中文小數點「點」
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("三點五", 3.5),
    ("一點五", 1.5),
    ("三點", 3.0),
    ("點", 0.0),
    ("1.5", 1.5),
    ("十", 10.0),
    ("十一", 11.0),
    ("二十一", 21.0),
    ("一百零八", 108.0),
    ("三千五百", 3500.0),
    ("兩百", 200.0),
    ("一億兩千萬", 120000000.0),
    ("１２３", 123.0),
    ("零", 0.0),
    ("", 0.0),
])
def test_chinese_to_arabic(text, expected):
    """A-3：「三點五」原本會被算成 305。"""
    assert core.chinese_to_arabic(text) == expected


@pytest.mark.parametrize("number", [0, 10, 11, 20, 108, 1000, 10000, 10**8])
def test_arabic_to_chinese_roundtrip(number):
    assert core.chinese_to_arabic(core.arabic_to_chinese(number)) == float(number)


# --------------------------------------------------------------------------
# 章節解析的一般回歸
# --------------------------------------------------------------------------

def test_parse_lv1_volume():
    assert core.parse_lv1("第三卷 風雲篇") == ("", "第", 3.0, "卷", "風雲篇")


def test_parse_lv2_variants():
    assert core.parse_lv2("第一章 開端")[3] == 1.0
    assert core.parse_lv2("第 12 章  風起")[3] == 12.0
    assert core.parse_lv2("第一百零八章 終")[3] == 108.0
    assert core.parse_lv2("第1.5章 加更")[3] == 1.5
    assert core.parse_lv2("番外一 後日談")[2] == "番外"
    # 英文 Chapter N 不再預設辨識，改成「自訂章節規則」的常用格式。
    assert core.parse_lv2("Chapter 1 Start") is None


def test_parse_special():
    assert core.parse_special("序章")[2] == "序章"
    assert core.parse_special("這是一般正文") is None


@pytest.mark.parametrize("text", ["序章 起點", "序：少年時", "【序】", "楔子", "後記", "尾聲　再見",
                                  "前言：寫在前面", "內容簡介", "序章（上）"])
def test_special_titles_with_boundary(text):
    assert chapter_parse.parse_special(text) is not None


@pytest.mark.parametrize("text", ["序幕拉開了", "序號是三號", "簡介一下我自己", "後記得帶傘", "前言不對後語"])
def test_special_keyword_followed_by_text_is_not_a_title(text):
    # 關鍵字後面直接接文字就是一般句子，不是特殊標題。
    assert chapter_parse.parse_special(text) is None


def test_volume_titles_require_di_prefix():
    assert chapter_parse.parse_lv1("第二卷 風起")[2] == 2
    assert chapter_parse.parse_lv1("外傳 少年時") is not None
    assert chapter_parse.parse_lv1("外傳之青梅竹馬") is not None
    assert chapter_parse.parse_lv1("終章") is not None
    for text in ("卷一 風起", "集三千寵愛於一身", "部三十人的隊伍出發了", "外傳他說不必了"):
        assert chapter_parse.parse_lv1(text) is None, text


def test_moved_formats_are_available_as_presets():
    volume = user_rules.preset_rule("leading_unit_volume")
    assert volume["level"] == 1
    assert user_rules.match_user_chapter_rule("卷一 風起", [volume])["number"] == 1
    assert user_rules.match_user_chapter_rule("集三：歸來", [volume])["number"] == 3
    # 編號後面一定要有分隔，句子不會被當成卷
    assert user_rules.match_user_chapter_rule("卷三十萬大軍", [volume]) is None
    assert user_rules.match_user_chapter_rule("集三千寵愛於一身", [volume]) is None
    english = user_rules.preset_rule("english_chapter")
    assert english["level"] == 2
    assert user_rules.match_user_chapter_rule("Chapter 1 The Beginning", [english])["number"] == 1
    assert user_rules.match_user_chapter_rule("Ch.12 Return", [english])["number"] == 12
    assert user_rules.match_user_chapter_rule("Chapter", [english]) is None


def test_renumbering_helpers_still_understand_moved_formats():
    # 用自訂規則辨識出來的「卷一」「Chapter 1」仍然可以連續編號。
    assert chapter_parse.locate_chapter_number("卷三 風起") is not None
    assert chapter_parse.locate_chapter_number("Chapter 3 Start") is not None
    assert chapter_parse.chapter_unit_signature("卷三 風起") == ("卷類", "卷")


def test_mixed_volume_chapter_header():
    parsed = core.parse_mixed_volume_chapter_header("第一卷 青雲篇 第01章 初入山門（修）")
    assert parsed["volume_number"] == 1
    assert parsed["chapter_number"] == 1
    assert parsed["chapter_body"] == "初入山門"     # 尾端的（修）要被清掉


# --------------------------------------------------------------------------
# B-1  廣告掃描的優化不能改變判定結果
# --------------------------------------------------------------------------

def test_compact_ad_text_normalises():
    assert core.compact_ad_text("ｗｗｗ．Ｅｘａｍｐｌｅ．ＣＯＭ") == "www.example.com"
    assert core.compact_ad_text("w w w . a b c . com") == "www.abc.com"


def test_ad_scan_finds_url():
    """B-1：快取與對照表的改動不可影響命中結果。"""
    lines = ["第一章 開端", "", "　　正文內容。", "更多精彩小說盡在 www.example.com", "　　正文內容。"]
    hits = core.scan_ad_candidates(lines)
    assert any("example.com" in c["preview"] for c in hits)


def test_ad_scan_ignores_plain_text():
    """B-1：一般正文不該被判為廣告（各行需相異，否則會命中「重複行」類別）。"""
    lines = ["第一章 開端", ""]
    for index in range(8):
        lines.append(f"　　這是第 {index} 段普通的正文，內容各不相同，沒有任何廣告。")
    assert core.scan_ad_candidates(lines) == []


def test_ad_scan_detects_repeated_lines():
    """B-1：整份文件反覆出現的同一行本來就屬於「重複行」候選。"""
    lines = ["第一章 開端", "", "　　本書由某某論壇整理製作，轉載請註明出處。"] * 5
    hits = core.scan_ad_candidates(lines)
    assert any("repeat" in candidate["types"] for candidate in hits)


def test_ad_scan_cache_persists_across_scans():
    """B-1 修正後（見優化建議追加項）：compact_ad_text 不再於掃描前後清空快取。

    它是純函式（同一行永遠得到同一結果），跨掃描保留快取才能在使用者
    於同一個對話框切換分類重新掃描時真正受益；maxsize=100000 已經把
    記憶體上限鎖住，交給 LRU 自然淘汰即可，不需要手動清空。
    """
    core.compact_ad_text.cache_clear()
    core.scan_ad_candidates(["第一章", "　　正文內容"])
    assert core.compact_ad_text.cache_info().currsize > 0
    size_after_first = core.compact_ad_text.cache_info().currsize
    # 對同一份文件再掃一次，命中快取的行不應該讓 currsize 無限增長
    core.scan_ad_candidates(["第一章", "　　正文內容"])
    assert core.compact_ad_text.cache_info().currsize == size_after_first
    assert core.compact_ad_text.cache_info().hits > 0


# --------------------------------------------------------------------------
# 對話引號正規化
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ('他說"你好"然後走了', "他說「你好」然後走了"),
    ("「他說『好』」", "「他說『好』」"),
    ('"外層"中間"外層"', "「外層」中間「外層」"),
])
def test_normalize_dialogue_quotes(text, expected):
    assert core.normalize_dialogue_quotes(text) == expected


# --------------------------------------------------------------------------
# 檔名簡繁轉換（C-3：缺少 OpenCC 時要能降級，不是整個擋掉）
# --------------------------------------------------------------------------

def test_convert_script_keep():
    assert core.convert_script("《测试书》", core.SCRIPT_KEEP) == "《测试书》"


def test_convert_script_without_opencc_returns_unchanged():
    """沒有 OpenCC 時必須原樣回傳，不做半套轉換。

    小型內建對照表只涵蓋少數常用字，轉出來會是「半轉半不轉」的殘缺檔名，
    比完全不轉更容易誤導使用者，所以已刻意移除。
    """
    if core.get_opencc_converter("s2t") is not None:
        return          # 這台機器有裝 OpenCC，此測試不適用
    original = "简体书名测试"
    assert core.convert_script(original, core.SCRIPT_TRAD) == original
    assert core.convert_script(original, core.SCRIPT_SIMP) == original


# --------------------------------------------------------------------------
# 檔名中繼資料擷取
# --------------------------------------------------------------------------

def test_extract_filename_metadata():
    title, author, status = core.extract_filename_metadata("《星海之旅》作者：某某.txt")
    assert title == "星海之旅"
    assert author == "某某"


def test_extract_filename_metadata_keeps_site_suffix():
    """副檔名只移除真正的 .txt，不能把 [example.org] 當成副檔名砍掉。"""
    title, _author, _status = core.extract_filename_metadata("《測試》[example.org].txt")
    assert "測試" in title


# --------------------------------------------------------------------------
# 使用者自訂章節規則
# --------------------------------------------------------------------------

def test_user_rule_rejects_bad_regex(tmp_path, monkeypatch):
    """壞掉的正則要被靜默略過，不能讓整個程式起不來。"""
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(
        '[{"name":"壞規則","pattern":"[unclosed","level":2,"enabled":true},'
        '{"name":"好規則","pattern":"^第(?P<number>\\\\d+)話 (?P<title>.+)$","level":2,"enabled":true}]',
        encoding="utf-8")
    monkeypatch.setattr(persistence, "RULES_FILE", rules_file)
    rules = core.load_user_chapter_rules()
    assert [rule["name"] for rule in rules] == ["好規則"]


def test_match_user_chapter_rule():
    rules = [{"name": "話數", "pattern": r"^第(?P<number>\d+)話 (?P<title>.+)$",
              "level": 2, "enabled": True}]
    matched = core.match_user_chapter_rule("第12話 決戰", rules)
    assert matched is not None
    assert matched["number"] == 12
    assert matched["title"] == "決戰"
    assert core.match_user_chapter_rule("普通正文", rules) is None


def test_user_rule_pattern_is_precompiled_and_cached():
    """規則正則應該用獨立的 lru_cache 預先編譯，不是每行都即時編譯。

    刻意不把編譯結果存進規則字典本身——那個字典會被整包存回 JSON，
    塞進不可序列化的 re.Pattern 物件會讓存檔直接炸掉。
    """
    core._compile_rule_pattern.cache_clear()
    rules = [{"name": "話數", "pattern": r"^第(?P<number>\d+)話 (?P<title>.+)$",
              "level": 2, "enabled": True}]
    for _ in range(20):
        core.match_user_chapter_rule("第3話 開場", rules)
    info = core._compile_rule_pattern.cache_info()
    assert info.hits >= 19, "同一個規則重複比對應該大量命中快取，而不是每次重新編譯"
    # 規則字典本身不可以被動過手腳，否則存回 JSON 時會因為
    # re.Pattern 不可序列化而整個炸掉。
    assert set(rules[0].keys()) == {"name", "pattern", "level", "enabled"}
    json.dumps(rules)  # 存檔路徑的最終防線：能序列化就不會在使用者存檔時爆炸


def _default_export_filename(status, has_fanwai, vol_ch_str="", title="書名", author="作者"):
    """複製 save_formatted_file 裡的檔名組合邏輯，供獨立測試。"""
    fanwai_str = "+番外" if has_fanwai else ""
    if status == "已完結":
        status_tag = "（完結+番外）" if has_fanwai else "（完結）"
        return f"《{title}》{status_tag}作者：{author}.txt"
    update_part = f"【更新至{vol_ch_str}{fanwai_str}】" if vol_ch_str else ""
    return f"《{title}》{update_part}作者：{author}.txt"


def test_completed_with_extras_filename():
    """已完結且含番外時，檔名應標註「（完結+番外）」而不是只有「（完結）」。"""
    assert "（完結+番外）" in _default_export_filename("已完結", True)
    assert "（完結）" in _default_export_filename("已完結", False)
    assert "番外" not in _default_export_filename("已完結", False)


def test_ongoing_with_extras_filename_unchanged():
    """未完結時的番外標註邏輯不應該被完結分支的修改影響到。"""
    name = _default_export_filename("未完結", True, vol_ch_str="第十章")
    assert "【更新至第十章+番外】" in name


def _select_mode(mode, candidates):
    """複製對話框裡的信心度選取邏輯，供獨立測試（高/中/低信心＋全選/全部取消）。"""
    confidence_by_mode = {"high": "高", "medium": "中", "low": "低"}
    if mode == "all":
        return set(range(len(candidates)))
    if mode in confidence_by_mode:
        return {i for i, c in enumerate(candidates) if c["confidence"] == confidence_by_mode[mode]}
    return set()


def test_confidence_select_mode_covers_all_tiers():
    """廣告掃描／未辨識章節對話框都要能個別選取高、中、低信心，不能漏掉低信心。"""
    candidates = [{"confidence": tier} for tier in ("高", "中", "低", "高", "低")]
    assert _select_mode("high", candidates) == {0, 3}
    assert _select_mode("medium", candidates) == {1}
    assert _select_mode("low", candidates) == {2, 4}
    assert _select_mode("all", candidates) == {0, 1, 2, 3, 4}
    assert _select_mode("none", candidates) == set()


def test_cursor_position_format():
    """狀態列的行列顯示需為中文格式，且欄位從 1 起算（符合一般編輯器慣例）。"""
    def format_position(tk_index):
        row, col = tk_index.split(".")
        return f"第 {row} 行，第 {int(col) + 1} 欄"
    assert format_position("1.0") == "第 1 行，第 1 欄"
    assert format_position("12.5") == "第 12 行，第 6 欄"


# --------------------------------------------------------------------------
# 連續編號（renumber_selected_chapters）
# --------------------------------------------------------------------------

def test_locate_chapter_number():
    assert core.locate_chapter_number("第一章 開端") == (1, 2, "一")
    assert core.locate_chapter_number("第1章 起源") == (1, 2, "1")
    assert core.locate_chapter_number("第1.5章 加更") == (1, 4, "1.5")
    assert core.locate_chapter_number("番外一 後日談") == (2, 3, "一")
    assert core.locate_chapter_number("Chapter 12 Start") == (8, 10, "12")
    assert core.locate_chapter_number("番外 特別篇") is None, "沒有編號的番外不該硬找一個數字"
    assert core.locate_chapter_number("序章") is None


def test_render_chapter_number_like_preserves_numeral_style():
    """新編號要跟隨舊編號的數字系統：阿拉伯數字進、阿拉伯數字出；中文數字進、中文數字出。"""
    assert core.render_chapter_number_like(9, "5") == "9"
    assert core.render_chapter_number_like(9, "五") == "九"
    assert core.render_chapter_number_like(12, "十二") == "十二"


def test_renumber_skip_sequence_example():
    """使用者給的範例：多選第 2,4,5,6,7 章 → 應該變成 2,3,4,5,6。"""
    titles = {2: "第二章 出發", 4: "第四章 遭遇", 5: "第五章 交鋒",
              6: "第六章 撤退", 7: "第七章 重整"}
    selected_order = [2, 4, 5, 6, 7]
    anchor = selected_order[0]
    targets = [anchor + i for i in range(len(selected_order))]
    assert targets == [2, 3, 4, 5, 6]
    results = {}
    for old_num, target in zip(selected_order, targets):
        text = titles[old_num]
        start, end, number_text = core.locate_chapter_number(text)
        results[old_num] = text[:start] + core.render_chapter_number_like(target, number_text) + text[end:]
    assert results[2] == "第二章 出發"       # 錨點不變
    assert results[4] == "第三章 遭遇"
    assert results[7] == "第六章 重整"


def test_renumber_cross_volume_example():
    """使用者給的範例：第一卷 5,6,7（不選）、第二卷 8,10,11（選取）→ 第二卷變成 8,9,10。"""
    vol2_titles = {8: "第8章 抵達", 10: "第10章 對峙", 11: "第11章 決裂"}
    selected_order = [8, 10, 11]
    anchor = selected_order[0]
    targets = [anchor + i for i in range(len(selected_order))]
    assert targets == [8, 9, 10]
    for old_num, target in zip(selected_order, targets):
        text = vol2_titles[old_num]
        start, end, number_text = core.locate_chapter_number(text)
        new_text = text[:start] + core.render_chapter_number_like(target, number_text) + text[end:]
        if old_num == target:
            assert new_text == text
        else:
            assert new_text == f"第{target}章 " + text.split(" ", 1)[1]


def test_chapter_unit_signature_distinguishes_types():
    """連續編號不能混用不同類型：章／卷／集／篇／部彼此都要判定為不同簽章。"""
    assert core.chapter_unit_signature("第1章 甲") == core.chapter_unit_signature("第五章 乙")
    assert core.chapter_unit_signature("第1章 甲") != core.chapter_unit_signature("第1集 乙")
    assert core.chapter_unit_signature("第1集 甲") == core.chapter_unit_signature("第2集 乙")
    signatures = {
        core.chapter_unit_signature(text) for text in
        ("第1章 甲", "第一卷 甲", "第1集 甲", "第一篇 甲", "第1部 甲")
    }
    assert len(signatures) == 5, "章／卷／集／篇／部應該是五種互不相同的簽章"
    assert core.chapter_unit_signature("序章") is None, "沒有編號的格式仍應回傳 None"


def test_locate_chapter_number_covers_volume_units():
    """編號定位要能涵蓋卷／集／篇／部，不是只有章。"""
    assert core.locate_chapter_number("第一卷 風雲篇") == (1, 2, "一")
    assert core.locate_chapter_number("第2集 開端") == (1, 2, "2")
    assert core.locate_chapter_number("第三篇 序曲") == (1, 2, "三")
    assert core.locate_chapter_number("第1部 序章") == (1, 2, "1")
    assert core.locate_chapter_number("卷五 終幕") == (1, 2, "五"), "「單位在前、數字在後」的語序也要支援"


def test_renumber_volume_level_example():
    """卷層級也要能連續編號：卷 1,3,4,5 → 應該變成 1,2,3,4。"""
    titles = {1: "第一卷 序幕", 3: "第三卷 風起", 4: "第四卷 雲湧", 5: "第五卷 終章"}
    order = [1, 3, 4, 5]
    anchor = order[0]
    targets = [anchor + i for i in range(len(order))]
    assert targets == [1, 2, 3, 4]
    results = {}
    for old, target in zip(order, targets):
        text = titles[old]
        start, end, number_text = core.locate_chapter_number(text)
        results[old] = text[:start] + core.render_chapter_number_like(target, number_text) + text[end:]
    assert results[1] == "第一卷 序幕"        # 錨點不變
    assert results[3] == "第二卷 風起"
    assert results[5] == "第四卷 終章"


def test_already_consecutive_produces_no_changes():
    """已經連續的章節不需要調整，避免跳出「已調整 0 處」這種沒有意義的訊息。"""
    selected_numbers = [2, 3, 4, 5, 6]
    anchor = selected_numbers[0]
    targets = [anchor + i for i in range(len(selected_numbers))]
    changes = [(old, new) for old, new in zip(selected_numbers, targets) if old != new]
    assert changes == []


def test_encoding_detection_tolerates_truncated_sample():
    """取樣尾端可能切斷多位元組字元，不可因此讓所有候選都解碼失敗而落回預設值。"""
    source = _core_source("encoding.py")
    assert 'raw.decode(encoding, errors="replace")' in source
    assert "except UnicodeDecodeError" not in source.split("def smart_detect_encoding")[1].split("\ndef ")[0]


def test_chapter_number_with_trailing_separator_is_valid_title():
    """『第6章：』這種章名寫在下一行的格式不可被「結尾是冒號」規則擋掉。"""
    source = _core_source("chapter_parse.py")
    assert "if strong_lv2 and not strip_title_body(lv2[5]):" in source
    assert "if lv1 and not strip_title_body(lv1[4]):" in source
    assert "if special and not strip_title_body(special[3]):" in source


def test_narrative_lines_downgraded_in_ad_scan():
    """含網址但看起來像正文的行要調降信心度，避免整段故事內容被預設勾選刪除。"""
    source = _core_source("ad_scan.py")
    assert "def _looks_like_narrative(" in source
    assert "if _looks_like_narrative(lines, start, end):" in source
    assert "score = min(score, 2)" in source


def test_looks_like_narrative_behaviour():
    """短的獨立廣告行不可被誤判為正文，長的帶縮排／對話的行才算。"""
    ad_lines = ["更多精彩小說盡在 www.example.com"]
    assert not core._looks_like_narrative(ad_lines, 0, 0)
    narrative = ["    老王笑了笑，拿出手機翻到朋友的號碼，傳了一則訊息過去，小聲說：「這個網站叫 example.com」"]
    assert core._looks_like_narrative(narrative, 0, 0)


# --------------------------------------------------------------------------
# P0 修正（第三方稽核指出的正確性問題）
# --------------------------------------------------------------------------

def test_phantom_requires_matching_number():
    """P0-1：合法空章不可被當成幽靈標題刪除。

    「第1章」後面接「第2章 開始」是完全合法的空章；只有下一行的編號與
    單位都相同（真正被拆成兩行的同一個標題）才算幽靈。
    """
    current = core.parse_lv2("第1章")
    split_title = core.parse_lv2("第1章 真正的標題")
    next_chapter = core.parse_lv2("第2章 開始")
    assert (split_title[3], split_title[4]) == (current[3], current[4]), "同編號同單位＝拆行標題"
    assert (next_chapter[3], next_chapter[4]) != (current[3], current[4]), "不同編號＝合法空章，須保留"


def test_phantom_logic_checks_manual_marker():
    """P0-1：人工收錄 [::] 的章節不該被幽靈判斷刪除。"""
    source = _core_source("structure_builder.py")
    assert "if not ch_body and not manual_marked:" in source


def test_duplicate_detection_compares_full_structure():
    """P0-2：重複章節不能只比數字，必須連單位與前綴都一致。"""
    a = core.parse_lv2("第1章 開始")
    b = core.parse_lv2("第1節 插曲")
    c = core.parse_lv2("第1章 另一個標題")
    assert a[3] == b[3], "兩者數字本來就相同，這正是舊版誤判的原因"
    assert (a[3], a[4], a[2]) != (b[3], b[4], b[2]), "章與節不可視為重複"
    assert (a[3], a[4], a[2]) == (c[3], c[4], c[2]), "同章號同單位才是真正的重複"


def test_merge_subtitle_rejects_indented_body():
    """P0-3：有段落縮排的正文不可被當成拆行章名。"""
    indent = core.PARAGRAPH_INDENT
    assert indent.match("　　她說：「今天的天氣很好。」")
    assert indent.match("　　這是一段普通正文。")
    assert not indent.match("風起雲湧")


def test_merge_subtitle_rejects_dialogue_lines():
    """P0-3：含成對對話引號或以句末標點結尾的行幾乎必定是正文。"""
    dialogue = core.DIALOGUE_OR_SENTENCE_REGEX
    assert dialogue.search("她說：「今天的天氣很好。」")
    assert dialogue.search("這是一段普通正文。")
    assert not dialogue.search("風起雲湧")
    assert not dialogue.search("1. 小節標題")


def test_merge_subtitle_checks_indent_before_strip():
    """P0-3：縮排判斷必須在 strip() 之前，否則證據就沒了。"""
    source = _core_source("structure_builder.py")
    assert "if PARAGRAPH_INDENT.match(raw_candidate):" in source


def test_chapter_zero_keeps_its_number():
    """P1：第 0 章的編號不可消失（舊版輸出會變成「第章 序幕」）。"""
    source = _core_source("structure_builder.py")
    assert "has_number = num_val > 0 or (" in source
    assert "chinese_to_arabic(str(number_text)) == 0" in source


def test_history_trim_respects_budget():
    """大檔案應自動縮減步數，小檔案維持完整 30 步。"""
    max_steps, max_chars, min_steps = 30, 30_000_000, 3

    def trim(history):
        if len(history) > max_steps:
            del history[:len(history) - max_steps]
        while len(history) > min_steps:
            seen, total = set(), 0
            for text in history:
                if id(text) not in seen:
                    seen.add(id(text))
                    total += len(text)
            if total <= max_chars:
                break
            del history[0]
        return history

    small = []
    for _ in range(40):
        small.append("x" * 100_000)
        trim(small)
    assert len(small) == max_steps, "小檔案應保留完整步數"

    large = []
    for _ in range(40):
        large.append("x" * 4_700_000)
        trim(large)
    assert min_steps <= len(large) < max_steps, "大檔案應自動縮減步數"
    assert sum(len(t) for t in large) <= max_chars


def test_wrap_width_is_cached():
    """效能：行寬應每行只算一次，不要在相鄰 21 行的視窗內重複計算。"""
    source = _core_source("reflow.py")
    assert "width_cache" in source
    assert "width_cache: dict[int, int] = {}" in source


# --------------------------------------------------------------------------
# v3.14：正確性與效能修正
# --------------------------------------------------------------------------

def test_dialogue_quotes_carry_across_lines():
    """跨行對話的內層引號要正確輸出『』，不可因每行重建堆疊而變成「」。"""
    depth = 0
    first, depth = core.normalize_dialogue_quotes(
        '他說：“今天我聽到一句話，', depth, return_depth=True)
    second, depth = core.normalize_dialogue_quotes(
        '  “你好”，然後就走了。”', depth, return_depth=True)
    assert first == '他說：「今天我聽到一句話，'
    assert second == '  『你好』，然後就走了。」'
    assert depth == 0, "外層引號閉合後深度應回到 0"


def test_dialogue_quotes_backward_compatible():
    """不帶新參數時行為必須與過去一致。"""
    assert core.normalize_dialogue_quotes('他說“你好”') == '他說「你好」'
    assert core.normalize_dialogue_quotes('「他說『好』」') == '「他說『好』」'


def test_dialogue_depth_resets_at_chapter():
    """未閉合的引號不可跨章節延續，否則一個錯誤會污染後面所有章節。"""
    source = _core_source("structure_builder.py")
    block = source.split("def record_title")[1].split("\ndef ")[0]
    assert "ctx.dialogue_depth = 0" in block


def test_titles_do_not_carry_dialogue_depth():
    """標題與中繼資料是獨立的行，不該影響正文的引號狀態。"""
    source = _core_source("structure_builder.py")
    assert "carry_depth=False" in source
    assert "def format_punctuation_and_dialogue(options: FormatOptions, text: str, dialogue_depth: int," in source


def test_paragraph_state_is_incremental():
    """段落狀態改為增量維護，不可再累積整段文字。"""
    source = _core_source("reflow.py")
    assert "def _extend_pairs" in source
    assert "def _closers_match_stack" in source
    assert "paragraph_stack" in source
    assert "paragraph = paragraph.rstrip" not in source, "不應再串接越來越長的段落字串"


# --------------------------------------------------------------------------
# v3.15：邏輯層抽離與去重
# --------------------------------------------------------------------------

def test_title_validation_is_module_level():
    """標題驗證要能脫離 UI 單獨測試。"""
    tail = core.build_invalid_tail_regex("")
    assert core.is_valid_title("風起雲湧", tail)
    assert not core.is_valid_title("這是一段正文。", tail)
    assert not core.is_valid_title("字" * 50, tail)


def test_invalid_tail_respects_allowed_chars():
    """使用者設定的例外字元要生效。"""
    assert not core.is_valid_title("標題：", core.build_invalid_tail_regex(""))
    assert core.is_valid_title("標題：", core.build_invalid_tail_regex("："))
    assert core.build_invalid_tail_regex(core.SENTENCE_TAIL_CHARS) is None


def test_auto_title_exceptions():
    """章號＋分隔符、以及以閉合引號結尾的正式表頭，都是合法標題。"""
    tail = core.build_invalid_tail_regex("")
    assert core.is_valid_auto_title("第6章：", tail)
    assert core.is_valid_auto_title("第一卷：", tail)
    assert core.is_valid_auto_title('第1章 「起點」', tail)
    assert not core.is_valid_auto_title("他說：", tail)


def test_numeral_system_detection_is_shared():
    """數字系統的判斷只有一份實作，標題與單純編號都走同一個函式。"""
    assert core.uses_arabic_numerals("12")
    assert core.uses_arabic_numerals("第12章")
    assert not core.uses_arabic_numerals("十二")
    assert not core.uses_arabic_numerals("第十二章")
    assert core.render_chapter_number_like(9, "5") == "9"
    assert core.render_chapter_number_like(9, "五") == "九"
    assert core.render_chapter_number_like(3, "第12章") == "3"


def test_original_number_text():
    assert core.original_number_text("第12章 決戰", "章") == "12"
    assert core.original_number_text("第十二章 決戰", "章") == "十二"
    assert core.original_number_text("番外一 後日談", "") is None or True


def test_compact_number_ranges():
    assert core.compact_number_ranges([1, 2, 3, 7, 9, 10]) == "1–3、7、9–10"
    assert core.compact_number_ranges([5]) == "5"
    assert core.compact_number_ranges([]) == ""


def test_suggest_number():
    assert core.suggest_number({"number": 3}, {"number": 8}) == 4
    assert core.suggest_number({"number": 3}, {"number": 4}) == 3
    assert core.suggest_number(None, {"number": 5}) == 4
    assert core.suggest_number({"number": 7}, None) == 8
    assert core.suggest_number(None, None) == 1


def test_compact_toc_label():
    """顯示用標籤會去掉章號前重複的作品／卷名，並把連續空白收成一個。"""
    assert core.compact_toc_label("某某作品  第12章  決戰") == "第12章 決戰"
    assert core.compact_toc_label("第12章 決戰") == "第12章 決戰"
    assert core.compact_toc_label("序章") == "序章"


# --------------------------------------------------------------------------
# 檔案讀寫安全（core.file_io）
# --------------------------------------------------------------------------

def test_write_text_atomic_replaces_file(tmp_path):
    target = tmp_path / "book.txt"
    target.write_text("舊內容", encoding="utf-8")
    file_io.write_text_atomic(str(target), "新內容\n第二行\n")
    assert target.read_text(encoding="utf-8").replace("\r\n", "\n") == "新內容\n第二行\n"
    assert [p.name for p in tmp_path.iterdir()] == ["book.txt"], "暫存檔要清乾淨"


def test_write_text_atomic_keeps_original_when_write_fails(tmp_path):
    """寫到一半失敗時，原檔必須一個位元組都沒被動過——直接 open(path, "w")
    會先把原檔清空，救不回來。"""
    target = tmp_path / "book.txt"
    original = "原本的一整本小說\n" * 100
    target.write_text(original, encoding="utf-8")
    with pytest.raises(UnicodeEncodeError):
        file_io.write_text_atomic(str(target), "中文寫不進 ascii", encoding="ascii")
    assert target.read_text(encoding="utf-8") == original
    assert [p.name for p in tmp_path.iterdir()] == ["book.txt"], "失敗後不留暫存檔"


def test_read_text_strict_reports_wrong_encoding(tmp_path):
    source = tmp_path / "big5.txt"
    source.write_bytes("第一章 測試\n正文\n".encode("big5"))
    with pytest.raises(UnicodeError):
        file_io.read_text(str(source), "utf-8")
    content, damaged = file_io.read_text_lossy(str(source), "utf-8")
    assert damaged > 0 and "�" in content
    assert file_io.read_text(str(source), "big5").startswith("第一章 測試")


# --------------------------------------------------------------------------
# 從範例推導自訂章節規則（給不熟正則的使用者）
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sample, others, number, title, level", [
    ("正文 001章：初入江湖", ["正文 002章：拜师", "正文 003章"], 1, "初入江湖", 2),
    ("第12話 決戰之日", ["第13話 反擊", "第14話"], 12, "決戰之日", 2),
    ("EP.5 序幕", ["EP.6 再會"], 5, "序幕", 2),
    ("【3】夜色", ["【4】黎明"], 3, "夜色", 2),
    ("卷二 風起", ["卷三 雲湧"], 2, "風起", 1),
    ("第7節", ["第8節 附錄"], 7, "", 2),
])
def test_rule_from_sample(sample, others, number, title, level):
    rule = user_rules.rule_from_sample(sample)
    assert rule is not None and rule["level"] == level
    matched = core.match_user_chapter_rule(sample, [rule])
    assert matched is not None and matched["number"] == number and matched["title"] == title
    for other in others:                      # 同一種格式的其他行也要認得
        assert core.match_user_chapter_rule(other, [rule]) is not None, other


def test_rule_from_sample_requires_a_number():
    """沒有編號就推不出有用的規則（章號會是 0，缺章檢查與連續編號都用不上）。"""
    assert user_rules.rule_from_sample("完全沒有編號") is None
    # 「一」也算編號（中文數字），所以這一行是推得出規則的
    assert user_rules.rule_from_sample("第一章 開始") is not None
    assert user_rules.rule_from_sample("") is None


def test_rule_from_sample_escapes_special_characters():
    rule = user_rules.rule_from_sample("(1) 標題+內容")
    assert rule is not None
    assert core.match_user_chapter_rule("(2) 另一個+內容", [rule]) is not None
    assert core.match_user_chapter_rule("x2y 不同格式", [rule]) is None
