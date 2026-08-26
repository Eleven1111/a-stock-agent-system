"""S4 先于龙头套利（PreleaderArbitrage）— 纯逻辑单测，不触网。

覆盖（升级方案 §6.2 口径 + 本策略特有的"盘前表纪律"三条）：
1) 反事实：关掉 P5 成交约束后回测收益显著虚高；
2) NON-LIVE 消费端**行为**断言：未注册状态下正向信号被降级为 watch、仓位归零；
3) 盘前表纪律三条（本策略要害）：
   a) 盘前表带 as_of/generated_at，候选交易日 ≤ as_of（同日/早于）→ 判为"不是真
      盘前产物"，条件不通过；
   b) D0 表现极好但不在盘前表内的标的 —— 断言不触发（no_signal，不是补进表）；
   c) 把 D0 数据喂进 build_pretable 不改变结果（函数本身只吃 as_of 及更早数据）；
4) 四类入场条件各自边界 + 全缺失 → unavailable；
5) 回测适配层：真实事件表缺证据时必须 fail-closed。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

import preleader_arbitrage as pa

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "chanlun-backtest" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_SPEC = importlib.util.spec_from_file_location(
    "daban_bt_preleader_arbitrage", SCRIPTS / "daban_bt_preleader_arbitrage.py"
)
bt = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bt)


# --------------------------------------------------------------------------- #
# 合成 fixture —— 信号层
# --------------------------------------------------------------------------- #
def _leader(code="900000", *, confirmed=True, confirmed_time="093000", **extra):
    leader = {"code": code, "confirmed": confirmed, "confirmed_time": confirmed_time}
    leader.update(extra)
    return leader


def _candidate(code, *, attribute="X", date="2026-06-03", evaluation_time="093500",
               amount=3.0e7, **extra):
    record = {
        "code": code, "date": date, "attribute": attribute,
        "evaluation_time": evaluation_time, "amount": amount,
    }
    record.update(extra)
    return record


def _pretable(*, as_of="2026-06-02", leader_code="900000", attribute="X",
              candidates=("600000",), generated_at="2026-06-02T20:00:00"):
    return pa.build_pretable(
        [{"code": leader_code, "attribute": attribute, "date": as_of}],
        [
            {"code": code, "attribute": attribute, "date": as_of,
             "is_st": False, "material_bad_news": False, "avg_turnover_20d": 5.0e7}
            for code in candidates
        ],
        as_of=as_of, generated_at=generated_at,
    )


# --------------------------------------------------------------------------- #
# 4) 四个入场条件的边界 + 缺失 unavailable
# --------------------------------------------------------------------------- #
def test_full_signal_when_all_four_conditions_pass():
    leader = _leader()
    table = _pretable()
    candidate = _candidate("600000")
    result = pa.evaluate(candidate, leader=leader, pretable=table)
    assert result["status"] == pa.STATUS_SIGNAL
    assert {c["id"]: c["ok"] for c in result["conditions"]} == {
        pa.COND_PRETABLE_FRESH: True,
        pa.COND_PRETABLE_MEMBERSHIP: True,
        pa.COND_REACTION_WINDOW: True,
        pa.COND_LIQUIDITY: True,
    }


@pytest.mark.parametrize("as_of,candidate_date,expected", [
    ("2026-06-03", "2026-06-03", False),  # 表与候选同日 —— 不是真盘前产物
    ("2026-06-03", "2026-06-04", True),   # 表严格早于候选交易日 —— 合格
])
def test_pretable_fresh_boundary_requires_strictly_earlier_as_of(as_of, candidate_date, expected):
    table = _pretable(as_of=as_of, candidates=("600000",))
    leader = _leader()
    candidate = _candidate("600000", date=candidate_date)
    result = pa.evaluate(candidate, leader=leader, pretable=table)
    cond = {c["id"]: c["ok"] for c in result["conditions"]}[pa.COND_PRETABLE_FRESH]
    assert cond is expected
    assert result["status"] == (pa.STATUS_SIGNAL if expected else pa.STATUS_NO_SIGNAL)


def test_pretable_fresh_unavailable_when_pretable_missing():
    leader = _leader()
    candidate = _candidate("600000")
    result = pa.evaluate(candidate, leader=leader, pretable=None)
    assert result["status"] == pa.STATUS_UNAVAILABLE
    assert "pretable_missing" in result["reasons"]


@pytest.mark.parametrize("minutes,expected", [(11, False), (10, True)])
def test_reaction_window_boundary_is_inclusive_le(minutes, expected):
    leader = _leader(confirmed_time="093000")
    table = _pretable()
    eval_hh = 9 + (30 + minutes) // 60
    eval_mm = (30 + minutes) % 60
    candidate = _candidate("600000", evaluation_time=f"{eval_hh:02d}{eval_mm:02d}00")
    result = pa.evaluate(candidate, leader=leader, pretable=table)
    cond = {c["id"]: c["ok"] for c in result["conditions"]}[pa.COND_REACTION_WINDOW]
    assert cond is expected
    assert result["status"] == (pa.STATUS_SIGNAL if expected else pa.STATUS_NO_SIGNAL)


def test_reaction_window_unavailable_when_leader_confirmed_state_unknown():
    leader = {"code": "900000", "confirmed": None}
    table = _pretable()
    candidate = _candidate("600000")
    result = pa.evaluate(candidate, leader=leader, pretable=table)
    assert result["status"] == pa.STATUS_UNAVAILABLE
    assert "leader_confirmed_unknown" in result["reasons"]


def test_reaction_window_false_when_leader_not_confirmed():
    leader = _leader(confirmed=False, confirmed_time=None)
    table = _pretable()
    candidate = _candidate("600000")
    result = pa.evaluate(candidate, leader=leader, pretable=table)
    cond = {c["id"]: c["ok"] for c in result["conditions"]}[pa.COND_REACTION_WINDOW]
    assert cond is False
    assert result["status"] == pa.STATUS_NO_SIGNAL


@pytest.mark.parametrize("amount,expected", [(19999999.0, False), (20000000.0, True)])
def test_liquidity_boundary_is_inclusive_ge(amount, expected):
    leader = _leader()
    table = _pretable()
    candidate = _candidate("600000", amount=amount)
    result = pa.evaluate(candidate, leader=leader, pretable=table)
    cond = {c["id"]: c["ok"] for c in result["conditions"]}[pa.COND_LIQUIDITY]
    assert cond is expected
    assert result["status"] == (pa.STATUS_SIGNAL if expected else pa.STATUS_NO_SIGNAL)


def test_liquidity_unavailable_when_amount_missing():
    leader = _leader()
    table = _pretable()
    candidate = _candidate("600000", amount=None)
    result = pa.evaluate(candidate, leader=leader, pretable=table)
    assert result["status"] == pa.STATUS_UNAVAILABLE
    assert "candidate_liquidity_missing" in result["reasons"]


def test_all_evidence_missing_is_unavailable():
    empty = {"code": "600001"}
    result = pa.evaluate(empty, leader=None, pretable=None)
    assert result["status"] == pa.STATUS_UNAVAILABLE
    for reason in (
        "attribute_missing", "leader_missing", "pretable_missing",
        "candidate_liquidity_missing",
    ):
        assert reason in result["reasons"]


# --------------------------------------------------------------------------- #
# 3) 盘前表纪律三条 —— 本策略的成败点
# --------------------------------------------------------------------------- #
def test_candidate_not_in_pretable_never_triggers_even_when_strong():
    """D0 表现极好（反应窗口、流动性都合格）但不在盘前表候选列表内 —— 必须
    不触发（no_signal），不是"补进表里再判"。"""
    leader = _leader()
    table = _pretable(candidates=("600000",))  # 盘前表只列了 600000
    strong_but_absent = _candidate(
        "600999", evaluation_time="093000", amount=1.0e9,  # 各项证据都很强
    )
    result = pa.evaluate(strong_but_absent, leader=leader, pretable=table)
    assert result["status"] == pa.STATUS_NO_SIGNAL
    assert {c["id"]: c["ok"] for c in result["conditions"]}[pa.COND_PRETABLE_MEMBERSHIP] is False


def test_pretable_entry_absent_for_leader_attribute_is_no_signal_not_unavailable():
    """盘前表存在，但完全没有这个(龙头,属性)条目——同样是明确的"没在表里"，
    不是数据缺口。"""
    leader = _leader(code="900001")  # 表里没有这个龙头
    table = _pretable(leader_code="900000")
    candidate = _candidate("600000")
    result = pa.evaluate(candidate, leader=leader, pretable=table)
    assert result["status"] == pa.STATUS_NO_SIGNAL
    assert {c["id"]: c["ok"] for c in result["conditions"]}[pa.COND_PRETABLE_MEMBERSHIP] is False


def test_build_pretable_ignores_d0_data():
    """把 D0（晚于 as_of）的数据混进 leader_records / member_records，输出必须
    与只用 D-1 数据构建的结果完全一致——build_pretable 只吃 as_of 及更早数据。"""
    as_of = "2026-06-02"
    d0 = "2026-06-03"
    leaders_clean = [{"code": "900000", "attribute": "X", "date": as_of}]
    members_clean = [
        {"code": "600000", "attribute": "X", "date": as_of,
         "is_st": False, "material_bad_news": False, "avg_turnover_20d": 5.0e7},
        {"code": "600001", "attribute": "X", "date": as_of,
         "is_st": False, "material_bad_news": False, "avg_turnover_20d": 5.0e7},
    ]
    # D0 混入：一个新龙头、一个新成分股、以及试图把本该被排除的 600002 在 D0
    # 那天"洗白"成高流动性——全部必须被 build_pretable 忽略。
    leaders_polluted = leaders_clean + [{"code": "900999", "attribute": "X", "date": d0}]
    members_polluted = members_clean + [
        {"code": "600999", "attribute": "X", "date": d0,
         "is_st": False, "material_bad_news": False, "avg_turnover_20d": 9.0e8},
        {"code": "600002", "attribute": "X", "date": d0,
         "is_st": False, "material_bad_news": False, "avg_turnover_20d": 9.0e8},
    ]

    clean = pa.build_pretable(leaders_clean, members_clean, as_of=as_of, generated_at="t0")
    polluted = pa.build_pretable(leaders_polluted, members_polluted, as_of=as_of, generated_at="t0")
    assert polluted == clean
    assert clean["entries"] == [
        {"leader_code": "900000", "attribute": "X",
         "candidates": ["600000", "600001"], "excluded": []}
    ]


def test_build_pretable_excludes_st_bad_news_and_illiquid_members():
    as_of = "2026-06-02"
    leaders = [{"code": "900000", "attribute": "X", "date": as_of}]
    members = [
        {"code": "600001", "attribute": "X", "date": as_of,
         "is_st": True, "avg_turnover_20d": 5.0e7},
        {"code": "600002", "attribute": "X", "date": as_of,
         "material_bad_news": True, "avg_turnover_20d": 5.0e7},
        {"code": "600003", "attribute": "X", "date": as_of,
         "avg_turnover_20d": 1.0},  # 流动性不足
        {"code": "600004", "attribute": "X", "date": as_of,
         "avg_turnover_20d": 5.0e7},  # 唯一合格
    ]
    table = pa.build_pretable(leaders, members, as_of=as_of)
    entry = table["entries"][0]
    assert entry["candidates"] == ["600004"]
    reasons = {e["code"]: e["reason"] for e in entry["excluded"]}
    assert reasons == {
        "600001": "is_st", "600002": "material_bad_news", "600003": "insufficient_liquidity",
    }


# --------------------------------------------------------------------------- #
# pick_confirmed_leader / evaluate_group
# --------------------------------------------------------------------------- #
def test_pick_confirmed_leader_selects_earliest_confirmed():
    peers = [
        _candidate("A", confirmed=True, confirmed_time="093500"),
        _candidate("B", confirmed=True, confirmed_time="093000"),
        _candidate("C", confirmed=False, confirmed_time=None),
    ]
    leader = pa.pick_confirmed_leader(peers)
    assert leader["code"] == "B"


def test_pick_confirmed_leader_returns_none_when_nobody_confirmed():
    peers = [_candidate("A", confirmed=False), _candidate("B", confirmed=None)]
    assert pa.pick_confirmed_leader(peers) is None


def test_evaluate_group_excludes_leader_from_candidates():
    table = _pretable(candidates=("600000",))
    peers = [
        _candidate("900000", confirmed=True, confirmed_time="093000"),
        _candidate("600000"),
    ]
    results = pa.evaluate_group(peers, pretable=table)
    assert "900000" not in {r["code"] for r in results}
    assert len(results) == 1


# --------------------------------------------------------------------------- #
# 2) NON-LIVE：消费端行为断言
# --------------------------------------------------------------------------- #
def test_unregistered_signal_is_downgraded_to_watch_by_decision_policy():
    import decision_policy
    import strategy_registry

    leader = _leader()
    table = _pretable()
    fired = [pa.evaluate(_candidate("600000"), leader=leader, pretable=table)]
    fired = [r for r in fired if r["status"] == pa.STATUS_SIGNAL]
    assert fired, "前置：必须真的有一个正向 S4 信号，否则本用例恒真"

    record = strategy_registry.live_record("preleader_arbitrage")
    assert record is None, "S4 不得出现在 strategy_registry 中"

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

    guidance = ra.position_guidance("preleader_arbitrage", 10.0, 12.0, 9.0, total_asset=100000.0)
    assert guidance["recommended_position_pct"] == 0.0
    assert guidance["recommended_amount"] == 0.0
    assert guidance["method"] == "research_only"
    assert guidance["gating_status"] == "unverified"


def test_pack_is_reported_as_ungated_research_hypothesis():
    import strategy_packs

    record = strategy_packs.registry_records()["preleader_arbitrage"]
    assert record["allowed_in_live_agent"] is False
    assert record["gate_decision"] == "not_gated"
    assert strategy_packs.load_packs()["preleader_arbitrage"]["score_hints"] == []


def test_strategy_registry_reports_not_allowed_in_live():
    import strategy_registry

    assert strategy_registry.is_allowed_in_live("preleader_arbitrage") is False


# --------------------------------------------------------------------------- #
# 回测适配层 —— event_record 映射 + 反事实 + fail-closed
# --------------------------------------------------------------------------- #
def _bt_event(code, *, date, board_height, seal, one_word, gap_pct_value, t1_close,
              sector="X", breadth=5.0, amount=3.0e7):
    prev, close = 10.0, 11.0
    t1_open = close * (1 + gap_pct_value / 100.0)
    bar = ({"t_open": close, "t_high": close, "t_low": close} if one_word
           else {"t_open": close, "t_high": close, "t_low": 10.78})
    return {
        "code": code, "name": "SYN", "date": date, "sector": sector,
        "t_prev_close": prev, "t_close": close,
        "t_volume": 1.0e6, "t_amount": amount,
        "t1_open": t1_open, "t1_close": t1_close,
        "t1_high": max(t1_open, t1_close), "t1_low": min(t1_open, t1_close),
        "t1_volume": 1.0e6, "t1_amount": amount,
        "first_seal": seal, "lianban": board_height, "is_st": False,
        "sector_limitup_count": breadth,
        **bar,
    }


def _bt_events_two_days():
    """两个交易日、同一 sector：D-1 建表，D0 用表判定。D-1 里 600900 是龙头
    (lianban 最高)，600200/600201 是候选(封板早于其余基线成员)。D0 沿用相同
    的候选池，600200 在 D0 龙头确认后很快跟着封板（应命中），600201 一字板
    买不进（用于反事实），其余基线候选封板晚（不够格）。"""
    d1, d0 = "2026-06-02", "2026-06-03"
    events = []
    events.append(_bt_event(
        "600900", date=d1, board_height=8.0, seal="094500",
        one_word=False, gap_pct_value=0.0, t1_close=11.0,
    ))
    for i in range(6):
        events.append(_bt_event(
            f"60010{i}", date=d1, board_height=1.0, seal=f"09{40 + i:02d}00",
            one_word=False, gap_pct_value=1.0, t1_close=11.0,
        ))
    events.append(_bt_event(
        "600200", date=d1, board_height=1.0, seal="093000",
        one_word=False, gap_pct_value=6.0, t1_close=11.0,
    ))
    events.append(_bt_event(
        "600201", date=d1, board_height=1.0, seal="093100",
        one_word=False, gap_pct_value=7.0, t1_close=11.0,
    ))
    # D0：600900 再度是最高连板并率先确认；600200 紧随其后封板(可成交，赢家A)；
    # 600201 一字板买不进(赢家B，用于反事实)；其余基线候选封板晚。
    events.append(_bt_event(
        "600900", date=d0, board_height=9.0, seal="093000",
        one_word=False, gap_pct_value=0.0, t1_close=11.0,
    ))
    for i in range(6):
        events.append(_bt_event(
            f"60010{i}", date=d0, board_height=1.0, seal=f"10{i:02d}00",
            one_word=False, gap_pct_value=1.0, t1_close=11.0,
        ))
    events.append(_bt_event(
        "600200", date=d0, board_height=1.0, seal="093500",
        one_word=False, gap_pct_value=6.0, t1_close=11.66,
    ))
    events.append(_bt_event(
        "600201", date=d0, board_height=1.0, seal="093600",
        one_word=True, gap_pct_value=7.0, t1_close=13.2,
    ))
    return events


def test_counterfactual_disabling_constraints_inflates_returns():
    report = bt.counterfactual(_bt_events_two_days(), hold_mode="board_overnight")
    on, off = report["with_constraints"], report["without_constraints"]

    assert on["signal_count"] >= 1, on["signal_summary"]
    assert on["filled_count"] == on["signal_count"] - 1 or on["filled_count"] >= 1, on
    assert off["filled_count"] >= on["filled_count"]
    assert report["excluded_by_constraints"] >= 1
    assert on["returns"]["mean"] is not None and off["returns"]["mean"] is not None
    assert off["returns"]["mean"] > on["returns"]["mean"]
    assert report["constraints_bite"] is True


def test_counterfactual_reports_no_bite_on_empty_sample():
    report = bt.counterfactual([], hold_mode="board_overnight")
    assert report["with_constraints"]["filled_count"] == 0
    assert report["constraints_bite"] is False
    assert report["mean_return_inflation"] is None


def test_backtest_fails_closed_when_attribute_missing():
    """事件表没有可靠的属性映射来源时（本适配器用 sector 兜底但缺失时），
    候选记录必须 unavailable 而不是静默当作没有信号。"""
    events = _bt_events_two_days()
    for event in events:
        event["sector"] = None
    report = bt.run(events, hold_mode="board_overnight")
    assert report["signal_count"] == 0
    counts = report["signal_summary"]["status_counts"]
    assert counts[pa.STATUS_UNAVAILABLE] == report["universe_count"] > 0
    assert "attribute_missing" in report["signal_summary"]["unavailable_reasons"]


def test_event_record_maps_s4_fields_directly():
    record = bt.event_record(_bt_event(
        "600100", date="2026-06-03", board_height=3.0, seal="093300",
        one_word=False, gap_pct_value=2.0, t1_close=11.0,
    ))
    assert record["attribute"] == "X"
    assert record["evaluation_time"] == "093300"
    assert record["amount"] == pytest.approx(3.0e7)
