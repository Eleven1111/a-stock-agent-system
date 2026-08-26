"""晚间复盘清单（P6）。

核心守一件事：**"今天没有大面"与"大面这一节没采到"必须可区分**。两者都表现成
"这一节不见了"的话，读的人会默认今天没人吃面——那正是幸存者偏差被系统固化的方式。
"""

from __future__ import annotations

import pytest

import review_checklist as rc


def _full_evidence():
    return {
        "market": {"limit_count": 40, "break_rate": 0.2},
        "ladder": {"1": 30, "2": 8, "3": 2},
        "benchmarks": ["A", "B", "C"],
        "big_losses": ["X", "Y", "Z"],
        "themes": {"main": "半导体"},
        "sectors": {"半导体": 5},
        "fund_direction": "早盘切线",
        "scenarios": ["乐观", "中性", "悲观"],
        "candidates": [{"code": "600000", "invalidation": "龙头断板"}],
        "position_cap": "40%",
        "discipline": {"off_system_trade": 0},
        "errors": "追高一次，明日盘前复读触发器",
    }


# --------------------------------------------------------------------------- #
# 1) missing ≠ empty（本模块存在的理由）
# --------------------------------------------------------------------------- #
def test_absent_section_is_missing_not_empty():
    """上游没给这一节 → missing。"""
    state = rc.section_state(None)
    assert state["status"] == rc.MISSING
    assert state["count"] is None


def test_evidence_present_but_empty_is_empty_not_missing():
    """上游跑了、今天确实没有内容 → empty，正文写"今日无"。"""
    state = rc.section_state([])
    assert state["status"] == rc.EMPTY
    assert state["count"] == 0
    assert state["note"] == "今日无"


def test_no_big_loss_today_does_not_break_completeness():
    """今天确实没人吃面是事实，不是复盘没做——不该让清单变成"不完整"。"""
    evidence = {**_full_evidence(), "big_losses": []}
    result = rc.build_checklist(evidence, asof="2026-06-03")
    assert result["sections"]["big_losses"]["status"] == rc.EMPTY
    assert "big_losses" in result["empty_sections"]
    assert result["complete"] is True


def test_uncollected_big_loss_section_breaks_completeness():
    """采集失败必须让清单不完整，否则会被读成"今天没人吃面"。"""
    evidence = {k: v for k, v in _full_evidence().items() if k != "big_losses"}
    result = rc.build_checklist(evidence, asof="2026-06-03")
    assert result["sections"]["big_losses"]["status"] == rc.MISSING
    assert result["missing_sections"] == ["big_losses"]
    assert result["complete"] is False


# --------------------------------------------------------------------------- #
# 2) 条目数门槛：够不够 ≠ 有没有
# --------------------------------------------------------------------------- #
def test_big_losses_below_minimum_is_flagged_but_not_missing():
    """原书要求至少复盘 3 只大面；只写了 1 只是"做得不够"，不是"没数据"。"""
    evidence = {**_full_evidence(), "big_losses": ["X"]}
    result = rc.build_checklist(evidence)
    item = result["sections"]["big_losses"]
    assert item["status"] == rc.EMPTY
    assert "少于要求的 3 条" in item["note"]
    assert result["complete"] is True, "条目不足不计入 missing"


def test_scenarios_require_three_plans():
    """明日预案至少 3 套，不押单一剧本。"""
    result = rc.build_checklist({**_full_evidence(), "scenarios": ["乐观"]})
    assert result["sections"]["scenarios"]["status"] == rc.EMPTY
    assert "少于要求的 3 条" in result["sections"]["scenarios"]["note"]


def test_exactly_three_cases_passes():
    result = rc.build_checklist(_full_evidence())
    assert result["sections"]["big_losses"]["status"] == rc.OK
    assert result["sections"]["scenarios"]["status"] == rc.OK


# --------------------------------------------------------------------------- #
# 3) 全部 12 节都被检查，没有"通常没人填"的豁免
# --------------------------------------------------------------------------- #
def test_all_twelve_sections_are_always_present_in_output():
    result = rc.build_checklist({})
    assert len(result["sections"]) == 12
    assert len(result["missing_sections"]) == 12
    assert result["complete"] is False


def test_full_evidence_yields_complete_checklist():
    result = rc.build_checklist(_full_evidence(), asof="2026-06-03")
    assert result["complete"] is True
    assert result["missing_sections"] == []


@pytest.mark.parametrize("key", [name for name, _ in rc.SECTIONS])
def test_dropping_any_single_section_breaks_completeness(key):
    """逐节点验证：任何一节没采到都会让清单不完整，没有例外条款。"""
    evidence = {k: v for k, v in _full_evidence().items() if k != key}
    result = rc.build_checklist(evidence)
    assert result["complete"] is False
    assert result["missing_sections"] == [key]


# --------------------------------------------------------------------------- #
# 4) 渲染：缺项与空项都不许静默消失
# --------------------------------------------------------------------------- #
def test_report_lists_missing_sections_explicitly():
    evidence = {k: v for k, v in _full_evidence().items() if k != "big_losses"}
    text = rc.format_checklist(rc.build_checklist(evidence, asof="2026-06-03"))
    assert "big_losses" in text
    assert "缺项不等于今日无" in text


def test_report_marks_empty_section_as_today_none():
    text = rc.format_checklist(
        rc.build_checklist({**_full_evidence(), "big_losses": []}))
    assert "今日无" in text
    assert "12 节齐备" in text


def test_report_on_no_evidence_says_review_not_started():
    text = rc.format_checklist({})
    assert "复盘未开始" in text
