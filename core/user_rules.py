"""使用者自訂章節規則的比對，以及可在「自訂章節規則」裡勾選的常用格式。"""

import re
from functools import lru_cache

from .cn_numerals import chinese_to_arabic

# 純數字這類弱格式（#1、1.、1、、(1)…）不再自動辨識：一般正文的條列、
# 對話裡的數字很容易長得一模一樣，自動學習誤判時使用者很難察覺。改成
# 「自訂章節規則」裡的常用格式，一種格式一條規則，需要哪種就勾哪種（可複選）。
# 章名不可以用逗號、句號這類句中／句末標點結尾，也不可以太長——正文的
# 條列項目通常是完整句子，這兩個條件能擋掉大部分。
_NUMBER = r"(?P<number>[0-9０-９]{1,4})"
_CN_NUMBER = r"(?P<number>[一二三四五六七八九十百千零〇兩两]{1,6})"
_TITLE = r"(?P<title>\S(?:.{0,38}\S)?)(?<![，。：；、,;:])"
_LEAD_SEP = r"(?:[\.．、:：\-—]\s*)?"

PRESET_RULES = [
    {"preset": "hash_number", "name": "井號數字", "example": "#1 標題、## 12. 標題",
     "pattern": rf"^[#＃]{{1,6}}\s*{_NUMBER}\s*{_LEAD_SEP}{_TITLE}$"},
    {"preset": "hash_chapter", "name": "井號＋第N章", "example": "## 第1章 標題",
     "pattern": r"^[#＃]{1,6}\s*第\s*(?P<number>[0-9０-９一二三四五六七八九十百千零〇兩两]+)\s*[章回節节]"
                rf"[\s：:、.．\-—]*{_TITLE}$"},
    {"preset": "dot_number", "name": "數字加點", "example": "1. 標題、12．標題",
     "pattern": rf"^{_NUMBER}\s*[\.．]\s*(?![0-9０-９]){_TITLE}$"},   # 3.14 是小數，不是章號
    {"preset": "comma_number", "name": "數字頓號", "example": "1、標題",
     "pattern": rf"^{_NUMBER}\s*、\s*{_TITLE}$"},
    {"preset": "space_number", "name": "數字空格", "example": "1 標題",
     "pattern": rf"^{_NUMBER}[ \t　]+{_TITLE}$"},
    {"preset": "bracket_number", "name": "括號數字", "example": "(1) 標題、【1】標題",
     "pattern": rf"^[\(（\[【]\s*{_NUMBER}\s*[\)）\]】]\s*{_LEAD_SEP}{_TITLE}$"},
    {"preset": "cn_comma_number", "name": "中文數字頓號", "example": "一、標題",
     "pattern": rf"^{_CN_NUMBER}\s*、\s*{_TITLE}$"},
    {"preset": "bare_number", "name": "純數字獨立一行", "example": "1、001",
     "pattern": rf"^{_NUMBER}(?P<title>)$"},
    # 以下兩種原本是預設自動辨識，審查後改成可選（預設只收「第N章」這類正規格式）。
    # 編號後面一定要有分隔，才不會把「卷三十萬大軍」「集三千寵愛於一身」當成卷。
    {"preset": "leading_unit_volume", "name": "不帶「第」的卷號", "example": "卷一 風起、集三：歸來",
     "level": 1,
     "pattern": r"^[【\[\(（]?\s*[卷部篇集]\s*"
                r"(?P<number>[0-9０-９一二兩两三四五六七八九十百千零〇]{1,8})\s*[】\]\)）]?"
                rf"(?:[\s:：、．.\-—·]+{_TITLE})?$"},
    # 用名稱當卷名、沒有編號的卷（青雲篇、上卷）。整行就只能是「名稱＋篇／卷」，
    # 名稱最多 8 個字，不收「第」開頭（那是「第三篇」，預設規則就認得），也不收
    # 「這一篇」「每一卷」這種指示詞、數量詞開頭的說法；
    # 不收「部」：「全部」「那部」這類詞太常單獨出現。
    {"preset": "named_volume", "name": "名稱＋篇／卷", "example": "青雲篇、上卷",
     "level": 1,
     "pattern": r"^[【\[\(（]?\s*(?P<title>(?!第|[這这那哪每某整一兩两幾几])[\u4e00-\u9fff]{1,8}[篇卷])\s*[】\]\)）]?$"},
    {"preset": "english_chapter", "name": "英文 Chapter N", "example": "Chapter 1、Ch.12",
     "pattern": r"^(?:Chapter|Chap|Ch)\.?\s*(?P<number>[0-9]{1,4})"
                r"(?:[\s:：.\-—]+(?P<title>\S.{0,60}?))?\s*$"},
]


# 從範例推導規則用的字元分類。
_ARABIC = "0-9０-９"
_CHINESE_NUMBER = "一二兩两三四五六七八九十百千零〇"
_SEPARATORS = "：:、，,.．。\\-—─～~|｜/／"
_VOLUME_UNITS_IN_SAMPLE = "卷部篇集"
_CLOSING = "】\\]）\\)》>」』〕"
# 編號後面依序是：單位（章／話／節…）、收尾括號、分隔符、章名，後三者都可有可無。
_SAMPLE_REST = re.compile(
    rf"^(?P<unit>[^\s{_SEPARATORS}{_CLOSING}]{{0,4}})(?P<close>[{_CLOSING}]?)"
    rf"(?:[\s{_SEPARATORS}]*(?P<title>\S.*))?$")


def _literal_pattern(text: str) -> str:
    """把範例裡的固定文字轉成正則：空白放寬成「可有可無」，其餘逐字跳脫。"""
    parts, index = [], 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            while index < len(text) and text[index].isspace():
                index += 1
            parts.append(r"\s*")
            continue
        parts.append(re.escape(char))
        index += 1
    return "".join(parts)


def rule_from_sample(sample: str):
    r"""從一行章節標題推導出一條自訂規則（不懂正則也能建規則）。

    例如貼上「正文 001章：初入江湖」，得到
    `^\s*正文\s*(?P<number>[0-9０-９]{1,6})章[\s：:…]+(?P<title>\S.*)?\s*$`，
    章號與章名都有命名群組，缺章檢查、連續編號才有依據。

    找不到編號時回傳 None：沒有編號的規則沒辦法對應章號，價值有限，
    這種情況請使用者自己寫或用「將選取格式存為規則」。
    """
    clean = (sample or "").strip()
    if not clean or len(clean) > 120:
        return None
    match = re.search(rf"[{_ARABIC}]+", clean)
    number_class = f"[{_ARABIC}]"
    if match is None:
        match = re.search(rf"[{_CHINESE_NUMBER}]+", clean)
        number_class = f"[{_CHINESE_NUMBER}]"
    if match is None:
        return None

    prefix, rest = clean[:match.start()], clean[match.end():]
    rest_match = _SAMPLE_REST.match(rest)
    if rest_match:
        unit = rest_match.group("unit") + rest_match.group("close")
        title = rest_match.group("title") or ""
    else:
        unit, title = rest.strip(), ""

    # 章名一律做成「可有可無」：同一種格式常常有幾章只有章號、沒有標題。
    pattern = (r"^\s*" + _literal_pattern(prefix)
               + f"(?P<number>{number_class}{{1,8}})" + _literal_pattern(unit)
               + rf"(?:[\s{_SEPARATORS}]*(?P<title>\S.*?))?\s*$")

    name = f"{prefix}N{unit}".strip()
    if title:
        name += " 標題"
    return {
        "name": name[:30] or "自訂規則",
        "pattern": pattern,
        # 卷級：單位是卷／部／篇／集，或編號前面就是這些字（「卷二 風起」）。
        "level": 1 if (unit[:1] or prefix[-1:]) in _VOLUME_UNITS_IN_SAMPLE else 2,
        "enabled": True,
    }


def preset_rule(preset_id):
    """回傳一條可存進規則清單的常用格式規則（新的 dict，已啟用）。"""
    for preset in PRESET_RULES:
        if preset["preset"] == preset_id:
            return {"name": preset["name"], "pattern": preset["pattern"],
                    "level": preset.get("level", 2), "enabled": True, "preset": preset_id}
    raise KeyError(preset_id)


# 巢狀量詞（(a+)+、(.*)* …）在比對失敗時會呈指數成長，是正則卡死的典型寫法。
# 尋找面板與自訂章節規則共用這個判斷。
RISKY_REGEX = re.compile(r"\([^)]*[+*][^)]*\)\s*[+*]")


def is_risky_pattern(pattern: str) -> bool:
    return bool(pattern) and RISKY_REGEX.search(pattern) is not None


@lru_cache(maxsize=512)
def _compile_rule_pattern(pattern):
    """快取編譯後的自訂規則正則。

    刻意不把編譯結果存進規則字典本身——那個字典會被 `_save_json` 整包
    序列化回 JSON，塞進不可序列化的 re.Pattern 物件會讓存檔直接炸掉。
    用獨立的 lru_cache 換取同樣的效果：規則通常只有幾條，512 個位置
    綽綽有餘，重複呼叫可命中；即使遇到壞掉的正則，交由呼叫端的
    try/except 處理，這裡不特別攔截。
    """
    return re.compile(pattern, re.IGNORECASE)


def match_user_chapter_rule(text, rules):
    """套用使用者規則；規則仍受獨立行、長度與有效擷取內容限制。"""
    if not text or len(text) > 180:
        return None
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        try:
            match = _compile_rule_pattern(rule["pattern"]).fullmatch(text)
        except (KeyError, re.error):
            continue
        if not match:
            continue
        groups = match.groupdict()
        number_text = (groups.get("number") or "").strip()
        number = chinese_to_arabic(number_text) if number_text else 0
        # 規則裡有 title 群組就用它（可以是空的，例如只有章號的「純數字獨立
        # 一行」）；沒有 title 群組才拿整行當標題。
        title = (groups["title"] or "").strip() if "title" in groups else text.strip()
        if number_text and (number <= 0 or not float(number).is_integer()):
            continue
        if not title and not number_text:
            continue
        return {"rule": rule["name"], "level": int(rule["level"]),
                "number": int(number) if number else 0, "title": title}
    return None
