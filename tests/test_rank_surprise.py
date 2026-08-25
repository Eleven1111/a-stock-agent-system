"""S1 超预期（RankSurprise）— 纯逻辑单测，不触网。

覆盖四类断言（升级方案 §6.2）：
1) 反事实：关掉 P5 成交约束后回测收益显著虚高 —— 证明约束真的在咬（防假绿主证据）；
2) NON-LIVE 消费端**行为**断言：未注册状态下正向信号被降级为 watch、仓位归零
   （不是断言 pack 里 live:false 这种字段值 —— 配置断言 ≠ 行为断言）；
3) 防事后解释：ActualGap 相同、peer 分布不同 → surprise 必须不同；peer 不足 → unavailable；
4) 四个入场条件各自的边界 + 全缺失 → unavailable。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

import rank_surprise as rs

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "chanlun-backtest" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_SPEC = importlib.util.spec_from_file_location(
    "daban_bt_rank_surprise", SCRIPTS / "daban_bt_rank_surprise.py"
)
bt = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bt)

ALIVE = {"available": True, "dominant_state": "S3"}


# --------------------------------------------------------------------------- #
# 合成 fixture
# --------------------------------------------------------------------------- #
def _record(code, *, prior=10.0, tiebreak=-565.0, gap=1.0, ratio=2.0,
            sector="X", date="2026-06-03", height=1, prior_return=10.0):
    return {
        "code": code, "date": date, "sector": sector,
        "prior_strength": prior, "prior_strength_tiebreak": tiebreak,
        "auction_strength": gap, "prior_return_pct": prior_return,
        "board_height": height, "volume_ratio": ratio,
    }


def _group(n=8):
    """n 个同板块同日 peer：昨日强度全部 +10%（打板 universe 常态），
    封板时间越晚越弱；竞价 gap 随序号递增。最弱的两个同时也是竞价最强的两个。"""
    peers = []
    for i in range(n):
        # i 越大封板越晚（越弱）；gap 随 i 递增（今日越强）。
        peers.append(_record(f"60000{i}", tiebreak=-(560.0 + i), gap=float(i)))
    return peers


def _event(code, *, one_word, gap_pct_value, t1_close, seal="093000",
           date="2026-06-03", sector="X", ratio=2.0, amount=3.0e7):
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
        "volume_ratio": ratio, "volume_ratio_source": "intraday_0945",
        **bar,
    }


def _events():
    """8 个同板块事件：最弱(封板最晚)的两个竞价最强 → S1 命中 2 个。
    其中一个是 T 日一字板（约束下买不进，且次日暴涨），另一个是回封板（买得进，涨幅温和）。
    """
    events = []
    for i in range(6):
        events.append(_event(f"60000{i}", one_word=False, gap_pct_value=float(i),
                             t1_close=11.0, seal=f"09{30 + i}00"))
    # 命中者 A：回封板，可成交，次日 +1%
    events.append(_event("600006", one_word=False, gap_pct_value=6.0,
                         t1_close=11.11, seal="093600"))
    # 命中者 B：一字板，约束下买不进；次日 +20%（关掉约束就会把它算进收益）
    events.append(_event("600007", one_word=True, gap_pct_value=7.0,
                         t1_close=13.2, seal="093700"))
    return events


# --------------------------------------------------------------------------- #
# 1) 反事实：关掉成交约束 → 收益虚高（防假绿主证据）
# --------------------------------------------------------------------------- #
def test_counterfactual_disabling_constraints_inflates_returns():
    report = bt.counterfactual(_events(), market_state=ALIVE, hold_mode="board_overnight")
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
    report = bt.counterfactual([], market_state=ALIVE, hold_mode="board_overnight")
    assert report["with_constraints"]["filled_count"] == 0
    assert report["constraints_bite"] is False
    assert report["mean_return_inflation"] is None


# --------------------------------------------------------------------------- #
# 2) NON-LIVE：消费端行为断言（不是配置字段断言）
# --------------------------------------------------------------------------- #
def test_unregistered_signal_is_downgraded_to_watch_by_decision_policy():
    """构造一个 S1 正向信号，断言**消费端**把 buy 降为 watch、仓位倍率归零。"""
    import decision_policy
    import strategy_registry

    fired = [r for r in rs.evaluate_group(_group(), market_state=ALIVE)
             if r["status"] == rs.STATUS_SIGNAL]
    assert fired, "前置：必须真的有一个正向 S1 信号，否则本用例恒真"

    record = strategy_registry.live_record("rank_surprise")
    assert record is None, "S1 不得出现在 strategy_registry 中"

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

    guidance = ra.position_guidance("rank_surprise", 10.0, 12.0, 9.0, total_asset=100000.0)
    assert guidance["recommended_position_pct"] == 0.0
    assert guidance["recommended_amount"] == 0.0
    assert guidance["method"] == "research_only"
    assert guidance["gating_status"] == "unverified"


def test_pack_is_reported_as_ungated_research_hypothesis():
    import strategy_packs

    record = strategy_packs.registry_records()["rank_surprise"]
    assert record["allowed_in_live_agent"] is False
    assert record["gate_decision"] == "not_gated"
    assert strategy_packs.load_packs()["rank_surprise"]["score_hints"] == []


# --------------------------------------------------------------------------- #
# 3) 防事后解释：预期基准必须事前可算且真的在起作用
# --------------------------------------------------------------------------- #
def test_same_actual_gap_different_peers_yields_different_surprise():
    """两个 ActualGap 完全相同、只有 peer 分布不同的样本，surprise 必须不同。
    若相同，说明预期基准形同虚设，"超预期"退化成"今天涨了所以是超预期"。"""
    weak_peers = [0.0, 0.1, 0.2, 0.3, 0.4]
    strong_peers = [5.0, 5.1, 5.2, 5.3, 5.4]
    common = dict(actual_gap=6.0, prior_return_pct=10.0, board_height=1)
    low = rs.surprise(peer_gaps=weak_peers, **common)
    high = rs.surprise(peer_gaps=strong_peers, **common)
    assert low["status"] == "ok" and high["status"] == "ok"
    assert low["value"] != high["value"]
    assert low["value"] == pytest.approx(6.0 - 0.2)
    assert high["value"] == pytest.approx(6.0 - 5.2)


def test_expected_gap_uses_betas_when_fitted():
    cfg = {**rs.config(), "beta_prior_return": 0.5, "beta_board_height": -1.0,
           "betas_fitted": True}
    baseline = rs.expected_gap(peer_gaps=[1.0, 2.0, 3.0, 4.0, 5.0],
                               prior_return_pct=10.0, board_height=2, cfg=cfg)
    assert baseline["peer_median"] == pytest.approx(3.0)
    assert baseline["value"] == pytest.approx(3.0 + 0.5 * 10.0 - 1.0 * 2)


def test_expected_gap_unavailable_when_peer_sample_insufficient():
    baseline = rs.expected_gap(peer_gaps=[1.0, 2.0], prior_return_pct=10.0, board_height=1)
    assert baseline["status"] == rs.STATUS_UNAVAILABLE
    assert baseline["value"] is None
    assert any("peer_gap_sample_insufficient" in r for r in baseline["reasons"])


def test_surprise_unavailable_never_degrades_to_zero():
    delta = rs.surprise(actual_gap=None, peer_gaps=[1.0] * 6,
                        prior_return_pct=10.0, board_height=1)
    assert delta["status"] == rs.STATUS_UNAVAILABLE
    assert delta["value"] is None


def test_betas_unfitted_is_flagged_as_degraded():
    fired = rs.evaluate_group(_group(), market_state=ALIVE)
    assert all("betas_unfitted_placeholder" in r["degraded"] for r in fired)


# --------------------------------------------------------------------------- #
# 4) 四个入场条件的边界 + 不满足时不触发
# --------------------------------------------------------------------------- #
def _evaluate_target(peers, target_code="600007"):
    target = next(p for p in peers if p["code"] == target_code)
    return rs.evaluate(target, peers=peers, market_state=ALIVE)


def test_baseline_group_fires_for_weakest_yesterday_strongest_today():
    result = _evaluate_target(_group())
    assert result["status"] == rs.STATUS_SIGNAL
    assert all(c["ok"] for c in result["conditions"])
    assert result["surprise"]["value"] == pytest.approx(7.0 - 3.0)  # peer 中位数 3.0


def test_prior_rank_boundary_not_bottom_30pct():
    """昨日强度改成板块内最强（分位 1.0）→ 条件 1 不满足，不触发。"""
    peers = _group()
    target = next(p for p in peers if p["code"] == "600007")
    target["prior_strength"] = 99.0
    result = _evaluate_target(peers)
    assert result["status"] == rs.STATUS_NO_SIGNAL
    assert {c["id"]: c["ok"] for c in result["conditions"]}[rs.COND_PRIOR_RANK] is False


def test_auction_rank_boundary_just_below_top_20pct():
    """竞价强度落到分位 0.714（<0.8）→ 条件 2 不满足。"""
    peers = _group()
    target = next(p for p in peers if p["code"] == "600007")
    target["auction_strength"] = 4.5   # 排第 6/8 → 分位 5/7≈0.714
    result = _evaluate_target(peers)
    assert result["status"] == rs.STATUS_NO_SIGNAL
    assert {c["id"]: c["ok"] for c in result["conditions"]}[rs.COND_AUCTION_RANK] is False


@pytest.mark.parametrize("ratio,expected", [(1.5, False), (1.51, True)])
def test_volume_ratio_boundary_is_strictly_greater(ratio, expected):
    peers = _group()
    target = next(p for p in peers if p["code"] == "600007")
    target["volume_ratio"] = ratio
    result = _evaluate_target(peers)
    assert {c["id"]: c["ok"] for c in result["conditions"]}[rs.COND_VOLUME_RATIO] is expected
    assert result["status"] == (rs.STATUS_SIGNAL if expected else rs.STATUS_NO_SIGNAL)


def test_theme_ebbing_blocks_signal():
    peers = _group()
    target = next(p for p in peers if p["code"] == "600007")
    result = rs.evaluate(target, peers=peers,
                         market_state={"available": True, "dominant_state": "S6"})
    assert result["status"] == rs.STATUS_NO_SIGNAL
    assert {c["id"]: c["ok"] for c in result["conditions"]}[rs.COND_THEME_ALIVE] is False


def test_missing_market_state_is_unavailable_not_no_signal():
    peers = _group()
    target = next(p for p in peers if p["code"] == "600007")
    result = rs.evaluate(target, peers=peers, market_state=None)
    assert result["status"] == rs.STATUS_UNAVAILABLE
    assert "theme_state_unavailable" in result["reasons"]


def test_missing_sector_and_volume_ratio_are_unavailable():
    peers = _group()
    target = next(p for p in peers if p["code"] == "600007")
    target["sector"] = None
    target["volume_ratio"] = None
    result = rs.evaluate(target, peers=peers, market_state=ALIVE)
    assert result["status"] == rs.STATUS_UNAVAILABLE
    assert "sector_missing" in result["reasons"]
    assert "volume_ratio_missing" in result["reasons"]


def test_all_evidence_missing_is_unavailable():
    empty = {"code": "600001"}
    result = rs.evaluate(empty, peers=[empty], market_state=None)
    assert result["status"] == rs.STATUS_UNAVAILABLE
    assert result["surprise"]["value"] is None
    for reason in ("sector_missing", "theme_state_unavailable", "volume_ratio_missing"):
        assert reason in result["reasons"]


def test_peer_group_too_small_is_unavailable():
    peers = _group(n=3)
    result = _evaluate_target(peers, target_code="600002")
    assert result["status"] == rs.STATUS_UNAVAILABLE
    assert any("peer_sample_insufficient" in r for r in result["reasons"])


# --------------------------------------------------------------------------- #
# 回测适配层：缺 09:45 量比时必须 fail-closed 成零样本，且原因可见
# --------------------------------------------------------------------------- #
def test_backtest_fails_closed_when_volume_ratio_absent_from_event_table():
    events = [{k: v for k, v in event.items() if k != "volume_ratio"}
              for event in _events()]
    report = bt.run(events, market_state=ALIVE, hold_mode="board_overnight")
    assert report["signal_count"] == 0
    counts = report["signal_summary"]["status_counts"]
    assert counts[rs.STATUS_UNAVAILABLE] == report["universe_count"] > 0
    assert report["signal_summary"]["unavailable_reasons"]["volume_ratio_missing"] > 0


def test_event_record_maps_gap_and_prior_return():
    record = bt.event_record(_event("600001", one_word=False, gap_pct_value=3.0,
                                    t1_close=11.0))
    assert record["auction_strength"] == pytest.approx(3.0)
    assert record["prior_return_pct"] == pytest.approx(10.0)
    assert record["prior_strength_tiebreak"] == pytest.approx(-570.0)
