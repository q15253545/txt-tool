"""從常見 TXT 檔名擷取書名、作者與連載狀態；也負責反過來組出建議匯出檔名。"""

import os
import re

from core.cn_numerals import chinese_to_arabic

_CN_NUMERAL_REGEX = re.compile(r'[零〇一二兩两三四五六七八九十百千万萬億亿兆]+')
_FILENAME_UNSAFE_REGEX = re.compile(r'[\\/:*?"<>|]')


def extract_filename_metadata(filename):
    """從常見 TXT 檔名擷取書名、作者與連載狀態。"""
    # 只移除真正的 .txt 副檔名。呼叫端可能已經去過一次副檔名，
    # 不能再用 splitext 把 [example.org]後半部誤當成副檔名。
    base = re.sub(r"(?i)\.txt$", "", os.path.basename(filename).strip()).strip()
    author_match = re.search(r"作者\s*[：:]\s*(.+?)(?=(?:[◎（(【\[]|$))", base)
    author = author_match.group(1).strip(" _-，,。") if author_match else ""

    if re.search(r"未\s*完[結结]|連載中|连载中|連載|连载", base, re.IGNORECASE):
        status = "未完結"
    elif re.search(r"已\s*完[結结]|完[結结]|完本", base, re.IGNORECASE):
        status = "已完結"
    else:
        status = "未指定"

    title = ""
    # 《《書名》》這種重複的書名號也要剝乾淨，不能留一個「《」在書名裡。
    book_match = re.search(r"《+\s*([^\n《》]+?)\s*》", base)
    if book_match:
        title = book_match.group(1).strip()
    else:
        bracket_match = re.search(r"【+\s*([^\n【】]+?)\s*】", base)
        if bracket_match:
            title = bracket_match.group(1).strip()

    if not title:
        cleaned = re.sub(r"\[[^\]]*\]|【[^】]*】|《[^》]*》", " ", base)
        cleaned = re.split(r"作者\s*[：:]", cleaned, maxsplit=1)[0]
        cleaned = re.sub(r"\b\d+\s*[-~～—至]\s*\d+\s*(?:章|回)?", " ", cleaned)
        cleaned = re.sub(r"(?:未\s*完[結结]|已\s*完[結结]|完[結结]|完本|連載中|连载中|連載|连载)", " ", cleaned)
        title = re.sub(r"\s+", " ", cleaned).strip(" _-◎，,")
    return title, author, status


def build_smart_filename(title, author, status, last_vol, last_ch, has_fanwai):
    """依書名／作者／連載狀態／最新卷章組出建議的匯出檔名（含 .txt）。

    只負責組字串與去除檔名不合法字元；簡繁轉換是另一件事，呼叫端
    自己決定要不要再套 core.script_convert.convert_script。
    """
    def cn_to_ar(match):
        value = chinese_to_arabic(match.group(0))
        return str(int(value)) if value.is_integer() else str(value)

    vol = _CN_NUMERAL_REGEX.sub(cn_to_ar, last_vol.strip())
    ch = _CN_NUMERAL_REGEX.sub(cn_to_ar, last_ch.strip())
    # 只有分章時不補卷／集；同時存在卷與章時才合併顯示。
    vol_ch_str = (vol + ch).replace(" ", "") if vol else ch.replace(" ", "")
    fanwai_str = "+番外" if has_fanwai else ""

    if status == "已完結":
        status_tag = "（完結+番外）" if has_fanwai else "（完結）"
        name = f"《{title}》{status_tag}作者：{author}.txt"
    else:
        update_part = f"【更新至{vol_ch_str}{fanwai_str}】" if vol_ch_str else ""
        name = f"《{title}》{update_part}作者：{author}.txt"
    return _FILENAME_UNSAFE_REGEX.sub("_", name)
