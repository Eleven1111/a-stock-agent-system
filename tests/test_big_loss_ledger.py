"""大面股库（P6）。

守的性质：回撤口径不能被收盘涨跌幅顶替、"没有大面"与"判不了"必须可区分、
合并幂等、样本不足不给分层结论。
"""

from __future__ import annotations

import pytest

import big_loss_ledger as bl


def _event(code="600000", date="2026-06-03", *, high=11.0, close=9.0, **extra):
    row = {"date": date, "code": code, "next_high": high, "next_close": close,
           "board_level": 2, "sector": "半导体", "sentiment_state": "退潮"}
    row.update(extra)
    return row


# --------------------------------------------------------------------------- #
# 1) 回撤口径：日内最高 → 收盘，不是收盘涨跌幅
# --------------------------------------------------------------------------- #
def test_drawdown_uses_intraday_high_not_previous_close():
    """T+1 高开 5% 后跌停：收盘涨跌幅口径只有 -5% 上下，看不出实际伤害。"""
    prev_close = 10.0
    high = prev_close * 1.05          # 高开后冲到 10.5
    close = prev_close * 0.90         # 收在跌停 9.0
    result = bl.drawdown_from_high_pct(high, close)
    assert result["value"] == pytest.approx((9.0 / 10.5 - 1) * 100, abs=1e-4)
    assert result["value"] < -14.0, "从最高点算的回撤必须显著大于收盘跌幅 -10%"


def test_drawdown_missing_price_is_unavailable_not_zero():
    for high, close in ((None, 9.0), (11.0, None), (0.0, 9.0)):
        result = bl.drawdown_from_high_pct(high, close)
        assert result["status"] == bl.UNAVAILABLE
        assert result["value"] is None


# --------------------------------------------------------------------------- #
# 2) 入库判定与阈值边界
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("close,expected", [
    (11.0 * 0.85, True),     # 恰好 -15%
    (11.0 * 0.851, False),   # 略好于 -15%
])
def test_threshold_boundary_is_inclusive(close, expected):
    verdict = bl.classify_event(_event(high=11.0, close=close), drawdown_pct=-15.0)
    assert verdict["status"] == bl.AVAILABLE
    assert verdict["is_big_loss"] is expected


def test_missing_required_field_is_undecidable_not_a_pass():
    verdict = bl.classify_event({"date": "2026-06-03", "code": "600000"})
    assert verdict["status"] == bl.UNAVAILABLE
    assert verdict["is_big_loss"] is None
    assert set(verdict["missing_fields"]) == {"next_high", "next_close"}


# --------------------------------------------------------------------------- #
# 3) "没有大面" ≠ "判不了"
# --------------------------------------------------------------------------- #
def test_collect_separates_no_big_loss_from_undecidable():
    """一份因为缺字段而空的库，绝不能被读成"这段时间没人吃面"。"""
    events = [
        _event("600000", high=11.0, close=10.9),          # 没大面
        _event("600001", next_high=None),                  # 判不了
        _event("600002", high=11.0, close=8.0),            # 大面
    ]
    events[1].pop("next_high", None)
    result = bl.collect(events)
    assert result["examined"] == 3
    assert [row["code"] for row in result["records"]] == ["600002"]
    assert result["undecidable_count"] == 1
    assert result["undecidable"][0]["reason"] == "required_fields_missing"


def test_collect_on_empty_input_reports_zero_examined():
    result = bl.collect([])
    assert result["examined"] == 0 and result["records"] == []
    assert result["undecidable_count"] == 0


def test_collect_keeps_context_fields_for_later_stratification():
    result = bl.collect([_event(high=11.0, close=8.0, first_seal_time="0931",
                                open_board_count=2)])
    row = result["records"][0]
    assert row["board_level"] == 2 and row["sector"] == "半导体"
    assert row["sentiment_state"] == "退潮"
    assert row["first_seal_time"] == "0931" and row["open_board_count"] == 2


# --------------------------------------------------------------------------- #
# 4) 合并幂等：重跑不改写历史判定
# --------------------------------------------------------------------------- #
def test_merge_is_idempotent_on_date_code():
    first = [{"date": "2026-06-03", "code": "600000", "drawdown_pct": -20.0}]
    merged = bl.merge_records(first, first)
    assert len(merged) == 1


def test_merge_keeps_existing_record_when_upstream_revises():
    """上游数据修订不得改写已入库的判定，否则分层统计失去可复现性。"""
    existing = [{"date": "2026-06-03", "code": "600000", "drawdown_pct": -20.0}]
    revised = [{"date": "2026-06-03", "code": "600000", "drawdown_pct": -3.0}]
    merged = bl.merge_records(existing, revised)
    assert merged[0]["drawdown_pct"] == -20.0


def test_merge_appends_new_keys_and_sorts():
    merged = bl.merge_records(
        [{"date": "2026-06-03", "code": "600001", "drawdown_pct": -18.0}],
        [{"date": "2026-06-02", "code": "600000", "drawdown_pct": -22.0}],
    )
    assert [row["code"] for row in merged] == ["600000", "600001"]


# --------------------------------------------------------------------------- #
# 5) 样本不足不给分层结论
# --------------------------------------------------------------------------- #
def test_summarize_below_threshold_is_unverified_and_withholds_groups():
    records = [{"sentiment_state": "退潮", "drawdown_pct": -20.0} for _ in range(5)]
    result = bl.summarize(records, min_samples=30)
    assert result["status"] == bl.UNVERIFIED
    assert result["groups"] == {}
    assert result["n"] == 5


def test_summarize_at_threshold_reports_groups():
    records = ([{"sentiment_state": "退潮", "drawdown_pct": -20.0} for _ in range(20)]
               + [{"sentiment_state": "高潮", "drawdown_pct": -30.0} for _ in range(10)])
    result = bl.summarize(records, min_samples=30)
    assert result["status"] == bl.AVAILABLE
    assert result["groups"]["退潮"]["n"] == 20
    assert result["groups"]["高潮"]["mean_drawdown_pct"] == pytest.approx(-30.0)


def test_summarize_empty_is_unavailable():
    assert bl.summarize([])["status"] == bl.UNAVAILABLE
