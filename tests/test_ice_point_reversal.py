"""S6 冰点反转（IcePointReversal）— 纯逻辑单测，不触网。

覆盖六类断言（照 tests/test_reverse_volume.py 的组织方式）：
1) 合取纪律（本策略要害）：四项全部满足才触发；逐项去掉任一项都不触发；
   单独满足"冰点"（S_t-1<20）而其余三项不满足时绝不触发；
2) sentiment_score 复用断言：S_t/ΔS 取自该模块（monkeypatch 桩验证），
   config 缺失/S_t 不可得时是 unavailable 而非 no_signal；
3) 反事实：关掉 P5 成交约束后回测收益虚高 + 零样本 bite=False；
4) NON-LIVE 消费端**行为**断言：未注册状态下正向信号被降级为 watch、仓位归零；
5) 四个条件各自边界 + 全缺失 → unavailable；
6) 回测适配层：真实事件表结构性不携带证据字段时必须 fail-closed 成零命中。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

import ice_point_reversal as ipr
import sentiment_score as ss

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "chanlun-backtest" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_SPEC = importlib.util.spec_from_file_location(
    "daban_bt_ice_point_reversal", SCRIPTS / "daban_bt_ice_point_reversal.py"
)
bt = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bt)


# --------------------------------------------------------------------------- #
# 合成 fixture —— 一份完整可用的 sentiment_score 配置（结构同 config/scoring.yaml
# 的 sentiment_score 节，数值与其保持一致，避免测试用一套虚构阈值造成误导）。
# --------------------------------------------------------------------------- #
def _sentiment_cfg():
    return {
        "quantile_window": 120,
        "min_history": 180,
        "min_available_weight": 0.60,
        "components": {
            "premium": {"field": "limit_premium_close", "weight": 0.20, "invert": False},
            "limit_count": {"field": "limit_count", "weight": 0.15, "invert": False},
            "adr": {"field": "adr", "weight": 0.15, "invert": False},
            "break_rate": {"field": "break_rate", "weight": 0.15, "invert": True},
            "max_board": {"field": "max_board", "weight": 0.15, "invert": False},
            "board4plus": {"field": "board4plus", "weight": 0.10, "invert": False},
            "leader_health": {"field": "leader_damage", "weight": 0.10, "invert": False},
        },
        "bands": [
            {"name": "冰点", "min": 0}, {"name": "修复", "min": 20},
            {"name": "发酵", "min": 40}, {"name": "加速", "min": 60},
            {"name": "极热", "min": 80},
        ],
        "ice_confirm": {"prev_score_max": 20, "delta_min": 10, "sector_breadth_min": 3},
    }


def _score_ok(previous_score=15.0, delta=12.0):
    """一份"已经算好"的 sentiment_score 输出（monkeypatch 用），status=ok。"""
    return {
        "schema": ss.SCHEMA, "status": "ok", "calibrated": False,
        "trading_date": "2026-06-03", "score": previous_score + delta,
        "band": "修复", "delta": delta, "delta_squared": None,
        "previous_score": previous_score, "available_weight": 1.0,
        "unavailable_components": [], "components": {},
    }


def _record(code="600000", date="2026-06-03"):
    return {"code": code, "date": date}


def _full_state():
    """四项全部满足的 market_state（配合 monkeypatch 桩固定 S_t/ΔS_t）。"""
    return {"sentiment_series": [{"trading_date": "2026-06-03"}],
            "leader_confirm": True, "sector_breadth_top": 3}


def _assert_only_condition_varies(result, cond_id, expected_ok):
    by_id = {c["id"]: c["ok"] for c in result["conditions"]}
    assert by_id[cond_id] is expected_ok


# --------------------------------------------------------------------------- #
# 1) 合取纪律 —— 本策略的要害
# --------------------------------------------------------------------------- #
def test_full_conjunction_produces_signal(monkeypatch):
    monkeypatch.setattr(ipr.ss, "compute_sentiment_score", lambda series, config=None: _score_ok())
    result = ipr.evaluate(_record(), market_state=_full_state(), cfg=_sentiment_cfg())
    assert result["status"] == ipr.STATUS_SIGNAL, result
    assert all(c["ok"] is True for c in result["conditions"])


@pytest.mark.parametrize("drop_field", [
    ipr.COND_PREV_SCORE_EXTREME, ipr.COND_DELTA_IMPROVING,
    ipr.COND_LEADER_CONFIRM, ipr.COND_SECTOR_BREADTH,
])
def test_dropping_any_single_condition_prevents_signal(monkeypatch, drop_field):
    """四项全满足才触发；逐项去掉任一项都不得触发——不是"接近就行"。"""
    score = _score_ok()
    state = _full_state()
    if drop_field == ipr.COND_PREV_SCORE_EXTREME:
        score["previous_score"] = 25.0  # 不再是冰点
    elif drop_field == ipr.COND_DELTA_IMPROVING:
        score["delta"] = 5.0  # 改善不够
    elif drop_field == ipr.COND_LEADER_CONFIRM:
        state["leader_confirm"] = False
    elif drop_field == ipr.COND_SECTOR_BREADTH:
        state["sector_breadth_top"] = 1

    monkeypatch.setattr(ipr.ss, "compute_sentiment_score", lambda series, config=None: score)
    result = ipr.evaluate(_record(), market_state=state, cfg=_sentiment_cfg())
    _assert_only_condition_varies(result, drop_field, False)
    assert result["status"] == ipr.STATUS_NO_SIGNAL, result


def test_ice_point_alone_does_not_trigger_signal(monkeypatch):
    """本策略最容易被误用的形态：只满足"极端冰点"（S_t-1<20），其余三项都不
    满足——绝不能触发。原书教训：机械在冰点抄底两周大赚后连续大面回撤30%+，
    否极并不必然泰来。"""
    score = _score_ok(previous_score=5.0, delta=1.0)  # 冰点确实很深，但改善幅度不够
    state = {"sentiment_series": [{"trading_date": "2026-06-03"}],
             "leader_confirm": False, "sector_breadth_top": 0}
    monkeypatch.setattr(ipr.ss, "compute_sentiment_score", lambda series, config=None: score)
    result = ipr.evaluate(_record(), market_state=state, cfg=_sentiment_cfg())
    assert result["status"] == ipr.STATUS_NO_SIGNAL, result
    by_id = {c["id"]: c["ok"] for c in result["conditions"]}
    assert by_id[ipr.COND_PREV_SCORE_EXTREME] is True   # 冰点条件本身满足
    assert by_id[ipr.COND_DELTA_IMPROVING] is False
    assert by_id[ipr.COND_LEADER_CONFIRM] is False
    assert by_id[ipr.COND_SECTOR_BREADTH] is False


# --------------------------------------------------------------------------- #
# 5) 四个条件各自边界 + 全缺失 → unavailable
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("prev,expected", [(20.0, False), (19.999, True)])
def test_prev_score_boundary_is_strict_less_than(monkeypatch, prev, expected):
    score = _score_ok(previous_score=prev, delta=12.0)
    monkeypatch.setattr(ipr.ss, "compute_sentiment_score", lambda series, config=None: score)
    result = ipr.evaluate(_record(), market_state=_full_state(), cfg=_sentiment_cfg())
    _assert_only_condition_varies(result, ipr.COND_PREV_SCORE_EXTREME, expected)
    assert result["status"] == (ipr.STATUS_SIGNAL if expected else ipr.STATUS_NO_SIGNAL)


@pytest.mark.parametrize("delta,expected", [(10.0, False), (10.001, True)])
def test_delta_boundary_is_strict_greater_than(monkeypatch, delta, expected):
    score = _score_ok(previous_score=15.0, delta=delta)
    monkeypatch.setattr(ipr.ss, "compute_sentiment_score", lambda series, config=None: score)
    result = ipr.evaluate(_record(), market_state=_full_state(), cfg=_sentiment_cfg())
    _assert_only_condition_varies(result, ipr.COND_DELTA_IMPROVING, expected)
    assert result["status"] == (ipr.STATUS_SIGNAL if expected else ipr.STATUS_NO_SIGNAL)


@pytest.mark.parametrize("breadth,expected", [(2.999, False), (3.0, True)])
def test_sector_breadth_boundary_is_inclusive_ge(monkeypatch, breadth, expected):
    monkeypatch.setattr(ipr.ss, "compute_sentiment_score", lambda series, config=None: _score_ok())
    state = _full_state()
    state["sector_breadth_top"] = breadth
    result = ipr.evaluate(_record(), market_state=state, cfg=_sentiment_cfg())
    _assert_only_condition_varies(result, ipr.COND_SECTOR_BREADTH, expected)
    assert result["status"] == (ipr.STATUS_SIGNAL if expected else ipr.STATUS_NO_SIGNAL)


@pytest.mark.parametrize("leader_confirm,expected", [(False, False), (True, True)])
def test_leader_confirm_reads_boolean_directly(monkeypatch, leader_confirm, expected):
    monkeypatch.setattr(ipr.ss, "compute_sentiment_score", lambda series, config=None: _score_ok())
    state = _full_state()
    state["leader_confirm"] = leader_confirm
    result = ipr.evaluate(_record(), market_state=state, cfg=_sentiment_cfg())
    _assert_only_condition_varies(result, ipr.COND_LEADER_CONFIRM, expected)
    assert result["status"] == (ipr.STATUS_SIGNAL if expected else ipr.STATUS_NO_SIGNAL)


def test_all_evidence_missing_is_unavailable():
    result = ipr.evaluate(_record(), market_state=None, cfg=_sentiment_cfg())
    assert result["status"] == ipr.STATUS_UNAVAILABLE
    for reason in ("sentiment_score_unavailable", "leader_confirm_missing", "sector_breadth_missing"):
        assert reason in result["reasons"], reason


def test_leader_confirm_missing_is_unavailable(monkeypatch):
    monkeypatch.setattr(ipr.ss, "compute_sentiment_score", lambda series, config=None: _score_ok())
    state = _full_state()
    state["leader_confirm"] = None
    result = ipr.evaluate(_record(), market_state=state, cfg=_sentiment_cfg())
    assert result["status"] == ipr.STATUS_UNAVAILABLE
    assert "leader_confirm_missing" in result["reasons"]


def test_sector_breadth_missing_is_unavailable(monkeypatch):
    monkeypatch.setattr(ipr.ss, "compute_sentiment_score", lambda series, config=None: _score_ok())
    state = _full_state()
    state["sector_breadth_top"] = None
    result = ipr.evaluate(_record(), market_state=state, cfg=_sentiment_cfg())
    assert result["status"] == ipr.STATUS_UNAVAILABLE
    assert "sector_breadth_missing" in result["reasons"]


# --------------------------------------------------------------------------- #
# 2) sentiment_score 复用断言 —— S_t/ΔS 必须取自该模块，缺一律 unavailable
# --------------------------------------------------------------------------- #
def test_prev_and_delta_come_from_sentiment_score_stub(monkeypatch):
    """把 sentiment_score.compute_sentiment_score 换成返回固定虚构值的桩，
    结果必须原样反映桩返回的数字——证明本模块没有自己重算 S_t/ΔS。"""
    fake_score = {"status": "ok", "previous_score": 3.5, "delta": 42.0}
    monkeypatch.setattr(ipr.ss, "compute_sentiment_score", lambda series, config=None: fake_score)
    result = ipr.evaluate(_record(), market_state=_full_state(), cfg=_sentiment_cfg())
    by_id = {c["id"]: c["detail"] for c in result["conditions"]}
    assert "3.5000" in by_id[ipr.COND_PREV_SCORE_EXTREME]
    assert "42.0000" in by_id[ipr.COND_DELTA_IMPROVING]
    assert result["status"] == ipr.STATUS_SIGNAL


def test_sentiment_score_unavailable_status_propagates_as_unavailable_not_no_signal(monkeypatch):
    """S_t 不可得（预热不足等）时必须 fail-closed 成 unavailable，绝不能被
    折叠成"不是冰点"这个 no_signal 负面结论——那是把没数据包装成已验证的负结果。"""
    monkeypatch.setattr(
        ipr.ss, "compute_sentiment_score",
        lambda series, config=None: {"status": "unavailable", "reason": "insufficient_history"},
    )
    result = ipr.evaluate(_record(), market_state=_full_state(), cfg=_sentiment_cfg())
    assert result["status"] == ipr.STATUS_UNAVAILABLE
    assert "sentiment_score_unavailable" in result["reasons"]


def test_sentiment_score_config_missing_is_unavailable_not_no_signal(monkeypatch):
    """config/scoring.yaml 的 sentiment_score 节缺失时（ss.load_config()->None）
    整体 unavailable，且四个条件里的 prev/delta/breadth 均不可判定。"""
    monkeypatch.setattr(ipr.ss, "load_config", lambda: None)
    result = ipr.evaluate(_record(), market_state=_full_state(), cfg=None)
    assert result["status"] == ipr.STATUS_UNAVAILABLE
    assert "sentiment_score_config_missing" in result["reasons"]
    by_id = {c["id"]: c["ok"] for c in result["conditions"]}
    assert by_id[ipr.COND_PREV_SCORE_EXTREME] is None
    assert by_id[ipr.COND_DELTA_IMPROVING] is None
    assert by_id[ipr.COND_SECTOR_BREADTH] is None


def test_empty_sentiment_series_is_unavailable_via_real_module():
    """不打桩的真实路径：空的情绪序列喂给真实的 sentiment_score.compute_
    sentiment_score，必须 unavailable（空样本），而不是 no_signal。"""
    state = {"sentiment_series": [], "leader_confirm": True, "sector_breadth_top": 5}
    result = ipr.evaluate(_record(), market_state=state, cfg=_sentiment_cfg())
    assert result["status"] == ipr.STATUS_UNAVAILABLE
    assert "sentiment_score_unavailable" in result["reasons"]


# --------------------------------------------------------------------------- #
# 4) NON-LIVE：消费端行为断言（不是配置字段断言）
# --------------------------------------------------------------------------- #
def test_unregistered_signal_is_downgraded_to_watch_by_decision_policy(monkeypatch):
    """构造一个 S6 正向信号，断言**消费端**把 buy 降为 watch、仓位倍率归零。"""
    import decision_policy
    import strategy_registry

    monkeypatch.setattr(ipr.ss, "compute_sentiment_score", lambda series, config=None: _score_ok())
    fired = [ipr.evaluate(_record(), market_state=_full_state(), cfg=_sentiment_cfg())]
    fired = [r for r in fired if r["status"] == ipr.STATUS_SIGNAL]
    assert fired, "前置：必须真的有一个正向 S6 信号，否则本用例恒真"

    record = strategy_registry.live_record("ice_point_reversal")
    assert record is None, "S6 不得出现在 strategy_registry 中"

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

    guidance = ra.position_guidance("ice_point_reversal", 10.0, 12.0, 9.0, total_asset=100000.0)
    assert guidance["recommended_position_pct"] == 0.0
    assert guidance["recommended_amount"] == 0.0
    assert guidance["method"] == "research_only"
    assert guidance["gating_status"] == "unverified"


def test_pack_is_reported_as_ungated_research_hypothesis():
    import strategy_packs

    record = strategy_packs.registry_records()["ice_point_reversal"]
    assert record["allowed_in_live_agent"] is False
    assert record["gate_decision"] == "not_gated"
    assert strategy_packs.load_packs()["ice_point_reversal"]["score_hints"] == []


def test_strategy_registry_reports_not_allowed_in_live():
    import strategy_registry

    assert strategy_registry.is_allowed_in_live("ice_point_reversal") is False


def test_no_register_gate_result_call_in_new_module_source():
    """新代码零 register_gate_result 调用——本轮严禁注册。"""
    import inspect

    source = inspect.getsource(ipr) + inspect.getsource(bt)
    assert "register_gate_result" not in source


# --------------------------------------------------------------------------- #
# 回测适配层 —— event_record 的诚实缺口 + 反事实 + fail-closed
# --------------------------------------------------------------------------- #
def test_event_record_carries_only_code_and_date():
    """事件表(v3/v4)是单日涨停快照结构，S6 的四类证据都是市场级/跨交易日证据，
    结构上不携带——event_record 只产出 code/date，市场级证据走 market_state。"""
    event = {"code": "600100", "date": "2026-06-03", "lianban": 3, "t_close": 11.0}
    record = bt.event_record(event)
    assert record == {"code": "600100", "date": "2026-06-03"}


def _bt_event(code, *, date="2026-06-03", one_word, t1_close, gap_pct_value=1.0):
    prev, close = 10.0, 11.0
    t1_open = close * (1 + gap_pct_value / 100.0)
    bar = ({"t_open": close, "t_high": close, "t_low": close} if one_word
           else {"t_open": close, "t_high": close, "t_low": 10.78})
    return {
        "code": code, "name": "SYN", "date": date, "sector": "X",
        "t_prev_close": prev, "t_close": close,
        "t_volume": 1.0e6, "t_amount": 3.0e7,
        "t1_open": t1_open, "t1_close": t1_close,
        "t1_high": max(t1_open, t1_close), "t1_low": min(t1_open, t1_close),
        "t1_volume": 1.0e6, "t1_amount": 3.0e7,
        "first_seal": "093000", "lianban": 1, "is_st": False,
        "sector_limitup_count": 3,
        **bar,
    }


def _bt_events():
    """两个"赢家"事件：A 可成交（次日温和+6.7%），B 一字板买不进（次日暴涨+20%），
    用于反事实证明 P5 约束真的在咬。"""
    return [
        _bt_event("600200", one_word=False, gap_pct_value=6.0, t1_close=11.66),
        _bt_event("600201", one_word=True, gap_pct_value=7.0, t1_close=13.2),
    ]


def test_counterfactual_disabling_constraints_inflates_returns(monkeypatch):
    monkeypatch.setattr(bt.ipr, "evaluate", lambda record, **kw: {
        "schema": ipr.SCHEMA, "code": record["code"], "date": record["date"],
        "status": ipr.STATUS_SIGNAL, "conditions": [], "reasons": [], "degraded": [],
        "influences_live_ranking": False, "note": "stub",
    })
    report = bt.counterfactual(_bt_events(), hold_mode="board_overnight")
    on, off = report["with_constraints"], report["without_constraints"]

    # 样本非空断言 —— 空集下任何"约束生效"的结论都是恒真的假绿。
    assert on["signal_count"] == 2, on["signal_summary"]
    assert on["filled_count"] == 1, "约束打开时一字板必须被剔除，只剩可成交的赢家A"
    assert off["filled_count"] == 2, "约束关闭时一字板会被算进收益"

    assert report["excluded_by_constraints"] == 1
    assert on["returns"]["mean"] is not None and off["returns"]["mean"] is not None
    assert report["mean_return_inflation"] > 0.05, report
    assert off["returns"]["mean"] > on["returns"]["mean"] * 2
    assert report["constraints_bite"] is True


def test_counterfactual_reports_no_bite_on_empty_sample():
    """零样本时不得报"约束在咬" —— 空集恒真是假绿的经典来源。"""
    report = bt.counterfactual([], hold_mode="board_overnight")
    assert report["with_constraints"]["filled_count"] == 0
    assert report["constraints_bite"] is False
    assert report["mean_return_inflation"] is None


def test_backtest_fails_closed_on_real_structure_with_zero_hits():
    """不打补丁的真实路径：event_record 对本策略的全部证据结构性留空
    （无 sentiment-table/leader-confirm 输入），命中数如实为 0，且
    unavailable_reasons 能一眼看出缺哪些字段。"""
    report = bt.run(_bt_events(), hold_mode="board_overnight")
    assert report["signal_count"] == 0
    counts = report["signal_summary"]["status_counts"]
    assert counts[ipr.STATUS_UNAVAILABLE] == report["universe_count"] > 0
    reasons = report["signal_summary"]["unavailable_reasons"]
    for key in ("sentiment_score_unavailable", "leader_confirm_missing", "sector_breadth_missing"):
        assert reasons.get(key, 0) > 0, key


def test_build_market_state_slices_by_as_of_date_and_reads_sector_breadth():
    records = [
        {"trading_date": "2026-06-01", "sector_breadth_top": 1},
        {"trading_date": "2026-06-02", "sector_breadth_top": 2},
        {"trading_date": "2026-06-03", "sector_breadth_top": 5},  # 事件日之后，须被截断掉
    ]
    state = bt.build_market_state(records, as_of_date="2026-06-02", leader_confirm=True)
    dates = [r["trading_date"] for r in state["sentiment_series"]]
    assert dates == ["2026-06-01", "2026-06-02"]
    assert state["sector_breadth_top"] == 2
    assert state["leader_confirm"] is True
