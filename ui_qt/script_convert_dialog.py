"""全文簡繁轉換的設定視窗：選方向、選範圍，附一行範例。

轉換會改動整份正文，所以這裡把「轉出來長什麼樣子」直接顯示出來——
尤其是「台灣用語」那個選項會連詞彙一起換（軟件→軟體、界面→介面），
不是每個人都想要，光看選項名稱看不出差別。
"""

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QLabel, QVBoxLayout,
)

from core.script_convert import (
    BODY_SCRIPT_CHOICES, BODY_SCRIPT_SAMPLE_SOURCE, BODY_SCRIPT_SAMPLES,
)
from . import i18n
from .widgets import keep_on_screen


class ScriptConvertDialog(QDialog):
    def __init__(self, parent=None, selected_count: int = 0, prefer_selected: bool = False,
                 mode: str | None = None, convert_metadata: bool = True):
        """prefer_selected：從目錄右鍵「繁簡轉換選取的章節」開的，就算只選一章也預設只轉那幾章。"""
        super().__init__(parent)
        self.setWindowTitle("繁簡轉換")
        self.setMinimumWidth(460)
        keep_on_screen(self)
        self._selected_count = selected_count

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(10)

        title = QLabel("轉換方向")
        title.setObjectName("appTitle")
        root.addWidget(title)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(BODY_SCRIPT_CHOICES)
        if mode in BODY_SCRIPT_CHOICES:
            self.mode_combo.setCurrentText(mode)
        self.mode_combo.currentIndexChanged.connect(lambda _index: self._update_sample())
        root.addWidget(self.mode_combo)

        self.sample_label = QLabel("")
        self.sample_label.setObjectName("fileLabel")
        self.sample_label.setWordWrap(True)
        i18n.skip(self.sample_label)   # 範例本身是被轉換的內容，不跟著介面切換
        root.addWidget(self.sample_label)

        # 只選了一章時不預設打勾：點目錄是用來跳到那一章看內容的，幾乎隨時都
        # 有一個被選著；預設只掃那一章的話，掃不到東西會讓人以為整本都沒問題。
        # 刻意多選兩章以上，才當成「只想處理這幾章」。
        self.scope_check = QCheckBox(
            f"只轉換選取的 {selected_count} 個章節" if selected_count else "只轉換選取的章節")
        self.scope_check.setChecked(bool(selected_count) and (prefer_selected or selected_count >= 2))
        self.scope_check.setEnabled(bool(selected_count))
        if not selected_count:
            self.scope_check.setToolTip("先在目錄選取章節，再開啟這個視窗，就可以只轉換那幾章")
        root.addWidget(self.scope_check)

        self.title_check = QCheckBox("一併轉換書名與作者欄位")
        self.title_check.setChecked(convert_metadata)
        root.addWidget(self.title_check)

        buttons = QDialogButtonBox()
        cancel_button = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        convert_button = buttons.addButton("開始轉換", QDialogButtonBox.ButtonRole.AcceptRole)
        convert_button.setObjectName("primary")
        cancel_button.clicked.connect(self.reject)
        convert_button.clicked.connect(self.accept)
        root.addWidget(buttons)

        self._update_sample()

    def _update_sample(self):
        mode = self.mode_combo.currentText()
        self.sample_label.setText(
            f"{BODY_SCRIPT_SAMPLE_SOURCE}\n　↓\n{BODY_SCRIPT_SAMPLES.get(mode, '')}")

    def mode(self) -> str:
        return self.mode_combo.currentText()

    def selected_only(self) -> bool:
        return self.scope_check.isChecked()

    def convert_metadata(self) -> bool:
        return self.title_check.isChecked()
