"""市场情绪温度计 — 五档判定 / 晋级率 / 退潮硬信号 / 梯队滚动。"""

import market_temperature as mt
import signal_context as sc


def _ladder(spec):
    """spec: {code: lianban}"""
    return {c: {"lianban": n, "sector": "半导体"} for c, n in spec.items()}


def test_height_and_promotion():
    today = _ladder({"000001": 3, "000002": 1, "000003": 2})
    prev = _ladder({"000001": 2, "000002": 1, "000004": 1})
    assert mt.ladder_height(today) == 3
    # 昨日3只，今日 lianban>=2 的有 000001(3)、000003 不在昨日 → 只有 000001 → 1/3
    assert mt.promotion_rate(today, prev) == round(1 / 3, 4)
    assert mt.promotion_rate(today, None) is None


def test_classify_five_tiers():
    assert mt.classify_tier(2, 0.10)["tier"] == "冰点"
    assert mt.classify_tier(3, 0.25)["tier"] == "修复"
    assert mt.classify_tier(5, 0.40)["tier"] == "发酵"
    assert mt.classify_tier(6, 0.55)["tier"] == "加速"
    assert mt.classify_tier(8, 0.40)["tier"] == "极热"   # height>=8 单边触发
    assert mt.classify_tier(5, 0.75)["tier"] == "极热"   # promo>=0.70 单边触发


def test_classify_without_promo_conservative():
    out = mt.classify_tier(6, None)
    assert out["tier"] == "发酵"  # 缺晋级率不判加速，保守降档
    assert any("晋级率缺失" in n for n in out["notes"])


def test_tier_constraints():
    frozen = mt.compute_temperature(_ladder({"a": 1, "b": 2}), _ladder({"a": 1}))
    assert frozen["tier"] == "冰点"
    assert frozen["allow_new_daban"] is False
    assert frozen["top_n_limit"] == 0

    accel = mt.compute_temperature(
        _ladder({"a": 6, "b": 3, "c": 2}),
        _ladder({"a": 5, "b": 2, "c": 1, "d": 1}),
    )
    # 4只昨日涨停，今日>=2板的有 a/b/c → 75% → 极热(promo>=0.70)
    assert accel["tier"] == "极热"
    assert accel["position_multiplier"] == 0.0


def test_retreat_signal_forces_exit_only():
    today = _ladder({"a": 5, "b": 2})
    prev = _ladder({"a": 4, "b": 1})
    morning = {"a": {"change_pct": -6.2}, "b": {"change_pct": 1.0}}
    out = mt.compute_temperature(today, prev, morning_quotes=morning)
    assert out["retreat_signal"] is not None
    assert out["allow_new_daban"] is False
    assert out["position_multiplier"] == 0.0


def test_missing_ladder_neutral():
    out = mt.compute_temperature(None)
    assert out["tier"] == "neutral"
    assert out["position_multiplier"] == 1.0
    assert out["allow_new_daban"] is True


def test_ladder_rolls_to_prev_on_new_day(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sc.update_signal_context({"lianban_ladder": _ladder({"000001": 1}),
                              "ladder_asof": "2026-06-10"})
    # 同日重复写 → 覆盖不滚动
    sc.update_signal_context({"lianban_ladder": _ladder({"000001": 1, "000002": 1}),
                              "ladder_asof": "2026-06-10"})
    ctx = sc.read_signal_context()
    assert "prev_lianban_ladder" not in ctx
    # 新交易日 → 滚动
    sc.update_signal_context({"lianban_ladder": _ladder({"000001": 2}),
                              "ladder_asof": "2026-06-11"})
    ctx = sc.read_signal_context()
    assert ctx["prev_ladder_asof"] == "2026-06-10"
    assert set(ctx["prev_lianban_ladder"].keys()) == {"000001", "000002"}
    assert mt.promotion_rate(ctx["lianban_ladder"], ctx["prev_lianban_ladder"]) == 0.5
