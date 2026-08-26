"""S5 反量龙回头（ReverseVolume）— 纯逻辑单测，不触网。

覆盖六类断言（照 tests/test_assist_arbitrage.py 的组织方式）：
1) 反事实：关掉 P5 成交约束后回测收益显著虚高 —— 证明约束真的在咬（防假绿主证据）；
2) NON-LIVE 消费端**行为**断言：未注册状态下正向信号被降级为 watch、仓位归零；
3) 反未来函数（本策略要害）：max_directional_minute_volume 的 until_time 截断——
   喂入场之后的分钟数据不得改变入场之前的历史极值；
4) minute_derived 复用：断言分钟量峰值来自 minute_derived 的归一化输出，不自行
   解析原始供应商字段；
5) 七个入场条件各自边界 + 全缺失 → unavailable；二次确认条件同理；
6) 回测适配层：真实事件表结构性不携带证据字段时必须 fail-closed 成零命中。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

import minute_derived as md
import reverse_volume as rv

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "chanlun-backtest" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_SPEC = importlib.util.spec_from_file_location(
    "daban_bt_reverse_volume", SCRIPTS / "daban_bt_reverse_volume.py"
)
bt = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bt)


# --------------------------------------------------------------------------- #
# 合成 fixture —— 信号层：一条满足全部七条件的记录 + 稳定情绪状态
# --------------------------------------------------------------------------- #
def _signal_record(code="600000", date="2026-06-03", **overrides):
    record = {
        "code": code,
        "date": date,
        "was_prior_period_top_leader": True,
        "drawdown_pct": 0.30,
        "volatility_contraction_ratio": 0.5,
        "volume_percentile_20d": 0.20,
        "max_up_minute_volume": 150000.0,
        "max_down_minute_volume_prior": 100000.0,   # ratio = 1.5 >= 1.3
        "pullback_max_down_minute_volume": 50000.0,  # < 100000 -> shrinking
    }
    record.update(overrides)
    return record


_STABLE_MARKET = {"available": True, "deteriorating": False}


def _assert_only_condition_varies(result, cond_id, expected_ok):
    by_id = {c["id"]: c["ok"] for c in result["conditions"]}
    assert by_id[cond_id] is expected_ok


# --------------------------------------------------------------------------- #
# 5) 七个入场条件边界 + 全缺失 → unavailable
# --------------------------------------------------------------------------- #
def test_full_evidence_produces_signal():
    result = rv.evaluate(_signal_record(), market_state=_STABLE_MARKET)
    assert result["status"] == rv.STATUS_SIGNAL, result
    assert result["suggested_entry_position_pct"] == pytest.approx(0.10)


@pytest.mark.parametrize("was_leader,expected", [(False, False), (True, True)])
def test_prior_leader_condition_reads_boolean_directly(was_leader, expected):
    record = _signal_record(was_prior_period_top_leader=was_leader)
    result = rv.evaluate(record, market_state=_STABLE_MARKET)
    _assert_only_condition_varies(result, rv.COND_PRIOR_LEADER, expected)
    assert result["status"] == (rv.STATUS_SIGNAL if expected else rv.STATUS_NO_SIGNAL)


def test_prior_leader_missing_is_unavailable():
    record = _signal_record(was_prior_period_top_leader=None)
    result = rv.evaluate(record, market_state=_STABLE_MARKET)
    assert result["status"] == rv.STATUS_UNAVAILABLE
    assert "prior_period_leader_status_missing" in result["reasons"]


@pytest.mark.parametrize("drawdown,expected", [
    (0.2499, False), (0.25, True), (0.40, True), (0.4001, False),
])
def test_drawdown_boundary_is_inclusive_range(drawdown, expected):
    record = _signal_record(drawdown_pct=drawdown)
    result = rv.evaluate(record, market_state=_STABLE_MARKET)
    _assert_only_condition_varies(result, rv.COND_DRAWDOWN, expected)
    assert result["status"] == (rv.STATUS_SIGNAL if expected else rv.STATUS_NO_SIGNAL)


def test_drawdown_missing_is_unavailable():
    record = _signal_record(drawdown_pct=None)
    result = rv.evaluate(record, market_state=_STABLE_MARKET)
    assert result["status"] == rv.STATUS_UNAVAILABLE
    assert "drawdown_pct_missing" in result["reasons"]


@pytest.mark.parametrize("deteriorating,expected", [(True, False), (False, True)])
def test_sentiment_condition_boundary(deteriorating, expected):
    record = _signal_record()
    market_state = {"available": True, "deteriorating": deteriorating}
    result = rv.evaluate(record, market_state=market_state)
    _assert_only_condition_varies(result, rv.COND_SENTIMENT_STABLE, expected)
    assert result["status"] == (rv.STATUS_SIGNAL if expected else rv.STATUS_NO_SIGNAL)


@pytest.mark.parametrize("market_state", [None, {}, {"available": False}, {"available": True}])
def test_sentiment_missing_or_unavailable_is_fail_closed(market_state):
    result = rv.evaluate(_signal_record(), market_state=market_state)
    assert result["status"] == rv.STATUS_UNAVAILABLE
    assert "market_sentiment_unavailable" in result["reasons"]


@pytest.mark.parametrize("ratio,expected", [(0.6001, False), (0.6, True)])
def test_volatility_contraction_boundary_is_inclusive_le(ratio, expected):
    record = _signal_record(volatility_contraction_ratio=ratio)
    result = rv.evaluate(record, market_state=_STABLE_MARKET)
    _assert_only_condition_varies(result, rv.COND_VOLATILITY_CONTRACTION, expected)
    assert result["status"] == (rv.STATUS_SIGNAL if expected else rv.STATUS_NO_SIGNAL)


def test_volatility_contraction_missing_is_unavailable():
    record = _signal_record(volatility_contraction_ratio=None)
    result = rv.evaluate(record, market_state=_STABLE_MARKET)
    assert result["status"] == rv.STATUS_UNAVAILABLE
    assert "volatility_contraction_ratio_missing" in result["reasons"]


@pytest.mark.parametrize("pct,expected", [(0.3001, False), (0.30, True)])
def test_volume_low_percentile_boundary_is_inclusive_le(pct, expected):
    record = _signal_record(volume_percentile_20d=pct)
    result = rv.evaluate(record, market_state=_STABLE_MARKET)
    _assert_only_condition_varies(result, rv.COND_VOLUME_LOW_PERCENTILE, expected)
    assert result["status"] == (rv.STATUS_SIGNAL if expected else rv.STATUS_NO_SIGNAL)


def test_volume_low_percentile_missing_is_unavailable():
    record = _signal_record(volume_percentile_20d=None)
    result = rv.evaluate(record, market_state=_STABLE_MARKET)
    assert result["status"] == rv.STATUS_UNAVAILABLE
    assert "volume_percentile_20d_missing" in result["reasons"]


@pytest.mark.parametrize("up,expected", [(129.9, False), (130.0, True)])
def test_reversal_volume_boundary_is_inclusive_ge(up, expected):
    record = _signal_record(max_up_minute_volume=up, max_down_minute_volume_prior=100.0,
                            pullback_max_down_minute_volume=10.0)
    result = rv.evaluate(record, market_state=_STABLE_MARKET)
    _assert_only_condition_varies(result, rv.COND_REVERSAL_VOLUME, expected)
    assert result["status"] == (rv.STATUS_SIGNAL if expected else rv.STATUS_NO_SIGNAL)


def test_reversal_volume_missing_is_unavailable():
    record = _signal_record(max_up_minute_volume=None)
    result = rv.evaluate(record, market_state=_STABLE_MARKET)
    assert result["status"] == rv.STATUS_UNAVAILABLE
    assert "reversal_volume_evidence_missing" in result["reasons"]


def test_reversal_volume_non_positive_baseline_is_unavailable():
    record = _signal_record(max_down_minute_volume_prior=0.0)
    result = rv.evaluate(record, market_state=_STABLE_MARKET)
    assert result["status"] == rv.STATUS_UNAVAILABLE
    assert "max_down_minute_volume_prior_non_positive" in result["reasons"]


@pytest.mark.parametrize("pullback,expected", [(100.0, False), (99.999, True)])
def test_pullback_shrink_boundary_is_strict_less_than(pullback, expected):
    record = _signal_record(max_down_minute_volume_prior=100.0,
                            pullback_max_down_minute_volume=pullback,
                            max_up_minute_volume=150.0)
    result = rv.evaluate(record, market_state=_STABLE_MARKET)
    _assert_only_condition_varies(result, rv.COND_PULLBACK_SHRINK, expected)
    assert result["status"] == (rv.STATUS_SIGNAL if expected else rv.STATUS_NO_SIGNAL)


def test_pullback_shrink_missing_is_unavailable():
    record = _signal_record(pullback_max_down_minute_volume=None)
    result = rv.evaluate(record, market_state=_STABLE_MARKET)
    assert result["status"] == rv.STATUS_UNAVAILABLE
    assert "pullback_volume_evidence_missing" in result["reasons"]


def test_all_evidence_missing_is_unavailable():
    result = rv.evaluate({"code": "600000", "date": "2026-06-03"}, market_state=None)
    assert result["status"] == rv.STATUS_UNAVAILABLE
    for reason in (
        "prior_period_leader_status_missing", "drawdown_pct_missing",
        "market_sentiment_unavailable", "volatility_contraction_ratio_missing",
        "volume_percentile_20d_missing", "reversal_volume_evidence_missing",
        "pullback_volume_evidence_missing",
    ):
        assert reason in result["reasons"], reason


# --------------------------------------------------------------------------- #
# 二次确认（加仓）—— 独立于入场七条件
# --------------------------------------------------------------------------- #
def _second_record(**overrides):
    record = {"code": "600000", "date": "2026-06-03",
              "second_max_up_minute_volume": 150.0,
              "pullback_max_down_minute_volume": 100.0,  # ratio=1.5 >= 1.5
              "breakout_above_balance_zone": True}
    record.update(overrides)
    return record


def test_second_confirmation_full_evidence_produces_signal():
    result = rv.second_confirmation(_second_record())
    assert result["status"] == rv.STATUS_SIGNAL
    assert result["suggested_add_position_pct_min"] == pytest.approx(0.20)
    assert result["suggested_add_position_pct_max"] == pytest.approx(0.30)


@pytest.mark.parametrize("up,expected", [(149.9, False), (150.0, True)])
def test_second_confirmation_ratio_boundary_is_inclusive_ge(up, expected):
    result = rv.second_confirmation(_second_record(second_max_up_minute_volume=up))
    by_id = {c["id"]: c["ok"] for c in result["conditions"]}
    assert by_id[rv.COND2_RATIO] is expected
    assert result["status"] == (rv.STATUS_SIGNAL if expected else rv.STATUS_NO_SIGNAL)


def test_second_confirmation_ratio_missing_is_unavailable():
    result = rv.second_confirmation(_second_record(second_max_up_minute_volume=None))
    assert result["status"] == rv.STATUS_UNAVAILABLE
    assert "second_confirmation_volume_evidence_missing" in result["reasons"]


def test_second_confirmation_non_positive_denominator_is_unavailable():
    result = rv.second_confirmation(_second_record(pullback_max_down_minute_volume=0.0))
    assert result["status"] == rv.STATUS_UNAVAILABLE
    assert "pullback_max_down_minute_volume_non_positive" in result["reasons"]


@pytest.mark.parametrize("breakout,expected", [(False, False), (True, True)])
def test_second_confirmation_breakout_boundary(breakout, expected):
    result = rv.second_confirmation(_second_record(breakout_above_balance_zone=breakout))
    by_id = {c["id"]: c["ok"] for c in result["conditions"]}
    assert by_id[rv.COND2_BREAKOUT] is expected
    assert result["status"] == (rv.STATUS_SIGNAL if expected else rv.STATUS_NO_SIGNAL)


def test_second_confirmation_breakout_missing_is_unavailable():
    result = rv.second_confirmation(_second_record(breakout_above_balance_zone=None))
    assert result["status"] == rv.STATUS_UNAVAILABLE
    assert "breakout_above_balance_zone_missing" in result["reasons"]


# --------------------------------------------------------------------------- #
# 3+4) max_directional_minute_volume —— 反未来函数 + minute_derived 复用
# --------------------------------------------------------------------------- #
def _tencent_rows(ticks):
    """ticks: [(time, price, cum_volume_hand, cum_amount)] -> 腾讯原始行。"""
    return [
        {"time": t, "price": p, "cum_volume": cv, "cum_amount": ca}
        for t, p, cv, ca in ticks
    ]


def test_max_directional_minute_volume_tencent_classifies_up_and_down():
    # 价格路径：09:30=10.0(基准) 09:31涨->10.1(up,+100手) 09:32跌->10.0(down,+50手)
    # 09:33涨->10.2(up,+300手，全天最大上攻分钟量) 09:34跌->9.9(down,+400手，全天最大下跌分钟量)
    rows = _tencent_rows([
        ("0930", 10.0, 1000, 10000.0),
        ("0931", 10.1, 1100, 11100.0),  # +100 手 = +10000 股, up
        ("0932", 10.0, 1150, 11600.0),  # +50 手 = +5000 股, down
        ("0933", 10.2, 1450, 14700.0),  # +300 手 = +30000 股, up (最大上攻)
        ("0934", 9.9, 1850, 18400.0),   # +400 手 = +40000 股, down (最大下跌)
    ])
    up = rv.max_directional_minute_volume(rows, source=md.SOURCE_TENCENT_INTRADAY,
                                          direction=rv.DIRECTION_UP)
    down = rv.max_directional_minute_volume(rows, source=md.SOURCE_TENCENT_INTRADAY,
                                            direction=rv.DIRECTION_DOWN)
    assert up["availability"] == md.AVAILABLE
    assert up["value"] == pytest.approx(30000.0)
    assert up["minute"] == 9 * 60 + 33
    assert down["availability"] == md.AVAILABLE
    assert down["value"] == pytest.approx(40000.0)
    assert down["minute"] == 9 * 60 + 34


def _sina_rows(bars):
    """bars: [(day, open, close, volume)] -> 新浪原始 bar。"""
    return [
        {"day": d, "open": o, "high": max(o, c), "low": min(o, c), "close": c,
         "volume": v, "amount": v * (o + c) / 2}
        for d, o, c, v in bars
    ]


def test_max_directional_minute_volume_sina_classifies_up_and_down():
    rows = _sina_rows([
        ("2026-06-03 09:35:00", 10.0, 10.1, 5000.0),   # up
        ("2026-06-03 09:40:00", 10.1, 10.0, 20000.0),  # down (最大下跌)
        ("2026-06-03 09:45:00", 10.0, 10.3, 30000.0),  # up (最大上攻)
        ("2026-06-03 09:50:00", 10.3, 10.2, 8000.0),   # down
    ])
    up = rv.max_directional_minute_volume(rows, source=md.SOURCE_SINA_5MIN,
                                          direction=rv.DIRECTION_UP)
    down = rv.max_directional_minute_volume(rows, source=md.SOURCE_SINA_5MIN,
                                            direction=rv.DIRECTION_DOWN)
    assert up["value"] == pytest.approx(30000.0)
    assert down["value"] == pytest.approx(20000.0)


def test_max_directional_minute_volume_ignores_rows_after_until_time():
    """反未来函数：入场时刻(09:34)之前的下跌分钟量峰值，喂入之后更大的下跌分钟
    数据(09:40, 09:50)不得改变结果——这是本策略的要害用例。"""
    before_cutoff = _tencent_rows([
        ("0930", 10.0, 1000, 10000.0),
        ("0931", 10.1, 1100, 11100.0),
        ("0932", 10.0, 1150, 11600.0),
        ("0933", 10.2, 1450, 14700.0),
        ("0934", 9.9, 1850, 18400.0),   # +400手=+40000股 down，截至0934的历史极值
    ])
    baseline = rv.max_directional_minute_volume(
        before_cutoff, source=md.SOURCE_TENCENT_INTRADAY,
        direction=rv.DIRECTION_DOWN, until_time="0934")
    assert baseline["value"] == pytest.approx(40000.0)
    assert baseline["minute"] == 9 * 60 + 34

    with_future_rows = before_cutoff + _tencent_rows([
        ("0935", 9.5, 4850, 46650.0),  # +3000手=+300000股 down，发生在 until_time 之后
    ])
    same_cutoff = rv.max_directional_minute_volume(
        with_future_rows, source=md.SOURCE_TENCENT_INTRADAY,
        direction=rv.DIRECTION_DOWN, until_time="0934")
    assert same_cutoff == baseline, "喂入截止时刻之后的行不得改变历史极值判定结果"

    # 反证：不设截止时刻时确实会被未来的大单吃到，证明上面的稳定不是巧合。
    without_cutoff = rv.max_directional_minute_volume(
        with_future_rows, source=md.SOURCE_TENCENT_INTRADAY, direction=rv.DIRECTION_DOWN)
    assert without_cutoff["value"] == pytest.approx(300000.0)


def test_max_directional_minute_volume_reuses_minute_derived_normalize(monkeypatch):
    """复用纪律：分钟量峰值的具体数值必须来自 minute_derived.normalize_tencent_minute
    的输出，不是本模块自己重算的——把 normalize 换成一个返回固定虚构值的桩，
    结果必须原样反映桩返回的数字。"""
    fake_rows = [
        {"minute": 570, "time": "09:30", "volume_shares": 999.0, "amount": 0.0},
        {"minute": 571, "time": "09:31", "volume_shares": 555.0, "amount": 0.0},
    ]
    monkeypatch.setattr(rv.md, "normalize_tencent_minute", lambda rows, **kw: list(fake_rows))
    raw = _tencent_rows([("0930", 10.0, 1, 1.0), ("0931", 9.0, 2, 2.0)])  # 真实解析会得到完全不同的量
    result = rv.max_directional_minute_volume(
        raw, source=md.SOURCE_TENCENT_INTRADAY, direction=rv.DIRECTION_DOWN)
    assert result["value"] == pytest.approx(555.0), (
        "分钟量峰值必须原样来自 normalize_tencent_minute 的桩返回值，"
        "证明本模块没有自己重新解析 cum_volume/cum_amount")


def test_max_directional_minute_volume_returns_unavailable_when_normalize_fails(monkeypatch):
    """normalize_tencent_minute 返回 None（数据本身坏了）时必须原样 fail-closed，
    不得退回自己解析原始字段这条路。"""
    monkeypatch.setattr(rv.md, "normalize_tencent_minute", lambda rows, **kw: None)
    raw = _tencent_rows([("0930", 10.0, 1000, 10000.0), ("0931", 10.1, 1100, 11100.0)])
    result = rv.max_directional_minute_volume(
        raw, source=md.SOURCE_TENCENT_INTRADAY, direction=rv.DIRECTION_UP)
    assert result["availability"] == f"{md.UNAVAILABLE}:minute_rows_unavailable"
    assert result["value"] is None


def test_max_directional_minute_volume_bad_until_time_is_unavailable():
    rows = _tencent_rows([("0930", 10.0, 1000, 10000.0), ("0931", 10.1, 1100, 11100.0)])
    result = rv.max_directional_minute_volume(
        rows, source=md.SOURCE_TENCENT_INTRADAY, direction=rv.DIRECTION_UP,
        until_time="not-a-time")
    assert result["availability"].startswith(f"{md.UNAVAILABLE}:bad_until_time")


def test_max_directional_minute_volume_unknown_source_is_unavailable():
    rows = _tencent_rows([("0930", 10.0, 1000, 10000.0)])
    result = rv.max_directional_minute_volume(
        rows, source="unknown_source", direction=rv.DIRECTION_UP)
    assert result["availability"] == f"{md.UNAVAILABLE}:minute_rows_unavailable"


def test_max_directional_minute_volume_no_matching_direction_is_unavailable():
    # 全天只有一分钟且无法判定方向（首分钟无上一分钟可比）。
    rows = _tencent_rows([("0930", 10.0, 1000, 10000.0)])
    result = rv.max_directional_minute_volume(
        rows, source=md.SOURCE_TENCENT_INTRADAY, direction=rv.DIRECTION_UP)
    assert result["availability"].startswith(f"{md.UNAVAILABLE}:no_up_minute_before_cutoff")


def test_max_directional_minute_volume_invalid_direction_raises():
    with pytest.raises(ValueError):
        rv.max_directional_minute_volume(
            _tencent_rows([("0930", 10.0, 1000, 10000.0)]),
            source=md.SOURCE_TENCENT_INTRADAY, direction="sideways")


# --------------------------------------------------------------------------- #
# 2) NON-LIVE：消费端行为断言（不是配置字段断言）
# --------------------------------------------------------------------------- #
def test_unregistered_signal_is_downgraded_to_watch_by_decision_policy():
    """构造一个 S5 正向信号，断言**消费端**把 buy 降为 watch、仓位倍率归零。"""
    import decision_policy
    import strategy_registry

    fired = [rv.evaluate(_signal_record(), market_state=_STABLE_MARKET)]
    fired = [r for r in fired if r["status"] == rv.STATUS_SIGNAL]
    assert fired, "前置：必须真的有一个正向 S5 信号，否则本用例恒真"

    record = strategy_registry.live_record("reverse_volume")
    assert record is None, "S5 不得出现在 strategy_registry 中"

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

    guidance = ra.position_guidance("reverse_volume", 10.0, 12.0, 9.0, total_asset=100000.0)
    assert guidance["recommended_position_pct"] == 0.0
    assert guidance["recommended_amount"] == 0.0
    assert guidance["method"] == "research_only"
    assert guidance["gating_status"] == "unverified"


def test_pack_is_reported_as_ungated_research_hypothesis():
    import strategy_packs

    record = strategy_packs.registry_records()["reverse_volume"]
    assert record["allowed_in_live_agent"] is False
    assert record["gate_decision"] == "not_gated"
    assert strategy_packs.load_packs()["reverse_volume"]["score_hints"] == []


def test_strategy_registry_reports_not_allowed_in_live():
    import strategy_registry

    assert strategy_registry.is_allowed_in_live("reverse_volume") is False


# --------------------------------------------------------------------------- #
# 回测适配层 —— event_record 的诚实缺口 + 反事实 + fail-closed
# --------------------------------------------------------------------------- #
def test_event_record_leaves_all_evidence_fields_none_by_design():
    """事件表(v3/v4)是单日涨停快照结构，本策略的七类证据都是跨周期/跨分钟的
    时间序列证据，结构上不携带——event_record 必须诚实留空，不得拿近似值顶替。"""
    event = {"code": "600100", "date": "2026-06-03", "lianban": 3, "t_close": 11.0}
    record = bt.event_record(event)
    for field in bt._MISSING_EVIDENCE_FIELDS:
        assert record[field] is None, field


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
    用于反事实证明 P5 约束真的在咬。信号本身通过 monkeypatch bt.event_record 注入
    （见 test_counterfactual_disabling_constraints_inflates_returns），因为真实
    event_record 结构性拿不到本策略需要的证据字段。"""
    return [
        _bt_event("600200", one_word=False, gap_pct_value=6.0, t1_close=11.66),
        _bt_event("600201", one_word=True, gap_pct_value=7.0, t1_close=13.2),
    ]


def _stub_event_record_all_signal(event):
    return _signal_record(code=str(event["code"]).zfill(6), date=event["date"])


def test_counterfactual_disabling_constraints_inflates_returns(monkeypatch):
    monkeypatch.setattr(bt, "event_record", _stub_event_record_all_signal)
    report = bt.counterfactual(_bt_events(), hold_mode="board_overnight",
                               market_state=_STABLE_MARKET)
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
    """不打补丁的真实路径：event_record 对本策略的全部七类证据结构性留空，
    命中数如实为 0，且 unavailable_reasons 能一眼看出缺哪些字段。"""
    report = bt.run(_bt_events(), hold_mode="board_overnight")
    assert report["signal_count"] == 0
    counts = report["signal_summary"]["status_counts"]
    assert counts[rv.STATUS_UNAVAILABLE] == report["universe_count"] > 0
    reasons = report["signal_summary"]["unavailable_reasons"]
    for key in (
        "prior_period_leader_status_missing", "drawdown_pct_missing",
        "market_sentiment_unavailable", "volatility_contraction_ratio_missing",
        "volume_percentile_20d_missing", "reversal_volume_evidence_missing",
        "pullback_volume_evidence_missing",
    ):
        assert reasons.get(key, 0) > 0, key
