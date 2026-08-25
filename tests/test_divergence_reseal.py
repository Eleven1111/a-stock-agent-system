"""S2 龙头分歧回封（DivergenceReseal）— 纯逻辑单测，不触网。

覆盖四类断言（升级方案 §6.2，照 tests/test_rank_surprise.py 的组织方式）：
1) 反事实：关掉 P5 成交约束后回测收益显著虚高 —— 证明约束真的在咬（防假绿主证据）；
2) NON-LIVE 消费端**行为**断言：未注册状态下正向信号被降级为 watch、仓位归零
   （不是断言 pack 里 live:false 这种字段值 —— 配置断言 ≠ 行为断言）；
3) 防未来函数：先回封但后来炸板的标的仍被选中（排名只看回封时刻）；
   20 日同期换手基准缺失 → unavailable；
4) 四个入场条件各自的边界 + 全缺失 → unavailable。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

import divergence_reseal as dr

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "chanlun-backtest" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_SPEC = importlib.util.spec_from_file_location(
    "daban_bt_divergence_reseal", SCRIPTS / "daban_bt_divergence_reseal.py"
)
bt = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bt)


# --------------------------------------------------------------------------- #
# 合成 fixture —— 信号层
# --------------------------------------------------------------------------- #
def _record(code, *, sector="X", date="2026-06-03", breadth=5.0, fast=3.0,
            reseal_time="093000", turnover=6.0, baseline_median=3.0, baseline_days=20,
            **extra):
    record = {
        "code": code, "date": date, "sector": sector,
        "sector_limit_up_count": breadth, "sector_fast_seal_count": fast,
        "reseal_time": reseal_time, "pre_reseal_turnover_pct": turnover,
        "turnover_baseline_median_pct": baseline_median,
        "turnover_baseline_sample_days": baseline_days,
    }
    record.update(extra)
    return record


def _group(n=6):
    """n 个同板块同日候选池，回封时刻依次递增（index 0 最早）；
    换手倍数固定在 6.0/3.0=2.0（落在 [1.5,3.0] 内）。"""
    return [_record(f"60000{i}", reseal_time=f"09{30 + i:02d}00") for i in range(n)]


def _evaluate_target(peers, target_code="600000"):
    target = next(p for p in peers if p["code"] == target_code)
    return dr.evaluate(target, peers=peers)


# --------------------------------------------------------------------------- #
# 合成 fixture —— 回测层（8 事件：6 个填充 + 2 个命中，命中中 1 个一字板不可成交）
# --------------------------------------------------------------------------- #
def _event(code, *, one_word, gap_pct_value, t1_close, seal="093000",
           date="2026-06-03", sector="X", reseal_time="100000",
           breadth=5.0, fast=3.0, turnover=6.0, baseline_median=3.0, baseline_days=20,
           amount=3.0e7):
    """T 日主板涨停事件。one_word=True → 全日一字封死（约束模型判买不进）。"""
    prev, close = 10.0, 11.0
    t1_open = close * (1 + gap_pct_value / 100.0)
    bar = ({"t_open": close, "t_high": close, "t_low": close}
           if one_word else {"t_open": close, "t_high": close, "t_low": 10.2})
    return {
        "code": code, "name": "SYN", "date": date, "sector": sector,
        "t_prev_close": prev, "t_close": close,
        "t_volume": 1.0e6, "t_amount": amount,
        "t1_open": t1_open, "t1_close": t1_close,
        "t1_high": max(t1_open, t1_close), "t1_low": min(t1_open, t1_close),
        "t1_volume": 1.0e6, "t1_amount": amount,
        "first_seal": seal, "lianban": 1, "is_st": False,
        "sector_limit_up_count": breadth, "sector_fast_seal_count": fast,
        "reseal_time": reseal_time, "pre_reseal_turnover_pct": turnover,
        "turnover_baseline_median_pct": baseline_median,
        "turnover_baseline_sample_days": baseline_days,
        **bar,
    }


def _events():
    """8 个同板块事件：回封最早的两个命中 S2。
    其中一个是回封板（买得进，涨幅温和），另一个是 T 日一字板（约束下买不进，
    且次日暴涨）——用于反事实证明 P5 约束真的在咬。"""
    events = []
    for i in range(6):
        events.append(_event(f"6001{i:02d}", one_word=False, gap_pct_value=float(i),
                             t1_close=11.0, seal=f"09{30 + i}00",
                             reseal_time=f"10{i:02d}00"))
    # 命中者 A：回封板，可成交，次日 +1%，回封最早
    events.append(_event("600106", one_word=False, gap_pct_value=6.0,
                         t1_close=11.11, seal="093600", reseal_time="093100"))
    # 命中者 B：一字板，约束下买不进；次日 +20%（关掉约束就会把它算进收益），回封次早
    events.append(_event("600107", one_word=True, gap_pct_value=7.0,
                         t1_close=13.2, seal="093700", reseal_time="093200"))
    return events


# --------------------------------------------------------------------------- #
# 1) 反事实：关掉成交约束 → 收益虚高（防假绿主证据）
# --------------------------------------------------------------------------- #
def test_counterfactual_disabling_constraints_inflates_returns():
    report = bt.counterfactual(_events(), hold_mode="board_overnight")
    on, off = report["with_constraints"], report["without_constraints"]

    # 样本非空断言 —— 空集下任何"约束生效"的结论都是恒真的假绿。
    assert on["signal_count"] == 2, on["signal_summary"]
    assert on["filled_count"] == 1, "约束打开时一字板必须被剔除，只剩回封板可成交"
    assert off["filled_count"] == 2, "约束关闭时一字板会被算进收益"

    assert report["excluded_by_constraints"] == 1
    assert on["returns"]["mean"] is not None and off["returns"]["mean"] is not None
    # 一字板次日 +20% vs 回封板 +1%：关掉约束后均值必然显著虚高。
    assert report["mean_return_inflation"] > 0.05, report
    assert off["returns"]["mean"] > on["returns"]["mean"] * 2
    assert report["constraints_bite"] is True


def test_counterfactual_reports_no_bite_on_empty_sample():
    """零样本时不得报"约束在咬" —— 空集恒真是假绿的经典来源。"""
    report = bt.counterfactual([], hold_mode="board_overnight")
    assert report["with_constraints"]["filled_count"] == 0
    assert report["constraints_bite"] is False
    assert report["mean_return_inflation"] is None


# --------------------------------------------------------------------------- #
# 2) NON-LIVE：消费端行为断言（不是配置字段断言）
# --------------------------------------------------------------------------- #
def test_unregistered_signal_is_downgraded_to_watch_by_decision_policy():
    """构造一个 S2 正向信号，断言**消费端**把 buy 降为 watch、仓位倍率归零。"""
    import decision_policy
    import strategy_registry

    fired = [r for r in dr.evaluate_group(_group()) if r["status"] == dr.STATUS_SIGNAL]
    assert fired, "前置：必须真的有一个正向 S2 信号，否则本用例恒真"

    record = strategy_registry.live_record("divergence_reseal")
    assert record is None, "S2 不得出现在 strategy_registry 中"

    policy = decision_policy.evaluate_decision(
        requested_action="buy",
        quality_report={"status": "passed"},
        strategy_record=record,
        market_regime={"regime": "neutral", "context_status": "fresh"},
    )
    assert policy["decision"] == "watch"
    assert policy["position_multiplier"] == 0.0
    assert policy["abstain"] is True
    assert "strategy_unverified" in policy["reasons"]


def test_unregistered_signal_gets_zero_position_from_position_guidance():
    import recommendation_audit as ra

    guidance = ra.position_guidance("divergence_reseal", 10.0, 12.0, 9.0, total_asset=100000.0)
    assert guidance["recommended_position_pct"] == 0.0
    assert guidance["recommended_amount"] == 0.0
    assert guidance["method"] == "research_only"
    assert guidance["gating_status"] == "unverified"


def test_pack_is_reported_as_ungated_research_hypothesis():
    import strategy_packs

    record = strategy_packs.registry_records()["divergence_reseal"]
    assert record["allowed_in_live_agent"] is False
    assert record["gate_decision"] == "not_gated"
    assert strategy_packs.load_packs()["divergence_reseal"]["score_hints"] == []


# --------------------------------------------------------------------------- #
# 3) 防未来函数：排名只看回封时刻，20 日基准缺失必须 unavailable
# --------------------------------------------------------------------------- #
def test_earlier_reseal_selected_even_if_it_breaks_again_later():
    """先回封（时刻最早，rank=1）但后来又炸板的标的，仍必须被选中——
    因为判定发生在回封时刻，不看后续结果。`later_break` 字段本身对
    reseal_rank/evaluate 完全不可见，加不加都不能改变结果。"""
    peers = _group()
    target = next(p for p in peers if p["code"] == "600000")
    target["later_break"] = True  # 后续结果字段——signal 层从不读取

    with_flag = dr.evaluate(target, peers=peers)

    peers_without_flag = _group()
    without_flag = dr.evaluate(
        next(p for p in peers_without_flag if p["code"] == "600000"),
        peers=peers_without_flag,
    )

    assert with_flag["status"] == dr.STATUS_SIGNAL
    assert with_flag["reseal_rank"] == 1
    # 加不加 later_break 字段，判定结果必须逐字段一致（排除 code 无关差异）。
    assert with_flag["status"] == without_flag["status"]
    assert with_flag["reseal_rank"] == without_flag["reseal_rank"]
    assert with_flag["conditions"] == without_flag["conditions"]


def test_turnover_baseline_missing_sample_is_unavailable_not_no_signal():
    """20 日同期换手基准样本不足 → unavailable，绝不当作"未充分换手"处理。"""
    peers = _group()
    target = next(p for p in peers if p["code"] == "600000")
    target["turnover_baseline_sample_days"] = 10  # < min_baseline_sample_days(15)
    result = dr.evaluate(target, peers=peers)
    assert result["status"] == dr.STATUS_UNAVAILABLE
    assert any("turnover_baseline_sample_insufficient" in r for r in result["reasons"])


def test_turnover_ratio_helper_never_degrades_missing_to_zero():
    ratio = dr.turnover_ratio(
        pre_reseal_turnover_pct=None,
        turnover_baseline_median_pct=3.0,
        turnover_baseline_sample_days=20,
    )
    assert ratio["status"] == dr.STATUS_UNAVAILABLE
    assert ratio["value"] is None


def test_reseal_rank_uses_time_order_not_input_order():
    """输入顺序与回封时刻顺序故意错开：排名必须按 reseal_time，不能按输入下标。

    只用 _group()（其构造恰好让下标与时间同向递增）测不出"按下标排序"这类
    退化实现——必须单独构造一个二者不一致的样本。"""
    peers = [
        _record("A", reseal_time="094500"),  # 输入第0个，但回封最晚
        _record("B", reseal_time="093000"),  # 输入第1个，但回封最早
        _record("C", reseal_time="094000"),  # 输入第2个，回封次晚
        _record("D", reseal_time="093500"),  # 输入第3个，回封次早
    ]
    ranks = {p["code"]: r for p, r in zip(peers, dr.reseal_rank(peers))}
    assert ranks == {"B": 1, "D": 2, "C": 3, "A": 4}


def test_reseal_rank_ignores_unknown_extra_fields_and_uses_time_order_only():
    peers = _group(n=4)
    # 打乱输入顺序、附加各种"结果性"字段，排名必须只由 reseal_time 决定。
    peers[0]["later_break"] = True
    peers[2]["closed_price_change_pct"] = -9.9  # 收盘结果字段，非入参
    ranks = dr.reseal_rank(peers)
    assert ranks == [1, 2, 3, 4]


def test_below_preferred_baseline_sample_is_degraded_not_blocked():
    """样本天数在 [min, preferred) 区间：能算但打折，标记 degraded，不阻断信号。"""
    peers = _group()
    target = next(p for p in peers if p["code"] == "600000")
    target["turnover_baseline_sample_days"] = 16  # >= min(15) 但 < preferred(20)
    result = dr.evaluate(target, peers=peers)
    assert result["status"] == dr.STATUS_SIGNAL
    assert any("turnover_baseline_sample_below_preferred" in d for d in result["degraded"])


# --------------------------------------------------------------------------- #
# 4) 四个入场条件的边界 + 不满足时不触发
# --------------------------------------------------------------------------- #
def test_baseline_group_fires_for_top2_by_reseal_time_only():
    r0 = _evaluate_target(_group(), "600000")
    r1 = _evaluate_target(_group(), "600001")
    r2 = _evaluate_target(_group(), "600002")
    assert r0["status"] == dr.STATUS_SIGNAL and r0["reseal_rank"] == 1
    assert r1["status"] == dr.STATUS_SIGNAL and r1["reseal_rank"] == 2
    assert r2["status"] == dr.STATUS_NO_SIGNAL and r2["reseal_rank"] == 3
    assert {c["id"]: c["ok"] for c in r2["conditions"]}[dr.COND_RESEAL_RANK] is False


@pytest.mark.parametrize("breadth,expected", [(2.0, False), (3.0, True)])
def test_sector_breadth_boundary_is_inclusive_ge(breadth, expected):
    peers = _group()
    target = next(p for p in peers if p["code"] == "600000")
    target["sector_limit_up_count"] = breadth
    result = _evaluate_target(peers)
    assert {c["id"]: c["ok"] for c in result["conditions"]}[dr.COND_SECTOR_BREADTH] is expected
    assert result["status"] == (dr.STATUS_SIGNAL if expected else dr.STATUS_NO_SIGNAL)


@pytest.mark.parametrize("fast,expected", [(1.0, False), (2.0, True)])
def test_fast_seal_density_boundary_is_inclusive_ge(fast, expected):
    peers = _group()
    target = next(p for p in peers if p["code"] == "600000")
    target["sector_fast_seal_count"] = fast
    result = _evaluate_target(peers)
    assert {c["id"]: c["ok"] for c in result["conditions"]}[dr.COND_FAST_SEAL_DENSITY] is expected
    assert result["status"] == (dr.STATUS_SIGNAL if expected else dr.STATUS_NO_SIGNAL)


@pytest.mark.parametrize("turnover,expected", [
    (4.47, False),  # ratio≈1.49 < 1.5
    (4.5, True),    # ratio==1.5 边界含
    (9.0, True),    # ratio==3.0 边界含
    (9.03, False),  # ratio≈3.01 > 3.0
])
def test_turnover_band_boundary_is_inclusive_both_ends(turnover, expected):
    peers = _group()
    target = next(p for p in peers if p["code"] == "600000")
    target["pre_reseal_turnover_pct"] = turnover
    result = _evaluate_target(peers)
    assert {c["id"]: c["ok"] for c in result["conditions"]}[dr.COND_TURNOVER_BAND] is expected
    assert result["status"] == (dr.STATUS_SIGNAL if expected else dr.STATUS_NO_SIGNAL)


def test_missing_sector_is_unavailable():
    peers = _group()
    target = next(p for p in peers if p["code"] == "600000")
    target["sector"] = None
    result = _evaluate_target(peers)
    assert result["status"] == dr.STATUS_UNAVAILABLE
    assert "sector_missing" in result["reasons"]


def test_missing_reseal_time_is_unavailable_not_no_signal():
    peers = _group()
    target = next(p for p in peers if p["code"] == "600000")
    target["reseal_time"] = None
    result = dr.evaluate(target, peers=peers)
    assert result["status"] == dr.STATUS_UNAVAILABLE
    assert "reseal_time_missing_or_not_resealed" in result["reasons"]
    assert result["reseal_rank"] is None


def test_record_not_in_peer_group_is_unavailable():
    peers = _group()
    outsider = _record("999999")
    result = dr.evaluate(outsider, peers=peers)
    assert result["status"] == dr.STATUS_UNAVAILABLE
    assert "record_not_in_peer_group" in result["reasons"]


def test_all_evidence_missing_is_unavailable():
    empty = {"code": "600001"}
    result = dr.evaluate(empty, peers=[empty])
    assert result["status"] == dr.STATUS_UNAVAILABLE
    assert result["reseal_rank"] is None
    for reason in (
        "sector_missing",
        "sector_limit_up_count_missing",
        "sector_fast_seal_count_missing",
        "reseal_time_missing_or_not_resealed",
    ):
        assert reason in result["reasons"]


# --------------------------------------------------------------------------- #
# 回测适配层：缺证据字段时必须 fail-closed 成零样本，且原因可见
# --------------------------------------------------------------------------- #
def test_backtest_fails_closed_when_s2_fields_absent_from_event_table():
    drop_keys = {
        "sector_limit_up_count", "sector_fast_seal_count", "reseal_time",
        "pre_reseal_turnover_pct", "turnover_baseline_median_pct",
        "turnover_baseline_sample_days",
    }
    events = [{k: v for k, v in event.items() if k not in drop_keys} for event in _events()]
    report = bt.run(events, hold_mode="board_overnight")
    assert report["signal_count"] == 0
    counts = report["signal_summary"]["status_counts"]
    assert counts[dr.STATUS_UNAVAILABLE] == report["universe_count"] > 0
    reasons = report["signal_summary"]["unavailable_reasons"]
    assert reasons.get("sector_limit_up_count_missing", 0) > 0
    assert reasons.get("reseal_time_missing_or_not_resealed", 0) > 0


def test_event_record_maps_s2_fields_directly():
    record = bt.event_record(_event(
        "600101", one_word=False, gap_pct_value=3.0, t1_close=11.0,
        reseal_time="093300", turnover=7.5, baseline_median=2.5, baseline_days=22,
    ))
    assert record["reseal_time"] == "093300"
    assert record["pre_reseal_turnover_pct"] == pytest.approx(7.5)
    assert record["turnover_baseline_median_pct"] == pytest.approx(2.5)
    assert record["turnover_baseline_sample_days"] == 22
