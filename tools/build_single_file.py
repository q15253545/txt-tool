"""把 core／ui_qt 兩個套件彙整成一個可以直接執行的 .py 檔（給外部檢查程式碼用）。

執行：python tools/build_single_file.py
產出：dist/TXT排版工具_單檔版.py

做法：每個模組的原始碼原封不動放進一個字典，再裝一個 import hook，讓
`import core.xxx`、`from .widgets import ...` 從這個字典載入。這樣模組之間
的命名空間、相對 import、`global` 宣告都跟多檔案版本完全一樣，不會因為
把所有東西攤平在同一個命名空間而撞名或改變行為。
"""

import ast
import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "dist" / "TXT排版工具_單檔版.py"
PACKAGES = ("core", "ui_qt")


def module_name(path: Path) -> str:
    parts = path.relative_to(ROOT).with_suffix("").parts
    return ".".join(parts[:-1]) if parts[-1] == "__init__" else ".".join(parts)


def summary(source: str) -> str:
    docstring = ast.get_docstring(ast.parse(source)) or ""
    return docstring.strip().splitlines()[0] if docstring.strip() else ""


def main():
    modules = []
    for package in PACKAGES:
        files = sorted((ROOT / package).glob("*.py"), key=lambda p: (p.name != "__init__.py", p.name))
        for path in files:
            source = path.read_text(encoding="utf-8")
            if "'''" in source:
                raise SystemExit(f"{path} 含有 ''' ，無法原封不動嵌入")
            modules.append((module_name(path), path.relative_to(ROOT).as_posix(), source))

    total_lines = sum(source.count("\n") for _name, _path, source in modules)
    toc = "\n".join(f"  {path:<32} {summary(source)}" for _name, path, source in modules)
    parts = [f'''#!/usr/bin/env python3
"""TXT 排版工具（PySide6 版）單檔彙整版。

由 tools/build_single_file.py 自動產生（{datetime.date.today().isoformat()}），請勿直接修改這個檔案；
修改原始的 core/、ui_qt/ 後重新產生即可。

執行：python "TXT排版工具_單檔版.py"
需求：Python 3.10+、PySide6；opencc-python-reimplemented（選用：簡繁轉換）

結構：
  core/   與介面無關的純邏輯（章節辨識、排版、編碼偵測、規則…），可單獨測試
  ui_qt/  PySide6 介面；程式進入點是 ui_qt/__main__.py 的 main()

每個模組的原始碼原封不動放在下方 _SOURCES 字典裡（以「# ==== 路徑 ====」分隔），
由檔尾的 import hook 載入，模組之間的 import 關係與多檔案版本完全相同。
錯誤訊息的 traceback 會顯示原本的檔名與行號（例如 <bundle>/core/encoding.py）。

模組一覽（共 {len(modules)} 個、約 {total_lines} 行）：
{toc}
"""

_PACKAGES = {{{", ".join(repr(p) for p in PACKAGES)}}}
_SOURCES = {{}}
''']
    for name, path, source in modules:
        parts.append(f"\n# {'=' * 70}\n# ==== {path} ====\n# {'=' * 70}\n"
                     f"_SOURCES[{name!r}] = r'''{source}'''\n")
    parts.append('''

# ======================================================================
# 載入器：讓 import core.xxx／ui_qt.xxx 從上面的 _SOURCES 讀取
# ======================================================================
import importlib.abc  # noqa: E402
import importlib.util  # noqa: E402
import linecache  # noqa: E402
import sys  # noqa: E402


class _BundleImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname not in _SOURCES:
            return None
        return importlib.util.spec_from_loader(fullname, self, is_package=fullname in _PACKAGES)

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        name = module.__name__
        filename = "<bundle>/" + name.replace(".", "/") + ("/__init__.py" if name in _PACKAGES else ".py")
        source = _SOURCES[name]
        # 讓 traceback 也印得出原始碼那一行
        linecache.cache[filename] = (len(source), None, source.splitlines(True), filename)
        module.__file__ = filename
        exec(compile(source, filename, "exec"), module.__dict__)


sys.meta_path.insert(0, _BundleImporter())

if __name__ == "__main__":
    from ui_qt.__main__ import main
    main()
''')
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text("".join(parts), encoding="utf-8")
    print(f"已產生 {OUTPUT}（{len(modules)} 個模組，{OUTPUT.stat().st_size / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
