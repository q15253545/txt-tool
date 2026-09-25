"""自訂章節規則與視窗狀態的檔案存取。"""

import os
import re
import json
from pathlib import Path

# 設定檔放在使用者的應用程式資料夾。TXT_TOOL_DATA_DIR 可以把它改到別處——
# 測試都用這個指到暫存資料夾，才不會蓋掉使用者真正的規則與介面設定。
APP_DATA_DIR = (Path(os.environ["TXT_TOOL_DATA_DIR"]) if os.environ.get("TXT_TOOL_DATA_DIR") else Path(
    os.environ.get("LOCALAPPDATA")
    or os.environ.get("XDG_CONFIG_HOME")
    or (Path.home() / ".config")
) / "TXTFormatterV3")
RULES_FILE = APP_DATA_DIR / "chapter_rules.json"
WINDOW_FILE = APP_DATA_DIR / "window.json"
UI_STATE_FILE = APP_DATA_DIR / "ui_state.json"

# 預設沒有任何自訂規則；常用格式改在「自訂章節規則」視窗裡勾選
# （core.user_rules.PRESET_RULES），一種格式一條，不再一條規則包多種寫法。
DEFAULT_USER_RULES = []


def _load_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as source:
            return json.load(source)
    except (OSError, ValueError, TypeError):
        return fallback


def _save_json(path, value):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with open(temporary, "w", encoding="utf-8") as target:
            json.dump(value, target, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
        return True
    except OSError:
        return False


def load_user_chapter_rules():
    rules = _load_json(RULES_FILE, DEFAULT_USER_RULES)
    valid = []
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, dict):
            continue
        name = str(rule.get("name", "")).strip()
        pattern = str(rule.get("pattern", "")).strip()
        try:
            level = int(rule.get("level", 2))
            re.compile(pattern)
        except (ValueError, TypeError, re.error):
            continue
        if name and pattern and level in (1, 2):
            item = {"name": name, "pattern": pattern, "level": level,
                    "enabled": bool(rule.get("enabled", True))}
            if isinstance(rule.get("preset"), str):
                item["preset"] = rule["preset"]
            valid.append(item)
    return valid


def load_window_state():
    """上次關閉時的視窗大小與位置；沒有或壞掉就回傳 None（用預設值）。"""
    state = _load_json(WINDOW_FILE, None)
    if not isinstance(state, dict):
        return None
    try:
        geometry = [int(state[key]) for key in ("x", "y", "width", "height")]
    except (KeyError, TypeError, ValueError):
        return None
    if geometry[2] < 200 or geometry[3] < 150:
        return None
    return {"x": geometry[0], "y": geometry[1], "width": geometry[2], "height": geometry[3],
            "maximized": bool(state.get("maximized"))}


def load_ui_state() -> dict:
    """上次關閉時的介面狀態（深色模式、各種勾選…）；沒有或壞掉就是空的。"""
    state = _load_json(UI_STATE_FILE, {})
    return state if isinstance(state, dict) else {}


def save_ui_state(state: dict) -> bool:
    return _save_json(UI_STATE_FILE, state)


def save_window_state(x: int, y: int, width: int, height: int, maximized: bool) -> bool:
    return _save_json(WINDOW_FILE, {"x": int(x), "y": int(y), "width": int(width),
                                     "height": int(height), "maximized": bool(maximized)})
