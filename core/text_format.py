"""標點全形／半形轉換與對話引號正規化。"""

PUNCT_TRANS = str.maketrans({",": "，", "!": "！", "?": "？", ":": "：", ";": "；", "(": "（", ")": "）"})
HALF_PUNCT_TRANS = str.maketrans({
    "，": ",", "。": ".", "！": "!", "？": "?", "：": ":", "；": ";",
    "（": "(", "）": ")", "【": "[", "】": "]", "、": ","
})
FULLWIDTH_DIGIT_TRANS = str.maketrans("0123456789", "０１２３４５６７８９")
HALFWIDTH_DIGIT_TRANS = str.maketrans("０１２３４５６７８９", "0123456789")


def normalize_dialogue_quotes(text: str, initial_depth: int = 0, return_depth: bool = False):
    """將常見中英文引號正規化：外層「」，巢狀內層『』。

    initial_depth 是進入這一行時已經開啟的引號層數。小說的對話經常跨行——
    外層引號在某一行開啟、隔幾行才閉合——若每行都從零重建堆疊，第二行的
    內層 “你好” 會被誤判成外層而輸出「你好」。呼叫端逐行處理時，應把上一行
    的結束深度傳進來，並用 return_depth=True 取回這一行的結束深度。
    """
    result = []
    stack = list(range(initial_depth))
    opening_chars = {'"', '“', '「', '『'}
    closing_chars = {'”', '」', '』'}
    open_context = set(" \t\r\n（([【{：:，,—-")
    close_context = set(" \t\r\n）)]】}：:，,。！？!?；;—-")

    for index, char in enumerate(text):
        if char not in opening_chars and char not in closing_chars:
            result.append(char)
            continue

        previous = text[index - 1] if index > 0 else ""
        following = text[index + 1] if index + 1 < len(text) else ""

        if char in {'“', '「', '『'}:
            is_opening = True
        elif char in closing_chars:
            is_opening = False
        else:
            # 半形直引號沒有方向，利用上下文與目前巢狀深度判斷。
            if not stack:
                is_opening = True
            elif not following or following in close_context or following in opening_chars or following in closing_chars:
                is_opening = False
            elif previous in open_context:
                is_opening = True
            else:
                is_opening = False

        if is_opening:
            depth = len(stack)
            result.append("「" if depth % 2 == 0 else "『")
            stack.append(depth)
        else:
            depth = stack.pop() if stack else 0
            result.append("」" if depth % 2 == 0 else "』")

    rendered = "".join(result)
    return (rendered, len(stack)) if return_depth else rendered
