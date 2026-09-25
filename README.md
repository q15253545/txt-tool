# TXT 排版工具

智慧辨識章節、整理純文字小說的排版工具（PySide6 介面）。純邏輯（章節辨識、
廣告掃描、換行整理…）放在 `core/`，介面放在 `ui_qt/`。

## 下載

到 [Releases](../../releases) 下載 `txt-tool.exe`（頁面上顯示為「TXT排版工具.exe」），雙擊就能執行，不需要安裝 Python。
（沒有數位簽章，第一次開啟時 Windows 可能顯示「已保護您的電腦」，按「其他資訊」→「仍要執行」。）

設定存放在 `%LOCALAPPDATA%\TXTFormatterV3`：自訂章節規則（`chapter_rules.json`）、
排版設定與選單狀態（`ui_state.json`）、視窗大小（`window.json`）、執行記錄（`logs/`）。
換新版 exe 或搬動 exe 位置，設定都會保留；原始碼版與 exe 版共用同一份。

## 從原始碼執行

```bash
python -m pip install PySide6
python -m ui_qt
```

選用套件：`opencc-python-reimplemented`（簡體介面、匯出檔名與正文的繁簡轉換）。

也可以執行單檔版：`python tools/build_single_file.py` 會產生
`dist/TXT排版工具_單檔版.py`，單一檔案就能執行。

打包成 exe（需要 `pip install pyinstaller`）：`python tools/build_exe.py` 會產生
`dist/TXT排版工具.exe`。

## 專案結構

```
core/       純邏輯，跟介面無關
  chapter_parse.py       單行章節標題辨識（預設只收正規格式）
  user_rules.py          自訂章節規則與常用格式
  collection.py          多作品合集、未辨識章節候選、缺章檢查
  structure_builder.py   整份文件的章節結構／目錄樹建立
  ad_scan.py             廣告與作品資訊掃描
  quote_check.py         引號與標點檢查
  reflow.py              硬換行／異常空白整理
  script_convert.py      繁簡轉換（OpenCC）
  persistence.py         自訂規則／視窗狀態存檔
  ...

ui_qt/      PySide6 介面
  main_window.py         主視窗
  *_dialog.py            各功能對話框
  find_bar.py            尋找／取代面板
  theme.py               色彩 token 與樣式表
  icons.py               內嵌 SVG 圖示
  app_log.py             記錄檔與卡死監看（Ctrl+Shift+L 開啟記錄檔資料夾）

tools/build_single_file.py   打包成單一 .py 檔
tools/build_exe.py           打包成單一 exe（PyInstaller）
test_*.py                    pytest 測試
```

## 測試

```bash
python -m pip install pytest
python -m pytest -q
```
