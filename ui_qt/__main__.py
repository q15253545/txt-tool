"""執行方式：python -m ui_qt"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import QApplication

from ui_qt import app_log, i18n
from ui_qt.main_window import MainWindow


def main():
    # 最先啟動記錄：之後任何一步出錯、卡死都有記錄可查（見 ui_qt/app_log.py）。
    app_log.setup()
    # 縮放比例照 Windows 顯示設定的實際值（125%、150%…），不取整數。
    # 以前用 Round 四捨五入：150% 會被當成 200%，2K 螢幕上字大一號、1080p／150%
    # 的螢幕實際可用空間只剩 960×540，版面擠到無法正常顯示。照實際比例之後，
    # 在每台螢幕上看起來都跟系統其他程式一樣大。
    # 小數倍率下字的清晰度靠下面的完整字型微調（hinting）撐住；圖示是照實際的
    # 裝置像素比重畫的（見 icons._render_pixmap），不會糊。
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    # Windows 原生佈景主題（windowsvista／windows11）不保證完全遵守樣式表，
    # 尤其是 QTreeWidget 的 ::branch 選取狀態——曾經修了背景色還是冒出原生
    # 畫的殘留（藍底、來路不明的圖示），因為原生風格在畫面上會補畫自己的
    # 選取裝飾，樣式表蓋不掉。改用 Qt 完全用 QPainter 自己畫、不呼叫系統
    # 佈景 API 的 Fusion 風格，樣式表才能真正說了算，不會有原生殘留。
    app.setStyle("Fusion")
    # 中文字糊、筆畫粗細不一的主因：Qt 在高解析度螢幕上預設「不做字型微調
    # (hinting)」，筆畫會落在半個像素上，邊緣變成一圈灰。改成完整微調後，
    # Windows 的 DirectWrite 會把筆畫對齊像素格線，跟系統原生程式一樣銳利。
    # （實機對照：4K／200% 螢幕、14px 微軟正黑體，差異一眼可見。）
    # 樣式表只指定字型家族與大小，這個設定會被每個元件繼承下去。
    app_font = QFont("Microsoft JhengHei UI")
    app_font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    app.setFont(app_font)
    # Qt 自己的中文翻譯（輸入框右鍵選單、訊息框按鈕）。
    i18n.install_qt_translation(app)
    # 卡死監看在建立視窗之前就啟動：建構主視窗、載入字型這些啟動步驟如果
    # 卡住，也要留得下呼叫堆疊（審查報告 C-22）。
    app_log.start_freeze_watchdog()
    window = MainWindow()
    window.show()
    exit_code = app.exec()
    app_log.log.info("===== 結束 =====（%s）", exit_code)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
