"""簡繁轉換引擎（檔名與正文共用）。

刻意不保留任何內建簡繁對照表：小型對照表只涵蓋少數常用字，轉出來的檔名
會是「半轉半不轉」的殘缺狀態，比完全不轉更容易誤導。沒有 OpenCC 時，
改為詢問使用者是否直接以原文檔名儲存。
"""

from functools import lru_cache

# 檔名簡繁轉換的目標選項；「保留原文」不做任何轉換。
SCRIPT_KEEP = "保留原文"
SCRIPT_TRAD = "繁體"
SCRIPT_SIMP = "簡體"
SCRIPT_CHOICES = (SCRIPT_TRAD, SCRIPT_SIMP, SCRIPT_KEEP)
SCRIPT_CONFIG = {SCRIPT_TRAD: "s2t", SCRIPT_SIMP: "t2s"}


@lru_cache(maxsize=4)
def get_opencc_converter(config="s2t"):
    """僅首次匯出時初始化，成功後重複使用。config 為 s2t（簡轉繁）或 t2s（繁轉簡）。"""
    try:
        import opencc
        return opencc.OpenCC(config)
    except Exception:
        return None


# 正文轉換的方向。跟檔名那組分開：正文多了「台灣用語」這個選項（會連詞彙
# 一起換，軟件→軟體、界面→介面），而且方向要講得更明確。
BODY_SCRIPT_MODES = {
    "簡體轉繁體": "s2t",
    "簡體轉繁體（台灣用語）": "s2twp",
    "繁體轉簡體": "t2s",
}
BODY_SCRIPT_CHOICES = tuple(BODY_SCRIPT_MODES)

BODY_SCRIPT_SAMPLES = {
    "簡體轉繁體": "這個軟件的界面設計，默認採用簡體中文。",
    "簡體轉繁體（台灣用語）": "這個軟體的介面設計，預設採用簡體中文。",
    "繁體轉簡體": "这个软件的界面设计，默认采用简体中文。",
}
BODY_SCRIPT_SAMPLE_SOURCE = "这个软件的界面设计，默认采用简体中文。"


def opencc_available() -> bool:
    """OpenCC 是選用套件；沒裝的時候按鈕要停用並說明原因。"""
    return get_opencc_converter("s2t") is not None


def convert_body_text(text, mode):
    """把整份（或一段）正文做簡繁轉換。

    OpenCC 是逐字轉換，行數與行的對應完全不變，所以章節狀態的行號是 1:1，
    不需要像搬移章節那樣另外算映射。行尾的 [::] 標記是 ASCII，不會被動到。

    沒有 OpenCC 時原樣回傳——半套轉換比不轉更糟（見模組開頭的說明）。
    """
    config = BODY_SCRIPT_MODES.get(mode)
    if not text or config is None:
        return text
    converter = get_opencc_converter(config)
    return converter.convert(text) if converter is not None else text


def convert_script(text, target=SCRIPT_TRAD):
    """依 target 將檔名轉為繁體或簡體；沒有 OpenCC 時原樣回傳、不做半套轉換。"""
    if not text or target == SCRIPT_KEEP:
        return text
    config = SCRIPT_CONFIG.get(target)
    if config is None:
        return text
    converter = get_opencc_converter(config)
    return converter.convert(text) if converter is not None else text
