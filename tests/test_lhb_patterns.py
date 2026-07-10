"""龙虎榜模式识别 — issue #88（翔鹭钨业 -25.2% 复盘）的模式/策略/退出信号测试。

洗盘-建仓、高潮见顶、换手率趋势的用例直接取自复盘时间线：
6/12 净卖-3.07亿(换手44%) → 6/15 +0.72 → 6/16 +1.67 → 6/17 -2.15 →
6/18 +1.08 → 6/23 +1.83 → 7/02 +2.72(前3日均值3倍+) → 7/03 -1.07。
"""

import lhb_patterns as lp
from exit_signals import (
    check_deep_research_exit,
    check_lhb_climax,
    evaluate_all_exit_signals,
)

XIANGLU_SEQ = [
    {"date": "2026-06-12", "net_yi": -3.07, "turnover_pct": 44.0, "close": 41.0},
    {"date": "2026-06-15", "net_yi": 0.72, "turnover_pct": 37.0, "close": 42.0},
    {"date": "2026-06-16", "net_yi": 1.67, "turnover_pct": 31.0, "close": 43.5},
    {"date": "2026-06-17", "net_yi": -2.15, "turnover_pct": 27.0, "close": 42.8},
    {"date": "2026-06-18", "net_yi": 1.08, "turnover_pct": 33.0, "close": 42.0},
    {"date": "2026-06-23", "net_yi": 1.83, "turnover_pct": 26.0, "close": 49.5},
    {"date": "2026-06-26", "net_yi": 0.98, "turnover_pct": 27.0, "close": 50.2},
    {"date": "2026-07-02", "net_yi": 2.72, "turnover_pct": 22.0, "close": 51.0},
]


# ========== 洗盘-建仓 ==========

def test_wash_accumulation_detected_on_xianglu_timeline():
    result = lp.detect_wash_accumulation(XIANGLU_SEQ[:3])
    assert result["matched"]
    assert result["wash_date"] == "2026-06-12"
    assert result["accumulation_dates"] == ["2026-06-15", "2026-06-16"]
    assert "主力建仓" in result["note"]


def test_wash_without_followup_buying_not_matched():
    seq = [
        {"date": "2026-06-12", "net_yi": -3.07},
        {"date": "2026-06-15", "net_yi": -0.5},
        {"date": "2026-06-16", "net_yi": 0.3},
    ]
    assert not lp.detect_wash_accumulation(seq)["matched"]


def test_small_sell_below_threshold_not_wash():
    seq = [
        {"date": "2026-06-12", "net_yi": -1.0},
        {"date": "2026-06-15", "net_yi": 0.7},
        {"date": "2026-06-16", "net_yi": 1.6},
    ]
    assert not lp.detect_wash_accumulation(seq)["matched"]


# ========== 高潮见顶 ==========

def test_climax_detected_on_0702_volume_burst():
    # 前3上榜日 |净买| 均值 (2.15+1.08+1.83)/3 ≈ 1.69 → 需 >5.06 才是3倍
    seq = XIANGLU_SEQ[:5] + [
        {"date": "2026-06-23", "net_yi": 1.83},
        {"date": "2026-07-02", "net_yi": 6.0},
    ]
    result = lp.detect_climax_volume(seq)
    assert result["matched"]
    assert "高潮见顶" in result["note"]


def test_actual_0702_ratio_below_3x_is_not_climax():
    # 复盘原始数据 2.72亿 对前3日均值约1.30亿 ≈ 2.1倍，未到3倍阈值
    result = lp.detect_climax_volume(XIANGLU_SEQ)
    assert not result["matched"]
    assert result["latest_net_yi"] == 2.72


def test_climax_needs_at_least_four_days():
    assert not lp.detect_climax_volume(XIANGLU_SEQ[:3])["matched"]


# ========== 换手率趋势 ==========

def test_concentrating_turnover_declining_price_rising():
    seq = [
        {"date": "d1", "net_yi": 1, "turnover_pct": 20.0, "close": 100.0},
        {"date": "d2", "net_yi": 1, "turnover_pct": 15.0, "close": 110.0},
        {"date": "d3", "net_yi": 1, "turnover_pct": 11.0, "close": 125.0},
    ]
    result = lp.turnover_price_trend(seq)
    assert result["pattern"] == "concentrating"
    assert "筹码集中" in result["note"]


def test_churning_sustained_high_turnover():
    result = lp.turnover_price_trend(XIANGLU_SEQ[:4])
    assert result["pattern"] == "churning"
    assert "游资倒手" in result["note"]


def test_neutral_when_insufficient_days():
    assert lp.turnover_price_trend(XIANGLU_SEQ[:2])["pattern"] == "neutral"


# ========== 持有策略（席位主体分层） ==========

def test_institution_led_loosens_trailing_stop():
    policy = lp.holding_policy("institution")
    assert policy["style"] == "institution_led"
    assert policy["trailing_pct"] == 8.0
    assert policy["horizon_days"] == 10


def test_hot_money_led_tightens_discipline():
    policy = lp.holding_policy("hot_money")
    assert policy["trailing_pct"] == 4.0
    assert policy["horizon_days"] == 3


def test_climax_forces_exit_flag():
    policy = lp.holding_policy("hot_money", climax_matched=True)
    assert policy["climax_exit"] is True
    assert "立即止盈" in policy["note"]


# ========== 完整画像 ==========

def test_build_profile_combines_patterns_and_policy():
    seat_summary = {"dominant_force": "hot_money", "hot_money_led": True}
    profile = lp.build_lhb_profile(
        XIANGLU_SEQ, seat_summary, code="002842", asof="2026-07-02",
    )
    assert profile["schema"] == "lhb_profile_v1"
    assert profile["code"] == "002842"
    assert profile["wash_accumulation"]["matched"]
    assert profile["dominant_force"] == "hot_money"
    assert profile["policy"]["trailing_pct"] == 4.0
    assert profile["notes"]


# ========== akshare 明细行归一化（dragon_tiger 纯函数） ==========

def test_normalize_lhb_daily_rows_merges_same_day_boards():
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location(
        "dragon_tiger",
        os.path.join(os.path.dirname(__file__), "..", "skills",
                     "hot-money-tactics", "scripts", "dragon_tiger.py"),
    )
    dt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dt)

    records = [
        {"上榜日": "2026-07-02", "龙虎榜净买额": 150_000_000.0,
         "换手率": 22.0, "收盘价": 51.0},
        {"上榜日": "2026-07-02", "龙虎榜净买额": 122_000_000.0,
         "换手率": 22.0, "收盘价": 51.0},
        {"上榜日": "2026-06-23", "龙虎榜净买额": 183_000_000.0,
         "换手率": 26.0, "收盘价": 49.5},
        {"上榜日": "", "龙虎榜净买额": 1.0},
    ]
    seq = dt.normalize_lhb_daily_rows(records)
    assert [r["date"] for r in seq] == ["2026-06-23", "2026-07-02"]
    assert seq[1]["net_yi"] == 2.72
    assert seq[1]["turnover_pct"] == 22.0


# ========== 退出信号接线（exit_signals） ==========

def test_lhb_climax_hot_money_led_is_critical_sell():
    profile = {
        "climax": {"matched": True, "note": "净买6.0亿为前3日均值3.5倍"},
        "dominant_force": "hot_money",
    }
    signal = check_lhb_climax(profile)
    assert signal["triggered"]
    assert signal["severity"] == "critical"
    assert signal["action"] == "sell"


def test_lhb_climax_institution_led_is_reduce():
    profile = {
        "climax": {"matched": True, "note": ""},
        "dominant_force": "institution",
    }
    signal = check_lhb_climax(profile)
    assert signal["severity"] == "warning" and signal["action"] == "reduce"


def test_lhb_climax_not_triggered_without_match():
    assert not check_lhb_climax({"climax": {"matched": False}})["triggered"]
    assert not check_lhb_climax(None)["triggered"]


def test_deep_research_exit_enforces_red_line():
    # 翔鹭钨业 6/28 深研 2.0/10，当时只出报告没出动作
    signal = check_deep_research_exit(2.0)
    assert signal["triggered"]
    assert signal["severity"] == "critical" and signal["action"] == "sell"

    reduce_signal = check_deep_research_exit(4.5)
    assert reduce_signal["severity"] == "warning" and reduce_signal["action"] == "reduce"

    assert not check_deep_research_exit(6.0)["triggered"]
    assert not check_deep_research_exit(None)["triggered"]


def test_evaluate_all_uses_seat_policy_trailing_stop():
    # 游资主导 → 回撤止盈收紧到4%：峰值100回落至95.5(-4.5%)即触发
    profile = {
        "climax": {"matched": False},
        "dominant_force": "hot_money",
        "policy": {"style": "hot_money_led", "trailing_pct": 4.0},
    }
    result = evaluate_all_exit_signals(
        current_price=95.5,
        peak_price=100.0,
        current_pnl_pct=10.0,
        lhb_profile=profile,
    )
    assert result["action"] == "sell"
    assert result["top_signal"]["signal_type"] == "trailing_stop"

    # 默认5%阈值下同样的回撤不触发
    baseline = evaluate_all_exit_signals(
        current_price=95.5, peak_price=100.0, current_pnl_pct=10.0,
    )
    assert all(
        s["signal_type"] != "trailing_stop" or not s.get("triggered")
        for s in baseline["signals"]
    )


def test_evaluate_all_deep_score_red_line_flows_through():
    result = evaluate_all_exit_signals(current_price=50.0, deep_score=2.0)
    assert result["action"] == "sell"
    assert result["top_signal"]["signal_type"] == "deep_research_exit"
