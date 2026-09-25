"""打包成單一 exe：python tools/build_exe.py

產出：dist/TXT排版工具.exe（不需要安裝 Python 就能執行）

使用者的設定不放在 exe 旁邊，也不放在 exe 解壓縮的暫存資料夾（每次執行都不同、
關掉就刪掉），而是跟原始碼版一樣放在 %LOCALAPPDATA%\\TXTFormatterV3（見
core/persistence.py）：
  chapter_rules.json  自訂章節規則
  ui_state.json       排版設定、選單與對話框的勾選狀態
  window.json         視窗大小與位置
  logs/               執行記錄
所以換新版 exe、搬動 exe 位置，設定都還在；原始碼版與 exe 版共用同一份設定。
"""

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build" / "exe"
DIST_DIR = ROOT / "dist"
APP_NAME = "TXT排版工具"

sys.path.insert(0, str(ROOT))


def render_icon(target: Path):
    """程式圖示是用程式畫的（icons.make_app_icon），另存成 exe 用的 .ico。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from ui_qt import icons

    app = QApplication.instance() or QApplication([])
    icon = icons.make_app_icon()
    pixmap = icon.pixmap(256, 256)
    if not pixmap.save(str(target), "ICO"):
        # 沒有 ICO 外掛時改用 PNG，交給 PyInstaller（需要 Pillow 才能轉）。
        target = target.with_suffix(".png")
        pixmap.save(str(target), "PNG")
    del app
    return target


def main():
    import PyInstaller.__main__

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    entry = BUILD_DIR / "launch.py"
    entry.write_text("from ui_qt.__main__ import main\n\nmain()\n", encoding="utf-8")
    icon = render_icon(BUILD_DIR / "app.ico")

    args = [
        str(entry),
        "--name", APP_NAME,
        "--onefile",
        "--windowed",                       # 不跳出黑色主控台視窗
        "--noconfirm",
        "--clean",
        "--paths", str(ROOT),
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR / "work"),
        "--specpath", str(BUILD_DIR),
        "--icon", str(icon),
        # 繁簡轉換的字典檔（opencc-python-reimplemented）不是 .py，要另外帶進去。
        "--collect-data", "opencc",
        # 用不到的大型 Qt 模組，排除掉縮小檔案。
        "--exclude-module", "PySide6.QtWebEngineCore",
        "--exclude-module", "PySide6.QtWebEngineWidgets",
        "--exclude-module", "PySide6.QtQml",
        "--exclude-module", "PySide6.QtQuick",
        "--exclude-module", "PySide6.Qt3DCore",
        "--exclude-module", "PySide6.QtMultimedia",
        "--exclude-module", "PySide6.QtCharts",
        "--exclude-module", "PySide6.QtDataVisualization",
        "--exclude-module", "PySide6.QtPdf",
        "--exclude-module", "tkinter",
    ]
    PyInstaller.__main__.run(args)
    exe = DIST_DIR / f"{APP_NAME}.exe"
    size = exe.stat().st_size / 1024 / 1024
    print(f"已產生 {exe}（{size:.1f} MB）")


if __name__ == "__main__":
    main()
