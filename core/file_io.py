"""TXT 檔案的讀寫：寫入用原子替換，讀取先嚴格解碼。

寫入不直接開啟目標檔：`open(path, "w")` 一成功就把原檔清空了，之後只要
磁碟滿了、隨身碟被拔掉或程式被中斷，原本的檔案就只剩半截，而且救不回來
——被覆蓋的往往正是使用者唯一那份小說。改成先寫進同一個資料夾裡的暫存
檔，完整寫完（含 fsync）才用 os.replace 換掉目標檔；中途任何失敗都只是
刪掉暫存檔，原檔一個位元組都沒動過。

讀取預設用 errors="strict"：編碼猜錯時要讓呼叫端知道，而不是默默把解不開
的位元組換成 U+FFFD——那些替換字元會被當成正文編輯、存檔，等使用者發現
的時候原始資料已經救不回來了。確定要容錯開啟時再呼叫 read_text_lossy。
"""

import os
import tempfile


def write_text_atomic(path: str, text: str, encoding: str = "utf-8"):
    """寫入暫存檔後原子替換；失敗時拋出例外，且目標檔維持原狀。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".txt-tool-", suffix=".tmp")
    try:
        # 文字模式（newline 用預設值）：Windows 上換行仍然輸出成 CRLF，
        # 跟原本直接 open(path, "w") 的結果一致。
        with os.fdopen(handle, "w", encoding=encoding) as target:
            target.write(text)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read_text(path: str, encoding: str) -> str:
    """嚴格解碼；編碼不符時拋出 UnicodeError。"""
    with open(path, "r", encoding=encoding, errors="strict") as source:
        return source.read()


def read_text_lossy(path: str, encoding: str) -> tuple[str, int]:
    """容錯解碼，回傳（內容, 解不開而被替換掉的字元數）。"""
    with open(path, "r", encoding=encoding, errors="replace") as source:
        content = source.read()
    return content, content.count("�")
