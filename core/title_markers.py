"""行尾持久標記：[::] 人工收錄、[::X] 排除、[::W]/[::T] 自動作品／附錄結構。"""

import re

from .cn_numerals import chinese_to_arabic

END_MARK_REGEX = re.compile(r"[部卷篇集章回節节折幕][\s]*完(?:本)?[\s】\]）\)]*$", re.IGNORECASE)

# 卷／章「結尾行」：第一卷終、【第二卷终】、第一章 完、（本卷完）、卷三結束……
# 先拿掉括號、空白與句末符號再整行比對，所以寫法再怎麼包都認得出來；整行
# 必須「只有」編號＋結尾字，「第十章 終章」「第三卷 完結篇」這種真正的
# 標題不會被當成結尾。
_END_MARK_NOISE = re.compile(r"[\s【】\[\]（）()「」『』〔〕《》〈〉<>·・:：\-—~～!！。．.…*＊=＝]")
_END_NUMBER = r"[0-9０-９一二兩两三四五六七八九十百千萬万〇零]+"
_END_WORDS = r"(?:完結|完结|完本|結束|结束|終了|终了|完|終|终)"
_END_MARK_FULL = re.compile(
    rf"^(?:第(?P<n1>{_END_NUMBER})(?P<u1>[部卷篇集章回節节折幕])"
    rf"|(?P<u2>[部卷篇集])(?P<n2>{_END_NUMBER})"
    rf"|[本全](?P<u3>[部卷篇集章回節节]))"
    rf"{_END_WORDS}$")
_VOLUME_UNITS = set("部卷篇集")


def parse_end_mark(text):
    """辨識卷／章結尾行；回傳 {"level": "volume"|"chapter", "number": int|None,
    "unit": 單位字} 或 None。number 為 None 代表「本卷完」這類沒寫編號的寫法。"""
    compact = _END_MARK_NOISE.sub("", text)
    if not compact or len(compact) > 16:
        return None
    match = _END_MARK_FULL.match(compact)
    if not match:
        return None
    unit = match.group("u1") or match.group("u2") or match.group("u3")
    number_text = match.group("n1") or match.group("n2")
    number = None
    if number_text:
        value = chinese_to_arabic(number_text)
        number = int(value) if value and float(value).is_integer() else None
    return {"level": "volume" if unit in _VOLUME_UNITS else "chapter", "number": number, "unit": unit}
MANUAL_TITLE_REGEX = re.compile(r"^(.*?)\s*\[::\]\s*$", re.IGNORECASE)
EXCLUDED_TITLE_REGEX = re.compile(r"^(.*?)\s*\[::X\]\s*$", re.IGNORECASE)


def strip_persistent_title_marker(text):
    """移除行尾持久標記，回傳（正文、include／exclude／空字串）。"""
    match = EXCLUDED_TITLE_REGEX.match(text)
    if match:
        return match.group(1).rstrip(), "exclude"
    match = MANUAL_TITLE_REGEX.match(text)
    if match:
        return match.group(1).rstrip(), "include"
    match = re.fullmatch(r"(.*?)\[::(W|T)\]\s*", text, re.DOTALL)
    if match:
        return match.group(1).rstrip(), "auto_work" if match.group(2) == "W" else "auto_title"
    return text, ""


# 匯出用：整份文字裡所有行尾標記。標記前面的空白一起拿掉，避免留下一行
# 結尾的多餘空格；行尾可能是換行或檔案結尾。
EXPORT_MARKER_REGEX = re.compile(
    r"[ \t\u3000]*\[::[XxWwTt]?\][ \t\u3000]*(?=\r?\n|\Z)")


def strip_export_markers(text):
    """移除全文的行尾持久標記，回傳（移除後的文字、移除了幾個）。

    匯出給別人看的成品時用。拿掉之後，人工指定的章節、標註為非章節的設定
    都會跟著消失——那些狀態就是靠這些標記存在檔案裡的。"""
    return EXPORT_MARKER_REGEX.subn("", text)


def strip_manual_title_marker(text):
    """相容既有呼叫：只有 [::] 代表人工加入目錄。"""
    clean, marker = strip_persistent_title_marker(text)
    return clean, marker == "include"
