"""文件層級的章節結構分析：多作品合集、弱格式章節候選、缺章檢查。"""

import re
from collections import Counter

from .title_markers import strip_persistent_title_marker
from .user_rules import PRESET_RULES, match_user_chapter_rule, preset_rule
from .chapter_parse import (
    CN_NUM_PATTERN,
    is_noise_prefix,
    parse_lv1,
    parse_lv2,
    parse_special,
    parse_weak_numbered_title,
    is_weak_numbered_title,
    parse_mixed_volume_chapter_header,
)

COLLECTION_COMBINED_REGEX = re.compile(
    r"^\s*(.{1,30}?)\s+(第\s*" + CN_NUM_PATTERN + r"\s*[部卷篇集])\s+(.+?)\s*$",
    re.IGNORECASE,
)


def plausible_work_name(text):
    """書名推測採保守條件；正文句子不可只因位於卷前而升級。

    「正文 第一卷 …」的「正文」是網站加的雜訊前綴，不是作品名稱。"""
    return bool(text and len(text) <= 30 and not re.search(r"[，,。！？!?；;：:”“「」]", text)
                and not is_noise_prefix(text))


def analyze_collection_structure(lines, mode="自動判斷"):
    """第二階段分析多作品合集，不改動既有章節正則。"""
    if mode == "單本小說":
        return {"active": False, "combined": {}, "work_lines": {}, "works": []}

    combined = {}
    work_counts = Counter()
    for index, raw_line in enumerate(lines):
        text, marker = strip_persistent_title_marker(raw_line.strip())
        if marker == "exclude" or not text:
            continue
        combined_match = COLLECTION_COMBINED_REGEX.match(text)
        if combined_match:
            work, volume, remainder = (
                combined_match.group(1).strip(), combined_match.group(2).strip(),
                combined_match.group(3).strip(),
            )
            if not plausible_work_name(work):
                continue
            lv2 = parse_lv2(remainder)
            special = parse_special(remainder)
            if lv2 and not is_weak_numbered_title(remainder):
                kind, data = "chapter", lv2
            elif special:
                kind, data = "special", special
            else:
                # 合集中的人物介紹、終章、篇外篇、書名號標題等仍屬第三層。
                kind, data = "generic", None
            combined[index] = {
                "work": work, "volume": volume, "kind": kind,
                "data": data, "remainder": remainder,
            }
            work_counts[work] += 1

    combined_work_names = set(work_counts)

    # 支援已排版為「作品名稱／第一集／第一章」的分行結構。
    # 作品名必須有 [::] 明確標記、已出現於完整合集表頭，
    # 或位於第一卷／集前。不再把第二卷前的任意正文當作書名。
    work_lines = {}
    for index, raw_line in enumerate(lines):
        text, marker = strip_persistent_title_marker(raw_line.strip())
        if marker == "exclude" or not plausible_work_name(text):
            continue
        if marker != "include" and (parse_lv1(text) or parse_lv2(text) or parse_special(text)):
            continue
        peek = index + 1
        while peek < len(lines) and not lines[peek].strip():
            peek += 1
        if peek >= len(lines):
            continue
        next_text, next_marker = strip_persistent_title_marker(lines[peek].strip())
        # 各章重複的「第一卷…第04章」不是新作品起點。
        if parse_mixed_volume_chapter_header(next_text):
            continue
        if COLLECTION_COMBINED_REGEX.match(next_text):
            continue
        volume_data = parse_lv1(next_text)
        if next_marker != "exclude" and volume_data and volume_data[2] > 0:
            is_explicit = marker in {"include", "auto_work"}
            is_known = text in combined_work_names
            bracketed = ((text.startswith("《") and text.endswith("》"))
                         or (text.startswith("【") and text.endswith("】")))
            if is_explicit or is_known or bracketed or mode == "多作品合集":
                work_lines[index] = text

    repeated_works = {work for work, count in work_counts.items() if count >= 3}
    explicit_works = {
        work_lines[index] for index in work_lines
        if strip_persistent_title_marker(lines[index].strip())[1] in {"include", "auto_work"}
    }
    first_volume_works = set(work_lines.values())
    if mode == "多作品合集":
        active = bool(combined or work_lines)
    else:
        active = len(repeated_works) >= 2 or len(explicit_works) >= 2 or len(first_volume_works) >= 2
        active = active or any(strip_persistent_title_marker(lines[i].strip())[1] == "auto_work"
                               for i in work_lines)
    all_names = [item["work"] for item in combined.values()] + list(work_lines.values())
    works = list(dict.fromkeys(all_names)) if active else []
    return {"active": active, "combined": combined, "work_lines": work_lines, "works": works}


def _collect_weak_candidates(lines):
    """找出弱格式（純數字、井號、括號…）的章節候選，還沒算信心度。"""
    candidates = []
    for index, raw_line in enumerate(lines):
        text, marker = strip_persistent_title_marker(raw_line.strip())
        if marker == "exclude" or not text:
            continue
        weak = parse_weak_numbered_title(text)
        if not weak:
            continue

        # 正式獨立章號後的弱格式行是次行章名，不是另一個候選章節。
        previous = index - 1
        while previous >= 0 and not lines[previous].strip():
            previous -= 1
        if previous >= 0:
            previous_text, previous_marker = strip_persistent_title_marker(lines[previous].strip())
            previous_lv2 = parse_lv2(previous_text)
            if (previous_marker != "exclude" and previous_lv2
                    and not is_weak_numbered_title(previous_text)
                    and not previous_lv2[5].strip()):
                continue

        candidates.append({"index": index, **weak, "text": text})
    return candidates


def scan_weak_chapter_candidates(lines):
    """找出弱格式章節候選並算好信心度。"""
    return score_chapter_candidates(lines, _collect_weak_candidates(lines))


def score_chapter_candidates(lines, candidates):
    """替候選章節打信心度：同格式前後編號連續、兩章之間有夠多正文、
    前後有空行，都是「真的是章節」的證據；編號連續但中間幾乎沒有正文，
    則比較像正文裡的條列。

    candidates 要依行號排序，每筆至少要有 index、number、style、style_key。"""
    style_counts = Counter(candidate["style_key"] for candidate in candidates)
    for position, candidate in enumerate(candidates):
        previous = candidates[position - 1] if position else None
        following = candidates[position + 1] if position + 1 < len(candidates) else None
        sequence_links = 0
        if previous and previous["style_key"] == candidate["style_key"] and previous["number"] + 1 == candidate["number"]:
            sequence_links += 1
        if following and following["style_key"] == candidate["style_key"] and candidate["number"] + 1 == following["number"]:
            sequence_links += 1

        neighbor_indices = []
        if previous and previous["style_key"] == candidate["style_key"]:
            neighbor_indices.append(previous["index"])
        if following and following["style_key"] == candidate["style_key"]:
            neighbor_indices.append(following["index"])
        body_chars = 0
        for neighbor_index in neighbor_indices:
            start, end = sorted((candidate["index"], neighbor_index))
            body_chars = max(body_chars, sum(len(line.strip()) for line in lines[start + 1:end]))

        blank_before = candidate["index"] == 0 or not lines[candidate["index"] - 1].strip()
        blank_after = candidate["index"] + 1 >= len(lines) or not lines[candidate["index"] + 1].strip()
        score = sequence_links * 2
        if style_counts[candidate["style_key"]] >= 3:
            score += 1
        if body_chars >= 200:
            score += 3
        elif body_chars >= 60:
            score += 2
        if blank_before or blank_after:
            score += 1
        if body_chars < 30 and sequence_links:
            score -= 3
        if candidate["style"] == "一般" and candidate["style_key"] == "一般:空格":
            score -= 1

        candidate["score"] = score
        candidate["confidence"] = "高" if score >= 5 else ("中" if score >= 3 else "低")
        candidate["selected"] = score >= 5
        start = max(0, candidate["index"] - 2)
        end = min(len(lines), candidate["index"] + 3)
        candidate["preview"] = "\n".join(
            ("▶ " if row == candidate["index"] else "  ") + lines[row]
            for row in range(start, end)
        )
    return candidates


# 常用格式沒涵蓋的弱格式，在清單上怎麼稱呼（style_key → 名稱）。
_WEAK_STYLE_LABELS = {
    "井號": "井號數字",
    "括號": "括號數字",
    "一般:.": "數字加點",
    "一般:、": "數字頓號",
    "一般:空格": "數字空格",
    "一般::": "數字加冒號",
    "一般:,": "數字加逗號",
    "一般:-": "數字加破折號",
}


def weak_style_label(style_key: str) -> str:
    if style_key in _WEAK_STYLE_LABELS:
        return _WEAK_STYLE_LABELS[style_key]
    separator = style_key.split(":", 1)[1] if ":" in style_key else style_key
    return f"數字＋「{separator}」"


def scan_chapter_candidates(lines, known_rows=frozenset(), max_length=60):
    """「本文可疑章節」：看起來像章節、但目前不在目錄裡的行。

    每一行先看符不符合某個常用格式（依 PRESET_RULES 的順序，第一個符合的
    算數）；都不符合時，再看是不是常用格式沒涵蓋的弱格式（例如「1: 標題」）。
    兩種來源一起算信心度，清單上才能用同一套標準排序、勾選。

    每筆候選多了這幾個欄位：
      format：常用格式是 "preset:<id>"，其他格式是 "weak:<style_key>"
      label：清單上顯示的格式名稱
      level：逐行加入目錄時該當成卷（1）還是章（2）
    """
    presets = [(preset["preset"], preset["name"], [preset_rule(preset["preset"])],
                preset.get("level", 2)) for preset in PRESET_RULES]
    weak_by_row = {candidate["index"]: candidate for candidate in _collect_weak_candidates(lines)}
    candidates = []
    for index, raw_line in enumerate(lines):
        if index in known_rows:
            continue
        text, marker = strip_persistent_title_marker(raw_line.strip())
        if not text or marker == "exclude" or len(text) > max_length:
            continue
        for preset_id, name, rule, level in presets:
            match = match_user_chapter_rule(text, rule)
            if match:
                candidates.append({
                    "index": index, "text": text, "number": match["number"], "body": match["title"],
                    "style": "preset", "style_key": f"preset:{preset_id}",
                    "format": f"preset:{preset_id}", "label": name, "level": level,
                })
                break
        else:
            weak = weak_by_row.get(index)
            if weak is not None:
                candidates.append({**weak, "format": f"weak:{weak['style_key']}",
                                   "label": weak_style_label(weak["style_key"]), "level": 2})
    return score_chapter_candidates(lines, candidates)


def group_formal_chapters(records, parent_of, label_of):
    """把章節記錄依父節點分成幾組，供缺章檢查使用。

    parent_of／label_of 由呼叫端提供，所以同一套邏輯可以用在 Qt 的目錄樹
    上，也可以直接用在 core 的 SimpleTree 上（例如排版前只想算一次結構、
    不想重畫整棵目錄）。番外、小數章與沒有編號的標題不列入。
    """
    groups: dict = {}
    for node, record in records.items():
        number = record["number"]
        if (record["kind"] != "chapter" or record["prefix"] == "番外"
                or number <= 0 or not float(number).is_integer()):
            continue
        ancestors, ancestor, work = [], parent_of(node), None
        while ancestor is not None:
            ancestors.insert(0, label_of(ancestor))
            if records.get(ancestor, {}).get("kind") == "work":
                work = ancestor
            ancestor = parent_of(ancestor)
        entry = groups.setdefault(parent_of(node), {
            "label": " / ".join(ancestors) or "全書", "work": work, "numbers": [], "nodes": []})
        entry["numbers"].append(int(number))
        entry["nodes"].append(node)
    return list(groups.values())


def chapter_gap_report(numbers, label, mode="僅檢查中間缺口", previous_last=None):
    ordered = sorted(set(numbers))
    start = ordered[0]
    if mode == "每卷從第1章起算":
        start = 1
    elif mode == "同作品跨卷接續" and previous_last is not None and ordered[0] > previous_last:
        start = previous_last + 1
    ranges, cursor = [], start
    for number in ordered:
        if number > cursor:
            ranges.append((cursor, number - 1))
        cursor = number + 1
    return {"label": label, "missing_ranges": ranges,
            "duplicates": sorted(n for n, count in Counter(numbers).items() if count > 1),
            "first": ordered[0], "last": ordered[-1], "count": len(ordered),
            "start_unverified": mode == "僅檢查中間缺口" and ordered[0] > 1}
