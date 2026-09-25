"""模糊掃描廣告候選：網址、發布頁、QQ／微信群、來源署名、重複段落、作品資訊。"""

import unicodedata
import re
from collections import Counter
from functools import lru_cache

from .chapter_parse import parse_lv1, parse_lv2

AD_CATEGORY_LABELS = {
    "url": "網址",
    "publish": "發布頁／下載頁",
    "qq": "QQ／QQ群",
    "wechat": "微信／微信群／公眾號",
    "source": "小說來源／網站名稱",
    "repeat": "重複廣告段落",
    "meta": "作品資訊／分隔線",
}

COMMON_TLDS = {
    "com", "cn", "net", "org", "cc", "vip", "top", "xyz", "info",
    "me", "tv", "io", "co", "site", "online", "club", "link", "pro",
}

PUBLISH_WORDS = (
    "發布頁", "发布页", "發佈頁", "下載頁", "下载页", "下載地址", "下载地址",
    "最新地址", "最新網址", "最新网址", "备用网址", "備用網址", "访问地址",
    "訪問地址", "手機閱讀", "手机阅读", "請記住", "请记住", "防失聯", "防失联",
    "網址", "网址", "網站", "网站",
)
QQ_WORDS = ("qq群", "qq 群", "q群", "群號", "群号", "加群", "扣扣群")
WECHAT_WORDS = (
    "微信群", "微信號", "微信号", "微信", "公眾號", "公众号", "威信群",
)
SOURCE_WORDS = (
    "本書來自", "本书来自", "本文來自", "本文来自", "本文件來自", "本文件来自",
    "小說下載", "小说下载", "電子書下載", "电子书下载", "由本站整理",
    "本站發布", "本站发布", "更多精彩", "求收藏", "求推薦", "求推荐",
)
SEPARATOR_CHARS = set("-—－─_=*＊~～·•。.")

# 對照表放在模組層級，避免每次呼叫都重建。
AD_NORMALIZE_TRANS = str.maketrans({
    "。": ".", "．": ".", "｡": ".", "點": ".", "点": ".",
    "／": "/", "：": ":", "＠": "@", "﹒": ".",
    "​": "", "‌": "", "‍": "", "﻿": "",
})
AD_WHITESPACE_REGEX = re.compile(r"\s+")


def normalize_ad_text(text):
    """統一全半形、零寬字元與常見網址混淆符號，供模糊掃描使用。"""
    return unicodedata.normalize("NFKC", text).lower().translate(AD_NORMALIZE_TRANS)


@lru_cache(maxsize=100000)
def compact_ad_text(text):
    """同一行在掃描過程中會被查詢多次，正規化結果值得快取。"""
    return AD_WHITESPACE_REGEX.sub("", normalize_ad_text(text))


# 段落縮排 + 對話引號 = 典型的小說正文特徵。
NARRATIVE_INDENT_REGEX = re.compile(r"^(?:　|[ \t]{2,}|\t)")
NARRATIVE_QUOTE_REGEX = re.compile(r"[「」『』“”\"]")


def _looks_like_narrative(lines, start, end):
    """判斷這個區段是否比較像小說正文，而不是獨立的廣告行。

    廣告行通常是短的、獨立的、沒有縮排也沒有對話；正文則相反。
    只要區段內有任何一行同時具備「段落縮排」與「夠長」，
    或者有對話引號且夠長，就視為正文。
    """
    for index in range(start, min(end + 1, len(lines))):
        line = lines[index]
        stripped = line.strip()
        if len(stripped) < 40:
            continue
        if NARRATIVE_INDENT_REGEX.match(line) or NARRATIVE_QUOTE_REGEX.search(stripped):
            return True
    return False


# --------------------------------------------------------------------------
# 作品資訊行：作者、字數、發表日期與平台、裝飾分隔線
#
# 這些行在網路下載的小說檔裡很常見，但原本的廣告掃描一行都抓不到（它找的是
# 網址與推廣用語）。判斷一律要求「整行就是這個資訊」：行首就是關鍵字、
# 長度不長、句末沒有標點，才不會把正文裡提到作者或日期的句子掃進來。
# --------------------------------------------------------------------------

# 這類資訊行都很短；超過就當成正文。
META_MAX_LENGTH = 40
# 句末標點代表這是一句話，不是一行資訊。
META_SENTENCE_TAIL = re.compile(r"[。！？!?…」』”》\)）]$")

_META_AUTHOR = re.compile(r"^作\s*者\s*[:：]\s*\S.{0,20}$")
_META_WORDCOUNT = re.compile(r"^字\s*[数數]\s*[:：]?\s*[0-9]{2,9}\s*字?$")
# 「2015/07/27发表于：某某论坛」「2018-03-01首发某某网」「首發於 A、B、C」
_META_PLATFORM_WORDS = ("发表于", "發表於", "发表於", "首发", "首發", "发布于", "發佈於",
                        "發布於", "转载自", "轉載自", "原发", "原發")
# 整行就是一個日期：2022年2月20日、2015/07/27、2015-07-27
_META_DATE_PREFIX = re.compile(r"^[0-9]{4}\s*[-/年.]\s*[0-9]{1,2}\s*[-/月.]\s*[0-9]{1,2}\s*日?")
_META_DATE = re.compile(_META_DATE_PREFIX.pattern + r"$")


def _repeated_char_line(text):
    """整行都是同一個字元、而且重複十次以上（＊＊＊＊、──────）。"""
    compact = AD_WHITESPACE_REGEX.sub("", text)
    return len(compact) >= 10 and len(set(compact)) == 1


def meta_line_kind(text):
    """這一行是作品資訊嗎？回傳（種類, 信心）或 None。

    單獨的日期只給「中」信心：日記體小說每一章開頭就是日期，如果給高信心
    而使用者順手按「全選高信心」，整本書的章節開頭就被刪光了。
    """
    stripped = text.strip()
    if not stripped or len(stripped) > META_MAX_LENGTH:
        return None
    if _repeated_char_line(stripped):
        return ("separator", "中")
    if META_SENTENCE_TAIL.search(stripped):
        return None
    # 全形數字、全形冒號都先折成半形再比對。
    norm = unicodedata.normalize("NFKC", stripped)
    if _META_AUTHOR.match(norm):
        return ("author", "高")
    if _META_WORDCOUNT.match(norm):
        return ("wordcount", "高")
    if any(word in norm for word in _META_PLATFORM_WORDS):
        # 還要求這一行以日期或發表用語開頭，整行才真的是「發表資訊」；
        # 否則「他首發了一篇小說」這種正文句子也會被掃進來。
        if _META_DATE_PREFIX.match(norm) or norm.startswith(_META_PLATFORM_WORDS):
            return ("platform", "高")
        return None
    if _META_DATE.match(norm.replace(" ", "")):
        return ("date", "中")
    return None


def looks_like_separator(text):
    compact = compact_ad_text(text)
    return len(compact) >= 5 and all(char in SEPARATOR_CHARS for char in compact)


def find_domain_tokens(compact_text):
    """找出通用網域，不依賴任何特定小說網站名稱。"""
    tokens = []
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-._:/?=&%#@")
    start = None
    for index, char in enumerate(compact_text + " "):
        if char in allowed:
            if start is None:
                start = index
            continue
        if start is not None:
            token = compact_text[start:index].strip("-._:/")
            if "." in token:
                host_source = token.split("://", 1)[-1]
                host_part = host_source.split("/", 1)[0]
                host_part = host_part.rsplit("@", 1)[-1]
                pieces = [piece for piece in host_part.split(".") if piece]
                tld = pieces[-1] if pieces else ""
                is_domain = len(pieces) >= 2 and tld in COMMON_TLDS and any(c.isalpha() for c in pieces[-2])
                is_ipv4 = (len(pieces) == 4 and all(piece.isdigit() and 0 <= int(piece) <= 255
                                                   for piece in pieces))
                if is_domain or is_ipv4:
                    tokens.append(token)
            start = None
    return tokens


_AD_WORD_CACHE = {}


def _compact_contains_any(compact, words):
    normalized_words = _AD_WORD_CACHE.get(words)
    if normalized_words is None:
        normalized_words = tuple(compact_ad_text(word) for word in words)
        _AD_WORD_CACHE[words] = normalized_words
    return any(word in compact for word in normalized_words)


def _url_window_end(lines, index):
    """回傳從指定行起、最多五行內組成網址時的最後行號。"""
    first = compact_ad_text(lines[index])
    if find_domain_tokens(first):
        return index
    # 跨行只拼接網址碎片，不能把前面的普通英文／數字正文帶入。
    fragment = re.compile(r"^[a-z0-9:/._?=&%#@+\-]+$")
    publish_context = (_compact_contains_any(first, PUBLISH_WORDS)
                       or _compact_contains_any(first, SOURCE_WORDS))
    if not fragment.fullmatch(first) and not publish_context:
        return None
    parts = "" if publish_context else first
    for end in range(index + 1, min(len(lines), index + 5)):
        next_part = compact_ad_text(lines[end])
        if not next_part or not fragment.fullmatch(next_part):
            break
        # 下一行已是完整網址時，前行須有發布語意才可合併。
        if find_domain_tokens(next_part) and not publish_context:
            return None
        parts += next_part
        if find_domain_tokens(parts):
            return end
    return None


def _ad_line_features(lines, index, enabled_categories):
    """回傳某行附近的廣告類型；網址可跨最多三行並容許空白拆分。"""
    features = set()
    line = lines[index]
    compact_line = compact_ad_text(line)
    ascii_count = sum(char.isascii() and char.isalnum() for char in compact_line)
    url_context = (
        ascii_count >= 2
        or _compact_contains_any(compact_line, PUBLISH_WORDS)
        or _compact_contains_any(compact_line, SOURCE_WORDS)
    )
    if "url" in enabled_categories and url_context and _url_window_end(lines, index) is not None:
        features.add("url")
    if "publish" in enabled_categories and _compact_contains_any(compact_line, PUBLISH_WORDS):
        features.add("publish")
    if "qq" in enabled_categories:
        has_qq_word = _compact_contains_any(compact_line, QQ_WORDS) or "qq" in compact_line
        digit_count = sum(char.isdigit() for char in compact_line)
        if has_qq_word and digit_count >= 4:
            features.add("qq")
    if "wechat" in enabled_categories and _compact_contains_any(compact_line, WECHAT_WORDS):
        features.add("wechat")
    if "source" in enabled_categories and _compact_contains_any(compact_line, SOURCE_WORDS):
        features.add("source")
    return features


def scan_ad_candidates(lines, enabled_categories=None, line_ranges=None):
    """模糊掃描廣告候選，只產生預覽資料，不直接刪除任何內容。

    line_ranges 是「只看這幾段」的行範圍（0 起算的半開區間），給「只掃描選取
    的章節」用。整份還是照掃（重複段落要看整本才數得準），只是最後把落在範圍
    外的候選濾掉，行號因此一律是整份文件裡的絕對行號。"""
    enabled = set(enabled_categories or AD_CATEGORY_LABELS)
    if not lines or not enabled:
        return []
    # compact_ad_text 是純函式，跨掃描保留快取是安全的：使用者常在同一個
    # 對話框裡切換分類重掃，命中快取可省下約一半時間。maxsize 已鎖住記憶體上限。
    candidates = _scan_ad_candidates(lines, enabled)
    if line_ranges is None:
        return candidates
    return _clip_to_ranges(candidates, lines, line_ranges)


def _clip_to_ranges(candidates, lines, line_ranges):
    """只留下跟選取範圍有交集的候選，並把範圍外的行切掉。

    不切的話，選了第三章卻刪掉跨到第四章的段落，等於偷偷改了沒選的地方。"""
    ranges = sorted((start, end) for start, end in line_ranges if start < end)
    kept = []
    for candidate in candidates:
        for start, end in ranges:
            new_start = max(candidate["start"], start)
            new_end = min(candidate["end"], end - 1)
            if new_start > new_end:
                continue
            clipped = dict(candidate)
            clipped["start"], clipped["end"] = new_start, new_end
            clipped["preview"] = "\n".join(lines[new_start:new_end + 1])
            clipped["line"] = new_start + 1
            kept.append(clipped)
            break
    return kept


def _scan_ad_candidates(lines, enabled):
    repeated = Counter()
    if "repeat" in enabled:
        for line in lines:
            compact = compact_ad_text(line)
            if 12 <= len(compact) <= 180 and not parse_lv1(line.strip()) and not parse_lv2(line.strip()):
                repeated[compact] += 1

    hits = []
    total = len(lines)
    features_by_line = [_ad_line_features(lines, index, enabled) for index in range(total)]
    # 作品資訊自己帶信心度（作者／字數／平台是高，日期與分隔線是中），
    # 不走下面那套「特徵愈多分數愈高」的算法。
    meta_by_line = ([meta_line_kind(line) for line in lines] if "meta" in enabled
                    else [None] * total)
    # 併進 features_by_line，底下「把相鄰的廣告行合併成一段」才看得到它們：
    # 作者、字數、發表資訊通常連續好幾行，被切成好幾個候選很難勾。
    for row, kind in enumerate(meta_by_line):
        if kind is not None:
            features_by_line[row].add("meta")
    for index, line in enumerate(lines):
        features = set(features_by_line[index])
        compact = compact_ad_text(line)
        if "repeat" in enabled and repeated.get(compact, 0) >= 3:
            features.add("repeat")
        if not features:
            continue

        start = end = index
        if features != {"repeat"}:
            if "url" in features:
                end = max(end, _url_window_end(lines, index) or index)
            # 跨行網址或相鄰廣告關鍵字合併為一個候選段落，最多擴展五行。
            for next_index in range(index + 1, min(total, index + 5)):
                next_line = lines[next_index]
                next_features = set(features_by_line[next_index])
                if next_features or not next_line.strip() or looks_like_separator(next_line):
                    end = max(end, next_index)
                    features.update(next_features)
                    if "url" in next_features:
                        end = max(end, _url_window_end(lines, next_index) or next_index)
                else:
                    break
        if start > 0 and looks_like_separator(lines[start - 1]):
            start -= 1
        if end + 1 < total and looks_like_separator(lines[end + 1]):
            end += 1

        edge = start < 200 or end >= max(0, total - 200)
        score = len(features) * 2 + (1 if edge else 0)
        if "url" in features and ({"publish", "source"} & features):
            score += 2
        # 網址若出現在「看起來像正文」的行裡（有段落縮排、對話引號、而且夠長），
        # 通常是角色提到某個網站，不是廣告行。整段刪掉會傷到故事內容，
        # 所以調降信心度、不讓它落入預設勾選，改由使用者自行判斷。
        if _looks_like_narrative(lines, start, end):
            score = min(score, 2)
        if "meta" in features:
            # 這一段裡只要有一行是高信心的作品資訊，整段就算高信心；
            # 只有日期或分隔線的話維持中信心。
            kinds = [meta_by_line[row] for row in range(start, min(end + 1, total))
                     if meta_by_line[row] is not None]
            score = max(score, 5 if any(kind[1] == "高" for kind in kinds) else 3)
        confidence = "高" if score >= 5 else "中" if score >= 3 else "低"
        hits.append({"start": start, "end": end, "types": set(features),
                     "confidence": confidence, "score": score})

    merged = []
    for hit in sorted(hits, key=lambda item: (item["start"], item["end"])):
        if merged and hit["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], hit["end"])
            merged[-1]["types"].update(hit["types"])
            merged[-1]["score"] = max(merged[-1]["score"], hit["score"])
            merged[-1]["confidence"] = "高" if merged[-1]["score"] >= 5 else "中" if merged[-1]["score"] >= 3 else "低"
        else:
            merged.append(dict(hit))

    for candidate in merged:
        candidate["preview"] = "\n".join(lines[candidate["start"]:candidate["end"] + 1])
        candidate["line"] = candidate["start"] + 1
    return merged
