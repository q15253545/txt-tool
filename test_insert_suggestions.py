"""core.insert_suggestions 回歸測試。"""

from core.insert_suggestions import (
    get_structure_records, get_insert_suggestions, build_inserted_title,
)


def test_structure_records_basic():
    raw_lines = ["第一卷 開端", "第一章 序幕", "　　正文。", "第二章 出發", "　　正文二。"]
    records = get_structure_records(raw_lines, recognized_indices=[0, 1, 3])
    assert [r["level"] for r in records] == [1, 2, 2]
    assert records[1]["number"] == 1
    assert records[2]["number"] == 2


def test_insert_suggestions_between_chapters():
    raw_lines = ["第一章 開始", "　　正文。", "第三章 結束", "　　正文。"]
    suggestions, default_kind = get_insert_suggestions(raw_lines, [0, 2], insert_index=2)
    assert suggestions["章"]["number"] == 2
    assert default_kind == "章"


def test_insert_suggestions_at_very_start():
    raw_lines = ["第一章 開始", "　　正文。"]
    suggestions, default_kind = get_insert_suggestions(raw_lines, [0], insert_index=0)
    assert default_kind == "前言"


def test_build_inserted_title_numeric():
    assert build_inserted_title("章", "5", "空白標題", "第3章") == "第5章 空白標題"


def test_build_inserted_title_preserves_numeral_style():
    # reference 用中文數字時，新編號也要輸出中文數字。
    assert build_inserted_title("章", "5", "", "第三章") == "第五章"


def test_build_inserted_title_rejects_invalid_number():
    import pytest
    with pytest.raises(ValueError):
        build_inserted_title("章", "abc", "標題")
    with pytest.raises(ValueError):
        build_inserted_title("章", "0", "標題")


def test_build_inserted_title_custom():
    assert build_inserted_title("自訂標題", "", "我的標題") == "我的標題[::]"


def test_build_inserted_title_non_numeric_kind():
    assert build_inserted_title("前言", "", "楔子內容") == "前言 楔子內容"
