"""統一的訊息框小工具。

直接呼叫 QMessageBox.information()/critical()/question() 在沒有載入 Qt
官方翻譯檔的情況下，按鈕文字會是英文的 OK/Yes/No，跟其餘全繁體中文的
介面不一致。這裡統一包一層，按鈕文字固定用中文。
"""

from PySide6.QtWidgets import QMessageBox


def info(parent, title: str, text: str):
    box = QMessageBox(QMessageBox.Icon.Information, title, text, parent=parent)
    box.addButton("確定", QMessageBox.ButtonRole.AcceptRole)
    box.exec()


def error(parent, title: str, text: str):
    box = QMessageBox(QMessageBox.Icon.Critical, title, text, parent=parent)
    box.addButton("確定", QMessageBox.ButtonRole.AcceptRole)
    box.exec()


def confirm(parent, title: str, text: str) -> bool:
    box = QMessageBox(QMessageBox.Icon.Question, title, text, parent=parent)
    box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
    yes_button = box.addButton("確定", QMessageBox.ButtonRole.AcceptRole)
    box.setDefaultButton(yes_button)
    box.exec()
    return box.clickedButton() is yes_button
