"""S3 最强助攻套利（AssistArbitrage）— 纯逻辑单测，不触网。

覆盖六类断言（升级方案 §6.2，照 tests/test_rank_surprise.py / test_divergence_reseal.py
的组织方式）：
1) 反事实：关掉 P5 成交约束后回测收益显著虚高 —— 证明约束真的在咬（防假绿主证据）；
2) NON-LIVE 消费端**行为**断言：未注册状态下正向信号被降级为 watch、仓位归零
   （不是断言 pack 里 live:false 这种字段值 —— 配置断言 ≠ 行为断言）；
3) 退出条件（本策略要害）：龙头走弱 ∧ 题材广度下降 → 必须退出，即使候选自身仍强；
   新主线 DirectionScore 超原主线 ≥ 阈值 → 也必须退出；
4) LeaderScore 复用：断言取自 leader_score_shadow，不可得 → unavailable；
5) 四个入场条件各自边界 + 全缺失 → unavailable；
6) 回测适配层：真实事件表缺 LeaderScore/突破时刻证据时必须 fail-closed 成零命中。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

import assist_arbitrage as aa

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "chanlun-backtest" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_SPEC = importlib.util.spec_from_file_location(
    "daban_bt_assist_arbitrage", SCRIPTS / "daban_bt_assist_arbitrage.py"
)
bt = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bt)


# --------------------------------------------------------------------------- #
# 合成 fixture —— 信号层
# --------------------------------------------------------------------------- #
def _leader(code="900000", *, board_height=5.0, score=85.0, score_status="ok",
            first_seal="093000", **extra):
    shadow = None
    if score_status == "ok":
        shadow = {"status": "ok", "score": score}
    elif score_status == "unavailable":
        shadow = {"status": "unavailable", "score": None, "reason": "insufficient_factor_weight"}
    leader = {
        "code": code, "board_height": board_height,
        "leader_score_shadow": shadow,
        "leader_confirmed": first_seal is not None,
    }
    leader.update(extra)
    return leader


def _candidate(code, *, sector="X", date="2026-06-03", board_height=3.0,
               sector_breadth=5.0, change_pct=5.0, breakout_time="093500", **extra):
    record = {
        "code": code, "date": date, "sector": sector,
        "board_height": board_height, "sector_breadth_count": sector_breadth,
        "change_pct": change_pct, "breakout_time": breakout_time,
    }
    record.update(extra)
    return record


def _baseline_group(n=6):
    """n 个同题材同日候选：change_pct 与 breakout_time 都随 index 递增
    （index 0 涨幅最低/突破最晚，index n-1 涨幅最高/突破最早不成立——刻意让
    两个指标"背离"排布，index 0 的 change_pct 最低但 breakout 最早，避免两个
    条件因为同向排列而互相掩盖对方的边界）。"""
    peers = []
    for i in range(n):
        peers.append(_candidate(
            f"60000{i}", change_pct=float(i), breakout_time=f"{9 + i:02d}3000",
        ))
    return peers


def _leader_and_group(n=6, **leader_kwargs):
    leader = _leader(**leader_kwargs)
    return leader, _baseline_group(n)


def _evaluate_target(peers, leader, target_code="600000", cfg=None):
    target = next(p for p in peers if p["code"] == target_code)
    return aa.evaluate(target, leader=leader, peers=peers, cfg=cfg)


# --------------------------------------------------------------------------- #
# 5) 四个入场条件的边界 + 不满足时不触发 / 缺失时 unavailable
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("score,expected", [(79.9, False), (80.0, True)])
def test_leader_score_boundary_is_inclusive_ge(score, expected):
    leader, peers = _leader_and_group(score=score)
    # 让目标候选同时满足其余三条，只考察 LeaderScore 这一维。
    target = peers[0]
    target["change_pct"] = 100.0  # 强行拉到相对强度最高，排除干扰
    target["breakout_time"] = "090000"  # 强行最早突破
    result = aa.evaluate(target, leader=leader, peers=peers)
    assert {c["id"]: c["ok"] for c in result["conditions"]}[aa.COND_LEADER_SCORE] is expected
    assert result["status"] == (aa.STATUS_SIGNAL if expected else aa.STATUS_NO_SIGNAL)


def test_leader_score_unavailable_when_shadow_missing_is_fail_closed():
    """LeaderScore 不可得（leader_score_shadow 缺失/status!=ok）→ unavailable，
    绝不当"默认不达标"处理成 no_signal——这是复用纪律的行为证据。"""
    leader, peers = _leader_and_group(score_status="unavailable")
    result = _evaluate_target(peers, leader, "600000")
    assert result["status"] == aa.STATUS_UNAVAILABLE
    assert "leader_score_shadow_unavailable" in result["reasons"]


def test_leader_score_reads_from_leader_score_shadow_field_only():
    """断言 LeaderScore 条件确实读的是 leader_score_shadow.score，不是其它字段
    （例如误读候选自己的字段）——两个 leader 除 leader_score_shadow.score 外
    完全一致，只有它变化，结论必须跟着变。"""
    peers = _baseline_group()
    target = peers[0]
    target["change_pct"], target["breakout_time"] = 100.0, "090000"
    leader_low = _leader(score=50.0)
    leader_high = _leader(score=95.0)
    low_result = aa.evaluate(target, leader=leader_low, peers=peers)
    high_result = aa.evaluate(dict(target), leader=leader_high, peers=peers)
    assert low_result["status"] == aa.STATUS_NO_SIGNAL
    assert high_result["status"] == aa.STATUS_SIGNAL
    assert low_result["leader_score"] == 50.0
    assert high_result["leader_score"] == 95.0


@pytest.mark.parametrize("breadth,expected", [(2.0, False), (3.0, True)])
def test_sector_breadth_boundary_is_inclusive_ge(breadth, expected):
    leader, peers = _leader_and_group()
    target = peers[0]
    target["change_pct"], target["breakout_time"] = 100.0, "090000"
    target["sector_breadth_count"] = breadth
    result = aa.evaluate(target, leader=leader, peers=peers)
    assert {c["id"]: c["ok"] for c in result["conditions"]}[aa.COND_SECTOR_BREADTH] is expected
    assert result["status"] == (aa.STATUS_SIGNAL if expected else aa.STATUS_NO_SIGNAL)


@pytest.mark.parametrize("board_height,expected", [(5.0, False), (4.0, True), (1.0, True)])
def test_board_level_gap_boundary_requires_strictly_lower_level(board_height, expected):
    """龙头连板5：候选必须 ≤4（至少矮一级）才算助攻；候选=5(并列)不合格。"""
    leader, peers = _leader_and_group(board_height=5.0)
    target = peers[0]
    target["change_pct"], target["breakout_time"] = 100.0, "090000"
    target["board_height"] = board_height
    result = aa.evaluate(target, leader=leader, peers=peers)
    assert {c["id"]: c["ok"] for c in result["conditions"]}[aa.COND_BOARD_LEVEL_GAP] is expected
    assert result["status"] == (aa.STATUS_SIGNAL if expected else aa.STATUS_NO_SIGNAL)


def test_board_level_gap_unavailable_when_leader_missing():
    peers = _baseline_group()
    result = aa.evaluate(peers[0], leader=None, peers=peers)
    assert result["status"] == aa.STATUS_UNAVAILABLE
    assert "leader_missing" in result["reasons"]
    assert {c["id"]: c["ok"] for c in result["conditions"]}[aa.COND_BOARD_LEVEL_GAP] is None


@pytest.mark.parametrize("percentile_rank,expected", [(3, False), (1, True)])
def test_relative_strength_top20_boundary(percentile_rank, expected):
    """8 人组：percentile = weaker_count/7。rank1(最强,weaker=7,pct=1.0)、
    rank2(weaker=6,pct=0.857) 都 ≥0.8 合格；rank3(weaker=5,pct=0.714) 不合格。
    这里用"倒数第 percentile_rank 强"控制分位，body 见 _group8_change_pct。"""
    leader, _ = _leader_and_group()
    peers = [_candidate(f"70{i:04d}", change_pct=float(i)) for i in range(8)]
    target = peers[-percentile_rank]  # 越大 index 涨幅越高
    target["breakout_time"] = "090000"
    result = aa.evaluate(target, leader=leader, peers=peers)
    assert {c["id"]: c["ok"] for c in result["conditions"]}[aa.COND_RELATIVE_STRENGTH] is expected
    assert result["status"] == (aa.STATUS_SIGNAL if expected else aa.STATUS_NO_SIGNAL)


def test_entry_trigger_requires_leader_confirmed_and_first_breakout():
    leader, peers = _leader_and_group()
    target = peers[0]
    target["change_pct"] = 100.0
    # 用 _baseline_group 的 breakout_time 排布：index0 是 "093000"，本身就是全组最早。
    result = aa.evaluate(target, leader=leader, peers=peers)
    assert {c["id"]: c["ok"] for c in result["conditions"]}[aa.COND_ENTRY_TRIGGER] is True

    not_confirmed = _leader(first_seal=None)
    result2 = aa.evaluate(dict(target), leader=not_confirmed, peers=peers)
    assert {c["id"]: c["ok"] for c in result2["conditions"]}[aa.COND_ENTRY_TRIGGER] is False

    # index0 换成不是最早突破 —— 只把 index0 的时刻改晚，其余不动。
    peers_late = _baseline_group()
    peers_late[0]["breakout_time"] = "150000"
    peers_late[0]["change_pct"] = 100.0
    late_target = peers_late[0]
    result3 = aa.evaluate(late_target, leader=leader, peers=peers_late)
    assert {c["id"]: c["ok"] for c in result3["conditions"]}[aa.COND_ENTRY_TRIGGER] is False


def test_all_evidence_missing_is_unavailable():
    empty = {"code": "600001"}
    result = aa.evaluate(empty, leader=None, peers=[empty])
    assert result["status"] == aa.STATUS_UNAVAILABLE
    for reason in (
        "sector_missing", "leader_missing",
        "sector_breadth_count_missing",
        "relative_strength_rank_unavailable",
    ):
        assert reason in result["reasons"]


def test_missing_sector_is_unavailable():
    leader, peers = _leader_and_group()
    target = peers[0]
    target["sector"] = None
    result = aa.evaluate(target, leader=leader, peers=peers)
    assert result["status"] == aa.STATUS_UNAVAILABLE
    assert "sector_missing" in result["reasons"]


def test_record_not_in_peer_group_is_unavailable():
    leader, peers = _leader_and_group()
    outsider = _candidate("999999")
    result = aa.evaluate(outsider, leader=leader, peers=peers)
    assert result["status"] == aa.STATUS_UNAVAILABLE
    assert "record_not_in_theme_peer_group" in result["reasons"]


# --------------------------------------------------------------------------- #
# pick_leader —— 龙头识别只看连板高度，缺失一律不可判定
# --------------------------------------------------------------------------- #
def test_pick_leader_selects_highest_board_height():
    peers = [_candidate("A", board_height=2.0), _candidate("B", board_height=5.0),
              _candidate("C", board_height=3.0)]
    leader = aa.pick_leader(peers)
    assert leader["code"] == "B"


def test_pick_leader_tie_breaks_by_smallest_code():
    peers = [_candidate("600002", board_height=5.0), _candidate("600001", board_height=5.0)]
    leader = aa.pick_leader(peers)
    assert leader["code"] == "600001"


def test_pick_leader_returns_none_when_no_board_height_available():
    peers = [{"code": "A"}, {"code": "B"}]
    assert aa.pick_leader(peers) is None


def test_evaluate_group_excludes_leader_from_candidates():
    leader, peers = _leader_and_group()
    peers_with_leader_shape = [dict(p) for p in peers]
    peers_with_leader_shape.append(
        {"code": "LEADER", "date": "2026-06-03", "sector": "X",
         "board_height": 99.0, "sector_breadth_count": 5.0,
         "change_pct": 0.0, "breakout_time": None,
         "leader_score_shadow": {"status": "ok", "score": 90.0},
         "leader_confirmed": True}
    )
    results = aa.evaluate_group(peers_with_leader_shape)
    assert "LEADER" not in {r["code"] for r in results}
    assert len(results) == len(peers_with_leader_shape) - 1


# --------------------------------------------------------------------------- #
# 3) 退出条件 —— 本策略的成败点：候选自身强也不能否决退出
# --------------------------------------------------------------------------- #
def test_leader_weakening_and_breadth_declining_forces_exit_even_if_candidate_still_strong():
    """构造一个候选自身量价依然很强（entry 仍是 signal）的样本，
    同时喂给 exit_signal 龙头走弱+题材广度下降的证据 —— 必须给出退出。"""
    leader, peers = _leader_and_group()
    target = peers[0]
    target["change_pct"], target["breakout_time"] = 100.0, "090000"
    entry = aa.evaluate(target, leader=leader, peers=peers)
    assert entry["status"] == aa.STATUS_SIGNAL, "前置：候选自身必须真的是强信号，否则本用例恒真"

    exit_record = {
        "code": target["code"], "date": target["date"],
        "leader_board_broken": True,
        "sector_breadth_count": 2.0, "sector_breadth_count_prior": 5.0,
    }
    result = aa.exit_signal(exit_record)
    assert result["status"] == aa.STATUS_EXIT
    assert result["leader_weakening"]["weak"] is True
    assert result["breadth_declining"]["declining"] is True


def test_mainline_rotation_alone_forces_exit():
    record = {
        "code": "600000", "date": "2026-06-03",
        "original_mainline_direction_score": 60.0,
        "new_mainline_direction_score": 76.0,  # gap=16 >= 15
    }
    result = aa.exit_signal(record)
    assert result["status"] == aa.STATUS_EXIT
    assert result["mainline_rotation"]["triggered"] is True


def test_leader_weak_alone_without_breadth_decline_does_not_exit():
    """路径A是"龙头走弱 ∧ 题材广度下降"的合取，不是析取——龙头走弱但题材广度
    没有下降时，不能单凭龙头走弱就退出（否则会在题材依然健康时误杀）。"""
    record = {
        "code": "600000", "date": "2026-06-03",
        "leader_board_broken": True,
        "sector_breadth_count": 5.0, "sector_breadth_count_prior": 5.0,  # 未下降
        "original_mainline_direction_score": 60.0, "new_mainline_direction_score": 61.0,
    }
    result = aa.exit_signal(record)
    assert result["status"] == aa.STATUS_HOLD
    assert result["leader_weakening"]["weak"] is True
    assert result["breadth_declining"]["declining"] is False


def test_exit_reports_hold_only_when_both_paths_ruled_out():
    record = {
        "code": "600000", "date": "2026-06-03",
        "leader_board_broken": False, "leader_change_pct": 1.0,
        "sector_breadth_count": 5.0, "sector_breadth_count_prior": 5.0,
        "original_mainline_direction_score": 60.0, "new_mainline_direction_score": 61.0,
    }
    result = aa.exit_signal(record)
    assert result["status"] == aa.STATUS_HOLD


def test_exit_reports_unavailable_when_evidence_missing_and_not_triggered():
    """龙头走弱证据缺失、题材广度趋势缺失、主线切换也未触发 —— 不能报 hold，
    因为没有足够证据排除退出，必须报 unavailable。"""
    record = {"code": "600000", "date": "2026-06-03"}
    result = aa.exit_signal(record)
    assert result["status"] == aa.STATUS_UNAVAILABLE
    assert "leader_weakening_evidence_missing" in result["reasons"]
    assert "sector_breadth_trend_missing" in result["reasons"]
    assert "mainline_direction_score_missing" in result["reasons"]


@pytest.mark.parametrize("change_pct,expected", [(-2.9, False), (-3.0, True)])
def test_leader_weakening_boundary_is_inclusive_le(change_pct, expected):
    result = aa.leader_weakening(
        {"leader_change_pct": change_pct}, aa.config(),
    )
    assert result["weak"] is expected


@pytest.mark.parametrize("drop,expected", [(0.9, False), (1.0, True)])
def test_breadth_declining_boundary_is_inclusive_ge(drop, expected):
    result = aa.breadth_declining(
        {"sector_breadth_count": 5.0 - drop, "sector_breadth_count_prior": 5.0}, aa.config(),
    )
    assert result["declining"] is expected


@pytest.mark.parametrize("gap,expected", [(14.9, False), (15.0, True)])
def test_mainline_rotation_boundary_is_inclusive_ge(gap, expected):
    result = aa.mainline_rotation(
        {"original_mainline_direction_score": 60.0, "new_mainline_direction_score": 60.0 + gap},
        aa.config(),
    )
    assert result["triggered"] is expected


# --------------------------------------------------------------------------- #
# 2) NON-LIVE：消费端行为断言（不是配置字段断言）
# --------------------------------------------------------------------------- #
def test_unregistered_signal_is_downgraded_to_watch_by_decision_policy():
    """构造一个 S3 正向信号，断言**消费端**把 buy 降为 watch、仓位倍率归零。"""
    import decision_policy
    import strategy_registry

    leader, peers = _leader_and_group()
    target = peers[0]
    target["change_pct"], target["breakout_time"] = 100.0, "090000"
    fired = [aa.evaluate(target, leader=leader, peers=peers)]
    fired = [r for r in fired if r["status"] == aa.STATUS_SIGNAL]
    assert fired, "前置：必须真的有一个正向 S3 信号，否则本用例恒真"

    record = strategy_registry.live_record("assist_arbitrage")
    assert record is None, "S3 不得出现在 strategy_registry 中"

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

    guidance = ra.position_guidance("assist_arbitrage", 10.0, 12.0, 9.0, total_asset=100000.0)
    assert guidance["recommended_position_pct"] == 0.0
    assert guidance["recommended_amount"] == 0.0
    assert guidance["method"] == "research_only"
    assert guidance["gating_status"] == "unverified"


def test_pack_is_reported_as_ungated_research_hypothesis():
    import strategy_packs

    record = strategy_packs.registry_records()["assist_arbitrage"]
    assert record["allowed_in_live_agent"] is False
    assert record["gate_decision"] == "not_gated"
    assert strategy_packs.load_packs()["assist_arbitrage"]["score_hints"] == []


def test_strategy_registry_reports_not_allowed_in_live():
    import strategy_registry

    assert strategy_registry.is_allowed_in_live("assist_arbitrage") is False


# --------------------------------------------------------------------------- #
# 回测适配层 —— event_record 映射 + 反事实 + fail-closed
# --------------------------------------------------------------------------- #
def _bt_event(code, *, board_height, seal, breakout_time, one_word,
              gap_pct_value, t1_close, date="2026-06-03", sector="X",
              breadth=5.0, amount=3.0e7):
    """T 日主板涨停事件：close 固定在真实 10% 涨停价，不随任何测试参数漂移
    （事件表 universe 全是涨停事件，涨跌幅本身没有区分度——见模块 docstring）。
    相对强度改由 ``seal``（封板早晚）区分：越早封板，event_record 映射出的
    change_pct 代理值越大。one_word=True → 全日一字封死（约束模型判买不进）。"""
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
        "sector_limitup_count": breadth, "breakout_time": breakout_time,
        **bar,
    }


def _bt_events():
    """9 个同题材事件：1 个龙头(lianban最高，被 pick_leader 选中、不参与候选评估)+
    6 个基线候选(封板晚/突破晚，两条都不够格)+ 2 个"赢家"(其余三条硬条件都满足，
    组内封板最早的两个，也是突破最早的两个)。赢家 A 可成交，赢家 B 一字板买不进但
    次日暴涨——用于反事实证明 P5 约束真的在咬。"""
    events = [
        _bt_event("600900", board_height=8.0, seal="094500", breakout_time=None,
                  one_word=False, gap_pct_value=0.0, t1_close=11.0),
    ]
    for i in range(6):
        events.append(_bt_event(
            f"60010{i}", board_height=1.0, seal=f"09{40 + i:02d}00",
            breakout_time=f"10{i:02d}00", one_word=False, gap_pct_value=1.0, t1_close=11.0,
        ))
    # 赢家 A：回封/可成交，封板与突破都是组内最早，次日温和 +1%。
    events.append(_bt_event(
        "600200", board_height=1.0, seal="093000", breakout_time="093100",
        one_word=False, gap_pct_value=6.0, t1_close=11.66,
    ))
    # 赢家 B：一字板，约束下买不进；次日 +20%（关掉约束就会把它算进收益）。
    events.append(_bt_event(
        "600201", board_height=1.0, seal="093100", breakout_time="093200",
        one_word=True, gap_pct_value=7.0, t1_close=13.2,
    ))
    return events


def _monkeypatch_leader_score_ok(monkeypatch, score=90.0):
    def _stub(candidate, **kwargs):
        return {"status": "ok", "score": score, "shadow_only": True, "calibrated": False}
    monkeypatch.setattr(bt.hms, "leader_score", _stub)


_LOOSE_CFG = dict(aa.config())
_LOOSE_CFG["breakout_rank_top_n"] = 2  # 本测试要两个"赢家"都通过突破排名，方案原文
# 的"率先"默认只取1个，这里放宽仅用于验证回测管线本身（引擎/约束/信号接线），
# 不代表默认阈值改变——config/daban_thresholds.yaml 的 breakout_rank_top_n 仍是 1。


def test_counterfactual_disabling_constraints_inflates_returns(monkeypatch):
    _monkeypatch_leader_score_ok(monkeypatch)
    report = bt.counterfactual(_bt_events(), hold_mode="board_overnight", cfg=_LOOSE_CFG)
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


def test_backtest_fails_closed_when_leader_score_shadow_unavailable():
    """不打补丁的真实路径：事件表现算 leader_score_shadow 时，六因子里可算的
    只有 assist_breadth（权重0.15），远低于 min_available_weight(0.60)，
    LeaderScore 必然 unavailable —— 这是数据缺口不是"没有信号"，命中数如实为 0。"""
    report = bt.run(_bt_events(), hold_mode="board_overnight")
    assert report["signal_count"] == 0
    counts = report["signal_summary"]["status_counts"]
    assert counts[aa.STATUS_UNAVAILABLE] == report["universe_count"] > 0
    reasons = report["signal_summary"]["unavailable_reasons"]
    assert reasons.get("leader_score_shadow_unavailable", 0) > 0


def test_event_record_maps_s3_fields_directly():
    record = bt.event_record(_bt_event(
        "600100", board_height=3.0, seal="093300", breakout_time="093300",
        one_word=False, gap_pct_value=2.0, t1_close=11.0,
    ))
    assert record["board_height"] == 3.0
    assert record["sector_breadth_count"] == pytest.approx(5.0)
    # change_pct = −封板分钟数：09:33 → -(9*60+33) = -573。
    assert record["change_pct"] == pytest.approx(-573.0)
    assert record["breakout_time"] == "093300"
