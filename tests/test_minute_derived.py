#!/usr/bin/env python3
"""分钟线派生层单测 —— 禁未来函数 / 单位与累计语义 / fail-closed 三类是硬约束。

这些用例的职责是守住三件容易被静默绕过的事：
1. 多喂 until_time 之后的行不得改变结果（未来函数）；
2. 累计值不当增量用、成交量单位「手」必须折算（仓内出过低估 100 倍的事故）；
3. 数据缺 / 不全 / 池外一律 unavailable，绝不返回 0 或代理值。
改实现前先读这段。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "common"))

import minute_derived as md  # noqa: E402
import minute_derived_store as store  # noqa: E402

LOT = md.LOT_SHARES


def tencent_rows(pairs):
    """[(HHMM, 累计手数)] → 腾讯分时行（累计成交额按每股 10 元合成）。"""
    return [{"time": t, "price": 10.0, "cum_volume": float(v),
             "cum_amount": float(v) * LOT * 10.0} for t, v in pairs]


def full_morning(step=1, per_minute_lots=100.0):
    """09:30..09:50 的腾讯分时：每分钟固定成交 per_minute_lots 手（累计值递增）。"""
    pairs = []
    total = 0.0
    for minute in range(570, 591, step):
        total += per_minute_lots
        pairs.append((f"{minute // 60:02d}{minute % 60:02d}", total))
    return tencent_rows(pairs)


# --------------------------------------------------------------------------- #
# 1. 单位与累计语义
# --------------------------------------------------------------------------- #
def test_tencent_cumulative_is_differenced_and_lots_converted():
    """累计（手）→ 增量（股）：若把累计值当增量用，第 3 根会是 300 手而不是 100 手。"""
    rows = md.normalize_tencent_minute(tencent_rows([("0930", 100), ("0931", 200),
                                                     ("0932", 300)]))
    assert [row["volume_shares"] for row in rows] == [100 * LOT, 100 * LOT, 100 * LOT]
    # 这条断言就是「当增量处理会算错」的样本：累计口径下总量是 300 手 = 30000 股，
    # 当增量相加会得到 600 手 = 60000 股，正好是错一倍的那类事故。
    assert sum(row["volume_shares"] for row in rows) == 300 * LOT


def test_lot_conversion_is_not_optional():
    """漏乘每手股数就会低估 100 倍 —— 用绝对数字钉死，不给"看起来差不多"留缝。"""
    rows = md.normalize_tencent_minute(tencent_rows([("0930", 1)]))
    assert rows[0]["volume_shares"] == 100.0


def test_sina_rows_are_incremental_shares():
    rows = md.normalize_sina_minute([
        {"day": "2026-08-25 09:35:00", "volume": 8300378.0, "amount": 96157152.0},
        {"day": "2026-08-25 09:40:00", "volume": 3310776.0, "amount": 38287525.0},
    ])
    assert [row["minute"] for row in rows] == [575, 580]
    assert rows[0]["volume_shares"] == 8300378.0


def test_baostock_rows_are_incremental_shares_and_use_bar_close_time():
    rows = md.normalize_baostock_minute([
        {"time": "20260827093500000", "open": "9.9", "high": "10.1",
         "low": "9.8", "close": "10.0", "volume": "182300", "amount": "237030374"},
        {"time": "20260827094000000", "open": "10.0", "high": "10.2",
         "low": "9.9", "close": "10.1", "volume": "109200", "amount": "141910000"},
    ])

    assert [row["minute"] for row in rows] == [575, 580]
    assert rows[0]["volume_shares"] == 182300.0
    assert rows[0]["amount"] == 237030374.0
    assert rows[0]["close"] == 10.0


def test_non_monotonic_cumulative_is_rejected_not_clamped():
    assert md.normalize_tencent_minute(tencent_rows([("0930", 200), ("0931", 100)])) is None


# --------------------------------------------------------------------------- #
# 2. 禁未来函数
# --------------------------------------------------------------------------- #
def test_volume_ratio_ignores_rows_after_checkpoint():
    """截断输入 vs 全天输入，结果必须逐位相等。"""
    truncated = md.normalize_tencent_minute(full_morning()[:16])   # 到 09:45
    whole_day = md.normalize_tencent_minute(full_morning())        # 到 09:50
    baseline = 1000.0
    left = md.volume_ratio_at(truncated, "09:45", baseline)
    right = md.volume_ratio_at(whole_day, "09:45", baseline)
    assert left["availability"] == md.AVAILABLE
    assert left["value"] == right["value"]


def test_volume_ratio_changes_when_pre_checkpoint_data_changes():
    """反向对照：改动 checkpoint **之前**的量必须改变结果，否则上一条测试是恒真的。"""
    baseline = 1000.0
    quiet = md.volume_ratio_at(md.normalize_tencent_minute(full_morning()), "09:45", baseline)
    busy = md.volume_ratio_at(
        md.normalize_tencent_minute(full_morning(per_minute_lots=200.0)), "09:45", baseline)
    assert busy["value"] > quiet["value"]


def test_cumulative_turnover_ignores_rows_after_until_time():
    truncated = md.normalize_tencent_minute(full_morning()[:16])
    whole_day = md.normalize_tencent_minute(full_morning())
    shares = 1_000_000.0
    left = md.cumulative_turnover_before(truncated, "09:45", shares)
    right = md.cumulative_turnover_before(whole_day, "09:45", shares)
    assert left["availability"] == md.AVAILABLE
    assert left["value"] == right["value"]


def test_cumulative_turnover_value_matches_hand_calculation():
    """09:30..09:45 共 16 根 × 100 手 = 160000 股；流通股本 1,000,000 → 16%。"""
    rows = md.normalize_tencent_minute(full_morning())
    result = md.cumulative_turnover_before(rows, "09:45", 1_000_000.0)
    assert result["value"] == pytest.approx(16.0)


# --------------------------------------------------------------------------- #
# 3. 量比口径：分母是走过的分钟数，不是行数
# --------------------------------------------------------------------------- #
def test_volume_ratio_denominator_is_elapsed_minutes_not_bar_count():
    """同一天的 1 分钟线与 5 分钟线必须算出同一个量比（差异只来自归桶边界）。"""
    minute_rows = md.normalize_tencent_minute(full_morning())
    five_min = md.downsample_rows(minute_rows, step_minutes=5)
    baseline = 1000.0
    fine = md.volume_ratio_at(minute_rows, "09:45", baseline)
    coarse = md.volume_ratio_at(five_min, "09:45", baseline)
    assert coarse["availability"] == md.AVAILABLE
    assert coarse["value"] == pytest.approx(fine["value"])


def test_elapsed_trading_minutes_skips_lunch_break():
    assert md.elapsed_trading_minutes(md.parse_minute("09:45")) == 15
    assert md.elapsed_trading_minutes(md.parse_minute("11:30")) == 120
    assert md.elapsed_trading_minutes(md.parse_minute("13:05")) == 125
    assert md.elapsed_trading_minutes(md.parse_minute("15:00")) == 240


# --------------------------------------------------------------------------- #
# 4. fail-closed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rows,expected", [
    (None, "minute_rows_missing"),
    ([], "minute_rows_missing"),
])
def test_missing_rows_are_unavailable_not_zero(rows, expected):
    result = md.volume_ratio_at(rows, "09:45", 1000.0)
    assert result["value"] is None
    assert result["availability"] == f"{md.UNAVAILABLE}:{expected}"


def test_rows_truncated_before_checkpoint_are_unavailable():
    rows = md.normalize_tencent_minute(full_morning()[:6])  # 只到 09:35
    result = md.volume_ratio_at(rows, "09:45", 1000.0)
    assert result["value"] is None
    assert result["availability"].endswith("minute_rows_truncated_before_checkpoint")


def test_rows_with_holes_are_unavailable():
    """只抓到 3 根 1 分钟线却横跨到 09:45 —— 中间有洞，不能当成"这段就只成交这么多"。"""
    rows = md.normalize_tencent_minute(tencent_rows([("0930", 100), ("0931", 200),
                                                     ("0945", 300)]))
    result = md.volume_ratio_at(rows, "09:45", 1000.0)
    assert result["value"] is None
    assert result["availability"].startswith(f"{md.UNAVAILABLE}:minute_rows_incomplete")


def test_missing_baseline_is_unavailable():
    rows = md.normalize_tencent_minute(full_morning())
    result = md.volume_ratio_at(rows, "09:45", None)
    assert result["value"] is None
    assert result["availability"].endswith("baseline_per_minute_unavailable")


def test_missing_float_shares_is_unavailable():
    rows = md.normalize_tencent_minute(full_morning())
    result = md.cumulative_turnover_before(rows, "09:45", None)
    assert result["value"] is None
    assert result["availability"].endswith("float_shares_unavailable")


def test_missing_reseal_time_is_unavailable():
    rows = md.normalize_tencent_minute(full_morning())
    result = md.cumulative_turnover_before(rows, None, 1_000_000.0)
    assert result["value"] is None
    assert result["availability"].endswith("until_time_missing")


def test_baseline_sample_shortfall_is_unavailable_not_partial_average():
    kline = [{"date": f"2026-08-{day:02d}", "volume": 1000.0} for day in (18, 19, 24, 25)]
    result = md.baseline_per_minute_from_daily(kline, "2026-08-25", window_days=5)
    assert result["value"] is None
    assert result["availability"].startswith(f"{md.UNAVAILABLE}:baseline_sample_insufficient")


def test_baseline_uses_only_days_before_event():
    kline = [{"date": f"2026-08-{day:02d}", "volume": 1200.0} for day in range(11, 19)]
    kline.append({"date": "2026-08-19", "volume": 999999.0})   # 事件日自身，必须被排除
    result = md.baseline_per_minute_from_daily(kline, "2026-08-19", window_days=5)
    assert result["availability"] == md.AVAILABLE
    assert result["value"] == pytest.approx(1200.0 * LOT / md.SESSION_MINUTES)


# --------------------------------------------------------------------------- #
# 5. 落盘层：有界 + 合并不倒退
# --------------------------------------------------------------------------- #
def test_merge_keeps_available_value_when_later_round_fails():
    existing = {"000001": {"volume_ratio": 1.8, "volume_ratio_availability": "available"}}
    incoming = {"000001": {"volume_ratio": None,
                           "volume_ratio_availability": "unavailable:minute_rows_missing"}}
    merged = store.merge_records(existing, incoming)
    assert merged["records"]["000001"]["volume_ratio"] == 1.8
    assert merged["records"]["000001"]["volume_ratio_availability"] == "available"


def test_merge_never_shortens_an_existing_curve():
    existing = {"000001": {"slots": {"0935": 1.0, "0940": 2.0, "0945": 3.0}}}
    incoming = {"000001": {"slots": {"0935": 1.0}}}
    merged = store.merge_records(existing, incoming)
    assert len(merged["records"]["000001"]["slots"]) == 3


def test_merge_is_bounded_by_max_codes():
    incoming = {f"{i:06d}": {"volume_ratio": 1.0} for i in range(50)}
    merged = store.merge_records({}, incoming, max_codes=10)
    assert len(merged["records"]) == 10
    assert merged["truncated"] == 40


def test_slim_record_drops_raw_minute_bars():
    """落盘白名单：原始分钟条不许混进去（5000 股 × 240 根的体量是硬约束）。"""
    slim = store.slim_record({"volume_ratio": 1.5, "minute_bars": [1] * 240,
                              "slots": {"0935": 1.0}})
    assert "minute_bars" not in slim
    assert set(slim) == {"volume_ratio", "slots"}


def test_slots_roundtrip_preserves_window_semantics():
    rows = md.normalize_tencent_minute(full_morning())
    slim = md.downsample_rows(rows, step_minutes=5)
    restored = md.slots_to_rows(md.rows_to_slots(slim))
    assert md.cumulative_turnover_before(restored, "09:45", 1_000_000.0)["value"] == \
        pytest.approx(md.cumulative_turnover_before(slim, "09:45", 1_000_000.0)["value"])


def test_corrupt_slots_are_rejected_wholesale():
    assert md.slots_to_rows({"0935": 1.0, "bogus": 2.0}) is None
    assert md.slots_to_rows({"0935": -1.0}) is None
