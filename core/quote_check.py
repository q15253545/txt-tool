"""引號與標點檢查，以及有明確答案時的自動修正。

參考網路上整理的純文字小說排版步驟（第 5～8 步）。檢查的部分只負責
標出可疑的地方；自動修正只處理「正確寫法只有一種」的情況（見 plan_fix），
看不出來的——例如一行裡引號亂成一團——留給使用者自己改。

參考資料附的正則在網頁上被吃掉了反斜線（`n` 其實是換行），不能直接照抄，判斷邏輯
是自己寫的；引號配對沿用 core/reflow.py 的 QUOTE_PAIRS。
"""

from .reflow import QUOTE_PAIRS

QUOTE_PROBLEM_LABELS = {
    "unclosed": "對話中途斷行",
    "unpaired": "引號沒有成對",
    "leading_punct": "標點在行首",
    "missing_separator": "兩段對話黏在一起",
}

# 只檢查對話用的引號。括號、書名號常常本來就會跨行（例如條列、註解），
# 一起檢查會蓋出一堆雜訊，反而看不到真正的問題。
DIALOGUE_PAIRS = {opener: closer for opener, closer in QUOTE_PAIRS.items()
                  if opener in "「『“‘"}
_DIALOGUE_CLOSERS = set(DIALOGUE_PAIRS.values())

# 不該出現在行首的標點。「、」「，」這類一定是上一行被截斷了；
# 引號、破折號、刪節號則是正常的開頭，要排除。
LEADING_PUNCT = "，。！？：；、,.!?;"

# 參考步驟的第 8 步還要求「開引號前面是句號就算問題（該用冒號）」，但那是簡體
# 出版社的規範；中文小說裡「他愣住了。「走吧。」」完全正常，照做會掃出成千
# 上萬筆雜訊。這裡只留真正可疑的一種：上一句的收尾引號後面「直接」接下一句
# 的開引號，中間沒有任何分隔——那通常是兩個人的對話被黏在同一行。
#
# 但只有「前一段是完整的一句話」才算：收尾引號前面要是句末標點。
# 說是“天太冷”“路太遠”“家裡有事” 這種是並列的引用語，
# 依標點符號用法（GB/T 15834），加了引號的並列成分之間本來就不用頓號，
# 不是問題，也不能拆行。
SENTENCE_END = "。！？!?…～~—.」』”’"


def _strip_indent(line: str):
    """回傳（縮排, 內文）。段首的全形空格是排版，不是內容。"""
    stripped = line.lstrip(" \t　")
    return line[:len(line) - len(stripped)], stripped


def check_line(line: str) -> list:
    """單獨檢查一行，回傳這一行的問題種類（可能不只一個）。"""
    problems = []
    _indent, text = _strip_indent(line)
    if not text:
        return problems

    if text[0] in LEADING_PUNCT:
        problems.append("leading_punct")

    stack = []
    stray_closer = False
    for char in text:
        if char in DIALOGUE_PAIRS:
            stack.append(DIALOGUE_PAIRS[char])
        elif char in _DIALOGUE_CLOSERS:
            if stack and stack[-1] == char:
                stack.pop()
            else:
                stray_closer = True
    if stray_closer:
        problems.append("unpaired")
    elif stack:
        # 有開沒關：整行到結束都沒有收尾，通常是對話被硬生生斷成兩行。
        problems.append("unclosed")

    if _stuck_dialogue_positions(text):
        problems.append("missing_separator")
    return problems


def _stuck_dialogue_positions(text: str) -> list:
    """「」「」黏在一起、而且前一段是完整句子的位置（下一段開引號的索引）。"""
    positions = []
    for index in range(2, len(text)):
        if (text[index] in DIALOGUE_PAIRS and text[index - 1] in _DIALOGUE_CLOSERS
                and text[index - 2] in SENTENCE_END):
            positions.append(index)
    return positions


def _balanced(text: str) -> bool:
    """對話引號是否全部成對、順序正確。"""
    stack = []
    for char in text:
        if char in DIALOGUE_PAIRS:
            stack.append(DIALOGUE_PAIRS[char])
        elif char in _DIALOGUE_CLOSERS:
            if not stack or stack[-1] != char:
                return False
            stack.pop()
    return not stack


def _missing_closers(text: str) -> str:
    """有開沒收（而且沒有多出來的收尾引號）時，回傳要補的收尾引號（由內而外）；
    否則回傳空字串。"""
    stack = []
    for char in text:
        if char in DIALOGUE_PAIRS:
            stack.append(DIALOGUE_PAIRS[char])
        elif char in _DIALOGUE_CLOSERS:
            if not stack or stack[-1] != char:
                return ""
            stack.pop()
    return "".join(reversed(stack))


def _next_line_opens_quote(lines, row: int) -> bool:
    """下一段（跳過空行）是不是以開引號開頭。"""
    for next_row in range(row + 1, min(row + 4, len(lines))):
        text = _strip_indent(lines[next_row])[1]
        if text:
            return text[0] in DIALOGUE_PAIRS
    return False


def _has_unclosed_opener(text: str) -> bool:
    """有開引號沒收尾（而且沒有多出來的收尾引號）。"""
    stack = []
    for char in text:
        if char in DIALOGUE_PAIRS:
            stack.append(DIALOGUE_PAIRS[char])
        elif char in _DIALOGUE_CLOSERS:
            if not stack or stack[-1] != char:
                return False
            stack.pop()
    return bool(stack)


# 左右長得很像、常被打錯方向的引號：同一組裡依出現順序重新配對
# （第一個當開、第二個當收…）。半形的 " 也算在雙引號這組：轉檔常留下
# 「"真的？”」這種半形開、全形收的寫法。半形的 ' 不算，英文縮寫會用到。
_QUOTE_FAMILIES = (
    ("“", "”", frozenset("“”\"")),
    ("‘", "’", frozenset("‘’")),
)


def _realign_quotes(text: str):
    """把同一組引號依順序重新配成開、收、開、收…；配不起來或本來就對，回傳 None。

    例：「他回頭看了一眼。”你今天……早點回來。”」第二段的開頭用了右引號，
    重新配對後變成左引號；「"真的？”」的半形開引號換成全形。
    只在該組引號數量是雙數時處理——單數代表真的少了一個，不能用猜的。"""
    fixed = list(text)
    changed = False
    for opener, closer, family in _QUOTE_FAMILIES:
        positions = [index for index, char in enumerate(text) if char in family]
        if not positions or len(positions) % 2:
            continue
        for order, index in enumerate(positions):
            wanted = opener if order % 2 == 0 else closer
            if fixed[index] != wanted:
                fixed[index] = wanted
                changed = True
    result = "".join(fixed)
    return result if changed and _balanced(result) else None


def plan_fix(lines, row: int, kind: str):
    """這一個問題能不能自動修？能的話回傳 {"start", "end", "after"}：
    把 lines[start:end] 換成 after 這幾行；修不了回傳 None。

    能修的情況都只有一種正確寫法：
      兩段對話黏在一起  「你說什麼？」「走吧。」→ 拆成兩行（兩個人各一段）
      標點在行首        上一行被截斷，接回上一行
      對話中途斷行      下一行正好把引號收尾時，兩行接回一行
      引號方向顛倒      」你好「 → 「你好」；……一眼。”你今天 → ……一眼。“你今天
      半形全形混用      "真的？” → “真的？”
      少了收尾引號      ……笑著問：“今天，……要去哪？ → 行尾補上 ”
                        （下一段又是開引號開頭時不補：可能是跨段對話的寫法）
      少了開引號        好啊。」他回答。 → 「好啊。」他回答。
                        （上一行有沒收尾的開引號時，改成兩行接回一行）
    """
    line = lines[row]
    indent, text = _strip_indent(line)
    if not text:
        return None

    if kind == "missing_separator":
        parts, start = [], 0
        for index in _stuck_dialogue_positions(text):
            parts.append(text[start:index])
            start = index
        parts.append(text[start:])
        if len(parts) < 2:
            return None
        return {"start": row, "end": row + 1, "after": [indent + part for part in parts]}

    if kind == "leading_punct":
        if row == 0 or not lines[row - 1].strip():
            return None            # 前面是空行：分不出來是接哪一段，不猜
        return {"start": row - 1, "end": row + 1, "after": [lines[row - 1].rstrip() + text]}

    if kind == "unclosed":
        if row + 1 >= len(lines) or not lines[row + 1].strip():
            return None
        _next_indent, next_text = _strip_indent(lines[row + 1])
        if not _balanced(text + next_text):
            return None            # 下一行也收不齊：可能是作者讓對話跨段，不動
        return {"start": row, "end": row + 2, "after": [line.rstrip() + next_text]}

    if kind == "unpaired":
        realigned = _realign_quotes(text)
        if realigned is not None:
            return {"start": row, "end": row + 1, "after": [indent + realigned]}
        # 半形 " 還在、又配不起來：分不出它是開還是收，不猜。
        if '"' in text:
            return None
        closers = _missing_closers(text)
        if closers and text[-1] in SENTENCE_END and not _next_line_opens_quote(lines, row):
            # 開了沒收、而且整句已經講完（行尾是句末標點）：少打了收尾引號，
            # 補在行尾。下一段開頭又是開引號時，可能是「跨段對話每段只開不收」
            # 的寫法，不動。
            return {"start": row, "end": row + 1, "after": [indent + text + closers]}
        quotes = [(index, char) for index, char in enumerate(text)
                  if char in DIALOGUE_PAIRS or char in _DIALOGUE_CLOSERS]
        if len(quotes) == 2:
            (first, a), (second, b) = quotes
            if a in _DIALOGUE_CLOSERS and DIALOGUE_PAIRS.get(b) == a:
                fixed = text[:first] + b + text[first + 1:second] + a + text[second + 1:]
                return {"start": row, "end": row + 1, "after": [indent + fixed]}
        if len(quotes) == 1 and quotes[0][1] in _DIALOGUE_CLOSERS:
            closer = quotes[0][1]
            opener = next(o for o, c in DIALOGUE_PAIRS.items() if c == closer)
            if row > 0 and lines[row - 1].strip():
                _prev_indent, prev_text = _strip_indent(lines[row - 1])
                if _has_unclosed_opener(prev_text) and _balanced(prev_text + text):
                    return {"start": row - 1, "end": row + 1, "after": [lines[row - 1].rstrip() + text]}
            return {"start": row, "end": row + 1, "after": [indent + opener + text]}
        return None

    return None


def apply_fixes(lines, plans) -> tuple:
    """套用多個修正計畫，回傳（新的行, 實際套用了幾個）。

    兩個計畫改到同一行時只套用前面那一個（例如「對話中途斷行」與下一行的
    「少了開引號」其實是同一件事，接一次就好）。由後往前套，前面的行號才
    不會被改掉。"""
    accepted, last_end = [], -1
    for plan in sorted(plans, key=lambda item: (item["start"], item["end"])):
        if plan["start"] < last_end:
            continue
        accepted.append(plan)
        last_end = plan["end"]
    result = list(lines)
    for plan in reversed(accepted):
        result[plan["start"]:plan["end"]] = plan["after"]
    return result, len(accepted)


def _refine_unclosed(lines, row: int, kind: str) -> str:
    """有開引號沒收尾，只有下一行正好把它收齊時，才是「對話中途斷行」；
    否則就只是引號沒有成對（例如少打了收尾引號），不要說成斷行。"""
    if kind != "unclosed":
        return kind
    if row + 1 < len(lines) and lines[row + 1].strip():
        text = _strip_indent(lines[row])[1]
        next_text = _strip_indent(lines[row + 1])[1]
        if _balanced(text + next_text):
            return kind
    return "unpaired"


def scan_quote_problems(lines, line_ranges=None) -> list:
    """掃描整份（或指定行範圍）的引號與標點問題。

    line_ranges 是 0 起算的半開區間，給「只檢查選取的章節」用；行號一律
    回傳整份文件裡的絕對行號（1 起算），點選才能跳到正確的地方。
    """
    if line_ranges is None:
        rows = range(len(lines))
    else:
        rows = sorted({row for start, end in line_ranges
                       for row in range(max(0, start), min(end, len(lines)))})
    problems = []
    for row in rows:
        line = lines[row]
        for kind in check_line(line):
            kind = _refine_unclosed(lines, row, kind)
            problems.append({
                "line": row + 1,
                "kind": kind,
                "label": QUOTE_PROBLEM_LABELS[kind],
                "preview": line.strip(),
                "fix": plan_fix(lines, row, kind),
            })
    return problems
