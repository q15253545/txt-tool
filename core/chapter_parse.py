"""單行章節標題辨識：正則、有效性判斷、編號轉換與標題清理。"""

import re
import unicodedata

from .cn_numerals import chinese_to_arabic, arabic_to_chinese

CN_NUM_PATTERN = r"[0-9０-９一二兩两三四五六七八九十百千萬万億亿兆〇零]+"
CN_NUM_FLOAT_PATTERN = CN_NUM_PATTERN + r"(?:[\.．]\d+)?"
CN_NUM_FLOAT_OPT_PATTERN = r"[0-9０-９一二兩两三四五六七八九十百千萬万億亿兆〇零]*(?:[\.．]\d+)?"

SEP = r"[ \t:：]+"

# 章節前面常見、但沒有任何意義的前綴：網站匯出的 TXT 常在每一章前面加上
# 「正文」「VIP章節」。規則會把「第N章」前面的字當成篇名／書名保留下來
# （合集的「某書 第一章」需要），這些字卻只是雜訊——排版後有的章留著
# 「正文」、有的章沒有（例如一部分走自訂規則），整本書就不一致了。
NOISE_TITLE_PREFIXES = frozenset(prefix.casefold() for prefix in (
    "正文", "正文卷", "正文部分", "正文篇", "作品正文", "作品正文卷",
    "內容", "内容", "章節", "章节", "本章", "全文",
    "VIP章節", "VIP章节", "VIP卷", "VIP", "免費章節", "免费章节", "最新章節", "最新章节",
))


def is_noise_prefix(text: str) -> bool:
    return bool(text) and text.strip().casefold() in NOISE_TITLE_PREFIXES


def _clean_arc(arc: str) -> str:
    """「第N章」前面的篇名：是雜訊前綴就當成沒有。"""
    return "" if is_noise_prefix(arc) else arc
ARC_PATTERN = rf"(?:(?P<arc>[^，。！？：；\.,!?;”’\n\(\)\[\]]{{1,15}}){SEP})?"
VOL_PATTERN = rf"(?:(?P<volume>第{CN_NUM_PATTERN}\s*[部卷篇集]){SEP})?"

# 關鍵字後面必須是行尾、空白、分隔符號或括號，不能直接接著文字：
# 否則「序幕拉開了」「簡介一下我自己」「後記得……」這種句子都會被當成
# 特殊標題（實測預設規則會把它們全部收進目錄）。
_TAG_BOUNDARY = r"(?=$|[\s　:：、．.·・\-—_~～（(【\[「『《〈】\]\)）」』》〉])"

COMBO_SPECIAL_REGEX = re.compile(
    r"^[\s【\[\(-]*" + ARC_PATTERN + VOL_PATTERN +
    r"(?P<tag>前言|簡介|简介|人物簡介|人物简介|序章|序言|序|楔子|引子|後記|后记|尾聲|尾声|內容簡介|内容简介)"
    + _TAG_BOUNDARY +
    r"[\s】\]\)-]*(?P<title>.*)$", re.IGNORECASE)

LV1_A_REGEX = re.compile(
    r"^[\s【\[\(-]*" + ARC_PATTERN +
    r"(?:(?P<prefix>第)\s*(?P<number>" + CN_NUM_PATTERN + r")\s*(?P<unit>[部卷篇集])|(?P<leading_unit>[部卷篇集])\s*(?P<trailing_number>" + CN_NUM_PATTERN + r"))"
    r"[\s】\]\)-]*(?P<title>.*)$", re.IGNORECASE)

# 外傳／終章同樣要有分隔，但多允許一個「之」：「外傳之青梅竹馬」是常見寫法，
# 「外傳他說不必了」則是正文。
LV1_B_REGEX = re.compile(
    r"^[\s【\[\(-]*" + ARC_PATTERN +
    r"(?P<unit>(?:外傳|外传|終章|终章)(?=之|" + _TAG_BOUNDARY[3:] + r"|分卷[\s:：])"
    r"[\s】\]\)-]*(?P<title>.*)$", re.IGNORECASE)

COMBO_LV2_REGEX = re.compile(
    r"^[\s【\[\(-]*" + ARC_PATTERN + VOL_PATTERN +
    rf"第\s*(?P<number>{CN_NUM_FLOAT_PATTERN})\s*(?P<unit>[章回節节折幕])"
    r"[\s】\]\)-]*(?P<title>.*)$", re.IGNORECASE)

COMBO_LV2_EXTRA_REGEX = re.compile(
    r"^[\s【\[\(-]*" + ARC_PATTERN + VOL_PATTERN +
    rf"番外\s*(?P<number>{CN_NUM_FLOAT_OPT_PATTERN})\s*(?P<unit>[章回節节]?)\s*(?P<title>.*)$", re.IGNORECASE)

COMBO_LV2_NUM_REGEX = re.compile(
    r"^[\s【\[\(-]*" + ARC_PATTERN + VOL_PATTERN +
    r"(?:Chapter|Chap|Ch)\.?\s*(?P<number>\d+(?:[\.．]\d+)?)[\s】\]\)-]*(?P<title>.*)$", re.IGNORECASE)

SUBTITLE_NUMBER_PATTERN = r"(?:[0-9０-９]+|[一二兩两三四五六七八九十百千萬万億亿兆〇零]+)"
WEAK_HASH_TITLE_REGEX = re.compile(
    r"^\s*[#＃]\s*(" + SUBTITLE_NUMBER_PATTERN +
    r")\s*(?:[\.．,，、:：\-—]+\s*)?(.+?)\s*$"
)
WEAK_BRACKET_TITLE_REGEX = re.compile(
    r"^\s*[\(（\[]\s*(" + SUBTITLE_NUMBER_PATTERN +
    r")\s*[\)）\]]\s*(?:[\.．,，、:：\-—]+\s*)?(.+?)\s*$"
)
WEAK_PLAIN_TITLE_REGEX = re.compile(
    r"^\s*(" + SUBTITLE_NUMBER_PATTERN +
    r")\s*([\.．,，、:：\-—]+|\s+)\s*(.+?)\s*$"
)


def parse_weak_numbered_title(text):
    """共用弱格式解析器：供次行章名合併與稀有章節掃描使用。"""
    for style, regex in (
        ("井號", WEAK_HASH_TITLE_REGEX),
        ("括號", WEAK_BRACKET_TITLE_REGEX),
        ("一般", WEAK_PLAIN_TITLE_REGEX),
    ):
        match = regex.match(text)
        if not match:
            continue
        if style == "一般":
            number_text, separator, body = match.group(1), match.group(2), match.group(3)
            normalized_separator = unicodedata.normalize("NFKC", separator).strip()
            style_key = f"一般:{normalized_separator or '空格'}"
        else:
            number_text, body = match.group(1), match.group(2)
            style_key = style
        number = chinese_to_arabic(number_text)
        if number <= 0 or not float(number).is_integer() or not body.strip():
            return None
        return {
            "number": int(number),
            "number_text": number_text,
            "body": body.strip(),
            "style": style,
            "style_key": style_key,
        }
    return None


def is_weak_numbered_title(text):
    return parse_weak_numbered_title(text) is not None


def weak_candidate_to_user_rule(candidate):
    """將弱格式候選轉為可保存的完整行自訂規則。"""
    number = r"(?P<number>" + SUBTITLE_NUMBER_PATTERN + r")"
    title = r"(?P<title>.+?)"
    style_key = candidate.get("style_key", candidate.get("style", "一般"))
    if style_key == "井號":
        pattern = (r"^\s*[#＃]\s*" + number
                   + r"\s*(?:[\.．,，、:：\-—]+\s*)?" + title + r"\s*$")
    elif style_key == "括號":
        pattern = (r"^\s*[\(（\[]\s*" + number + r"\s*[\)）\]]\s*"
                   + r"(?:[\.．,，、:：\-—]+\s*)?" + title + r"\s*$")
    else:
        separator = style_key.split(":", 1)[1] if ":" in style_key else "空格"
        separator_patterns = {
            "空格": r"\s+", ".": r"\s*[\.．]+\s*", ",": r"\s*[,，]+\s*",
            "、": r"\s*、+\s*", ":": r"\s*[:：]+\s*", "-": r"\s*[\-—]+\s*",
        }
        separator_pattern = separator_patterns.get(separator, r"\s*" + re.escape(separator) + r"+\s*")
        pattern = r"^\s*" + number + separator_pattern + title + r"\s*$"
    return {
        "name": f"未辨識格式－{style_key}",
        "pattern": pattern,
        "level": 2,
        "enabled": True,
    }


def parse_lv2(line):
    # 自動辨識只收正規格式（第N章／回／節…、番外）。英文的 Chapter N 已經改成
    # 「自訂章節規則」裡的常用格式，要用的人自己勾；COMBO_LV2_NUM_REGEX 仍留給
    # 連續編號、保留標題間隔這些「已經確定是標題」之後的處理使用。
    for regex, prefix in ((COMBO_LV2_REGEX, "第"), (COMBO_LV2_EXTRA_REGEX, "番外")):
        match = regex.match(line)
        if match:
            fields = match.groupdict(default="")
            return (_clean_arc(fields["arc"]), fields["volume"], prefix,
                    chinese_to_arabic(fields["number"]) if fields["number"] else 0.0,
                    fields.get("unit", "章"), fields["title"])
    weak = parse_weak_numbered_title(line)
    if weak:
        return ("", "", "第", weak["number"], "章", weak["body"])
    return None


def parse_lv1(line):
    m = LV1_A_REGEX.match(line)
    # 只收「第N卷／部／篇／集」。不帶「第」的「卷一」「集三」後面不需要任何分隔，
    # 「集三千寵愛於一身」「部三十人的隊伍出發了」都會被當成卷標題；改成
    # 「自訂章節規則」的常用格式（那裡要求編號後面有分隔）。LV1_A_REGEX 本身
    # 仍保留兩種語序，給連續編號這類「已經確定是標題」之後的處理使用。
    if m and m.group("number"):
        fields = m.groupdict(default="")
        return (_clean_arc(fields["arc"]), fields["prefix"] or "第", chinese_to_arabic(fields["number"]),
                fields["unit"], fields["title"])

    m = LV1_B_REGEX.match(line)
    if m:
        fields = m.groupdict(default="")
        return (_clean_arc(fields["arc"]), "", 0.0, fields["unit"], fields["title"])
    return None


MAX_TITLE_LENGTH = 45
SENTENCE_TAIL_CHARS = "，。：；.,;”’"
CLOSING_QUOTE_TAIL_REGEX = re.compile(r"[”’」』]\s*$")


def build_invalid_tail_regex(allowed_chars=""):
    """組出「標題不可用這些標點結尾」的樣式；allowed_chars 是使用者設定的例外。"""
    blocked = set(SENTENCE_TAIL_CHARS) - set(allowed_chars or "")
    if not blocked:
        return None
    return re.compile(f"[{re.escape(''.join(sorted(blocked)))}]\\s*$")


def is_valid_title(text, invalid_tail_regex):
    """標題的基本限制：不可過長，且不可用句末標點結尾。"""
    if len(text) > MAX_TITLE_LENGTH:
        return False
    return not (invalid_tail_regex and invalid_tail_regex.search(text))


def is_valid_auto_title(text, invalid_tail_regex):
    """自動辨識用的標題判斷，比 is_valid_title 多兩種例外。

    例外一：正式表頭以閉合引號結尾，例如『第1章 「起點」』。
    例外二：只有章號、章名寫在下一行的格式，例如『第6章：』。結尾的分隔符
            不是句末標點，而章號前綴本身就是夠強的結構證據。
    """
    if is_valid_title(text, invalid_tail_regex):
        return True
    if len(text) > MAX_TITLE_LENGTH:
        return False

    lv2 = parse_lv2(text)
    strong_lv2 = bool(lv2 and not is_weak_numbered_title(text))
    lv1 = parse_lv1(text)
    special = parse_special(text)

    if CLOSING_QUOTE_TAIL_REGEX.search(text) and (lv1 or special or strong_lv2):
        return True
    if strong_lv2 and not strip_title_body(lv2[5]):
        return True
    if lv1 and not strip_title_body(lv1[4]):
        return True
    if special and not strip_title_body(special[3]):
        return True
    return False


def parse_special(line):
    m = COMBO_SPECIAL_REGEX.match(line)
    if m:
        fields = m.groupdict(default="")
        return (_clean_arc(fields["arc"]), fields["volume"], fields["tag"], fields["title"])
    return None


# 「連續編號」只信任這三種能明確標出 number 群組位置的標準格式；自訂規則、
# 弱格式、不含編號的番外等一律回傳 None，交由呼叫端略過，不冒然改寫。
_CHAPTER_NUMBER_REGEXES = (COMBO_LV2_REGEX, COMBO_LV2_EXTRA_REGEX, COMBO_LV2_NUM_REGEX)


def looks_like_auto_chapter(text, user_rules=()):
    """這一行沒有任何標記時，自動辨識會不會把它當成章節？

    用來決定「把某一行移出目錄」要不要真的寫 [::X]：如果它本來就不會被
    認成章節（例如使用者手動用 [::] 加進來的一段正文），那只要把標記拿掉
    就夠了，不必在檔案裡留一個沒有意義的 [::X]。

    弱格式（「6.標題」這種純數字開頭）在這裡算「不是章節」：它們要在
    「疑似章節」確認過才會收進目錄，光靠格式不會自動成章。"""
    from .user_rules import match_user_chapter_rule   # 延後匯入，避免循環相依

    clean = text.strip()
    if not clean:
        return False
    if user_rules and match_user_chapter_rule(clean, user_rules):
        return True
    if parse_lv1(clean) or parse_special(clean) or parse_mixed_volume_chapter_header(clean):
        return True
    if is_weak_numbered_title(clean):
        return False
    return bool(parse_lv2(clean))


def locate_chapter_number(text):
    """在標題文字中找出章節／卷編號子字串的確切範圍，供原地替換數字用。

    先試章節（章／回／節／折／幕／番外／Chapter N），再試卷層級（卷／集／
    篇／部）；卷層級有兩種語序：「第X卷」（number 群組）或「卷X」
    （trailing_number 群組），要分開處理。
    """
    for regex in _CHAPTER_NUMBER_REGEXES:
        match = regex.match(text)
        if not match:
            continue
        try:
            start, end = match.start("number"), match.end("number")
        except (IndexError, re.error):
            continue
        number_text = match.group("number")
        if not number_text:
            continue
        return start, end, number_text
    match = LV1_A_REGEX.match(text)
    if match:
        if match.group("number"):
            start, end, number_text = match.start("number"), match.end("number"), match.group("number")
        elif match.group("trailing_number"):
            start, end = match.start("trailing_number"), match.end("trailing_number")
            number_text = match.group("trailing_number")
        else:
            return None
        if not number_text:
            return None
        return start, end, number_text
    return None


ARABIC_NUMBER_REGEX = re.compile(r"[0-9０-９]+(?:[.．]\d+)?")
ARABIC_TITLE_REGEX = re.compile(r"第\s*[0-9０-９]+\s*[部卷篇集章回節节折幕]")


def uses_arabic_numerals(text):
    """判斷一段標題文字採用的是阿拉伯數字還是中文數字。"""
    if not text:
        return False
    if ARABIC_NUMBER_REGEX.fullmatch(text):
        return True
    return bool(ARABIC_TITLE_REGEX.search(text))


def render_chapter_number_like(new_number, sample_text):
    """依照樣本採用的數字系統輸出新編號文字。"""
    number = int(new_number)
    return str(number) if uses_arabic_numerals(sample_text) else arabic_to_chinese(number)


def original_number_text(text, unit):
    """取出標題中該單位對應的原始編號文字，供保留原本的數字系統。"""
    match = re.search(r"(?:第|番外)\s*(" + CN_NUM_FLOAT_PATTERN + r")\s*" + re.escape(unit), text)
    if match:
        return match.group(1)
    weak = parse_weak_numbered_title(text)
    return weak["number_text"] if weak else None


def compact_toc_label(text):
    """只精簡畫面文字，不改動原標題、輸出內容或跳轉位置。"""
    value = re.sub(r"\s+", " ", text).strip()
    mixed = parse_mixed_volume_chapter_header(value)
    if mixed:
        return f"第{mixed['chapter_number_text']}{mixed['chapter_unit']} {mixed['chapter_body']}".strip()
    # 移除章號之前重複的作品名／卷名，但保留章號本身。
    chapter = re.search(r"(第\s*" + CN_NUM_FLOAT_PATTERN + r"\s*[章回節节折幕].*)$", value)
    if chapter and chapter.start() > 0:
        return chapter.group(1).strip()
    return value


def compact_number_ranges(numbers):
    if not numbers:
        return ""
    ranges = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(str(start) if start == previous else f"{start}–{previous}")
        start = previous = number
    ranges.append(str(start) if start == previous else f"{start}–{previous}")
    return "、".join(ranges)


def suggest_number(previous, following):
    if previous and following:
        if previous["number"] + 1 < following["number"]:
            return previous["number"] + 1
        if following["number"] > 1:
            return following["number"] - 1
        return previous["number"] + 1
    if following:
        return max(1, following["number"] - 1)
    if previous:
        return previous["number"] + 1
    return 1


def chapter_unit_signature(text):
    """回傳編號所屬的「類型」簽章，例如 ("第","章")、("卷類","卷")、("卷類","集")。

    「連續編號」不能把不同類型混在一起重排——例如「第1集」跟「第1章」是
    兩套完全獨立的編號系統，「第1卷」跟「第1集」也是各自獨立，硬要接起來
    變成連續數字沒有意義。找不到就回傳 None；呼叫端會把 None 當成「跟任何
    簽章都不同」，保守地不允許混選。
    """
    match = COMBO_LV2_REGEX.match(text)
    if match:
        return ("第", match.group("unit"))
    match = COMBO_LV2_EXTRA_REGEX.match(text)
    if match:
        return ("番外", match.group("unit") or "")
    match = COMBO_LV2_NUM_REGEX.match(text)
    if match:
        return ("Chapter", "")
    match = LV1_A_REGEX.match(text)
    if match:
        unit = match.group("unit") or match.group("leading_unit")
        if unit:
            return ("卷類", unit)
    return None


# 標題清理與空白正規化共用的樣式，避免同一份規則散落在多處。
TITLE_LEAD_SEP_REGEX = re.compile(r"^[\s，,、:：．.\-—·]+")
TITLE_TAIL_SEP_REGEX = re.compile(r"[\s。、]+$")
INLINE_SPACE_REGEX = re.compile(r"[ \t　]+")


def strip_title_body(text):
    """統一既有的標題尾端清理規則，並一併去除開頭殘留的分隔符。

    章節正則的分隔字元類別沒有涵蓋「：」「.」「—」，這些符號會被吃進
    title 群組。若不在這裡清掉，「第一章：」會被判定為「已有章名」，
    導致「合併下行標題」與幽靈標題偵測都不會啟動。
    """
    text = TITLE_LEAD_SEP_REGEX.sub("", text.strip())
    return TITLE_TAIL_SEP_REGEX.sub("", text).strip()


def preserve_title_separator(title, original):
    """在章號轉換或合集去前綴之後，還原原本的章號／章名間隔。"""
    def parts(text):
        for regex in (COMBO_LV2_REGEX, COMBO_LV2_EXTRA_REGEX, COMBO_LV2_NUM_REGEX,
                      LV1_A_REGEX, LV1_B_REGEX, COMBO_SPECIAL_REGEX):
            match = regex.match(text)
            if not match:
                continue
            ends = [match.end(name) for name in ("number", "trailing_number", "unit", "leading_unit", "tag")
                    if name in match.groupdict() and match.group(name) is not None]
            end = max(ends)
            gap = re.match(r"[ \t　:：·、\-—]*", text[end:]).group()
            return text[:end], gap, text[end + len(gap):]
        return None
    new, old = parts(title), parts(original)
    if new is None or old is None or not new[2]:
        return title
    # 無同行章名時沒有可保留的間隔；合併下行章名沿用產生的間隔。
    return new[0] + (old[1] if old[2] else new[1]) + new[2]


def extract_author_from_intro(lines):
    """從第一個正式卷／章之前的簡介區擷取「作者：名稱」。"""
    author_regex = re.compile(r"(?:^|[《》【】\s])(?:作者|作\s*者)\s*[：:]\s*([^\r\n]{1,80})")
    for raw_line in lines[:500]:
        line = raw_line.strip()
        if parse_lv1(line) or parse_lv2(line):
            break
        match = author_regex.search(line)
        if not match:
            continue
        author = match.group(1)
        author = re.split(r"[|｜]", author, maxsplit=1)[0]
        author = re.split(r"\s+(?:書名|作品|類型|类型|狀態|状态)\s*[：:]", author, maxsplit=1)[0]
        author = author.strip(" \t　，,。；;【】[]（）()《》")
        if author:
            return author
    return ""


def clean_merged_subtitle(text):
    """以共用弱格式規則移除「一、」「1.」「#1 標題」等編號。"""
    weak = parse_weak_numbered_title(text)
    if not weak:
        return text.strip(), False
    return weak["body"], True


MIXED_VOLUME_CHAPTER_REGEX = re.compile(
    r"^\s*(第\s*(" + CN_NUM_PATTERN + r")\s*([部卷篇集]))\s*"
    r"(.{0,30}?)\s+"
    r"(第\s*(" + CN_NUM_PATTERN + r")\s*([章回節节折幕])\s*(.*?))\s*$",
    re.IGNORECASE,
)


def parse_mixed_volume_chapter_header(text):
    """解析「第一卷 青雲篇 第01章 初入山門（修）」式混合表頭。"""
    match = MIXED_VOLUME_CHAPTER_REGEX.match(text)
    if not match:
        return None
    volume_raw, volume_number_text, volume_unit = match.group(1), match.group(2), match.group(3)
    volume_body = match.group(4).strip(" \t　:：-—")
    chapter_raw, chapter_number_text, chapter_unit = match.group(5), match.group(6), match.group(7)
    chapter_body = match.group(8).strip()
    # 混合表頭常附下載站版本字樣；若下一行有正式表頭，後續仍會優先採用下一行。
    chapter_body = re.sub(r"\s*[（(]\s*(?:修|修改|校對|校对|補|补)\s*[）)]\s*$", "", chapter_body).strip()
    return {
        "volume_raw": volume_raw,
        "volume_number_text": volume_number_text,
        "volume_number": int(chinese_to_arabic(volume_number_text)),
        "volume_unit": volume_unit,
        "volume_body": volume_body,
        "chapter_raw": chapter_raw,
        "chapter_number_text": chapter_number_text,
        "chapter_number": int(chinese_to_arabic(chapter_number_text)),
        "chapter_unit": chapter_unit,
        "chapter_body": chapter_body,
    }
