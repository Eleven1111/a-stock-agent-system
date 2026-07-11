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
    assert frozen["allow_new_daban"] is True
    assert frozen["top_n_limit"] == 2

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


def test_missing_ladder_is_unknown_and_blocks_new_risk():
    out = mt.compute_temperature(None)
    assert out["tier"] == "unknown"
    assert out["context_status"] == "unknown"
    assert out["position_multiplier"] == 0.0
    assert out["allow_new_daban"] is False
    assert out["top_n_limit"] == 0


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


def test_stale_context_is_first_class_and_blocks_new_risk():
    ctx = {
        "ladder_asof": "2026-06-01",
        "lianban_ladder": _ladder({"000001": 8}),
        "prev_lianban_ladder": _ladder({"000001": 7}),
    }

    out = mt.temperature_from_context(
        ctx,
        event_asof="2026-06-11",
        max_age_days=4,
    )

    assert out["tier"] == "stale"
    assert out["context_status"] == "stale"
    assert out["context_fresh"] is False
    assert out["allow_new_daban"] is False
    assert out["position_multiplier"] == 0.0
    assert "过期" in out["notes"][0]


def test_future_context_is_unknown_and_blocks_new_risk():
    out = mt.temperature_from_context(
        {
            "ladder_asof": "2026-06-12",
            "lianban_ladder": _ladder({"000001": 8}),
        },
        event_asof="2026-06-11",
    )

    assert out["tier"] == "unknown"
    assert out["context_status"] == "unknown"
    assert out["allow_new_daban"] is False
    assert out["position_multiplier"] == 0.0


def test_retreat_detection_accepts_prefixed_quotes_and_open_gap():
    yesterday = _ladder({"600001": 5, "000002": 2})
    morning = {
        "sh600001": {
            "open": 9.3,
            "prev_close": 10.0,
            "change_pct": -1.0,
        }
    }

    signal = mt.detect_retreat(yesterday, morning)

    assert signal is not None
    assert "600001" in signal


def test_read_temperature_keeps_weekend_context_for_date_gate(monkeypatch):
    captured = {}

    def _read_signal_context(max_age_hours):
        captured["max_age_hours"] = max_age_hours
        return {
            "ladder_asof": "2026-06-12",
            "lianban_ladder": _ladder({"600001": 4}),
        }

    monkeypatch.setattr(mt, "read_signal_context", _read_signal_context)

    out = mt.read_temperature(
        event_asof="2026-06-15",
        max_age_days=4,
    )

    assert captured["max_age_hours"] == 96
    assert out["context_fresh"] is True
    assert out["context_asof"] == "2026-06-12"


def test_read_temperature_exception_is_unknown_and_blocks_new_risk(monkeypatch):
    def _broken_context(**_kwargs):
        raise RuntimeError("corrupt signal context")

    monkeypatch.setattr(mt, "read_signal_context", _broken_context)

    out = mt.read_temperature(event_asof="2026-06-15")

    assert out["tier"] == "unknown"
    assert out["context_status"] == "unknown"
    assert out["allow_new_daban"] is False
    assert out["position_multiplier"] == 0.0
    assert "corrupt signal context" in out["notes"][0]


def test_daban_strategic_weight_default_and_override(monkeypatch):
    monkeypatch.delenv("HERMES_DABAN_STRATEGIC_WEIGHT", raising=False)
    assert mt.daban_strategic_weight() == 0.5            # 1+2 定位默认温和减半
    monkeypatch.setenv("HERMES_DABAN_STRATEGIC_WEIGHT", "0.3")
    assert mt.daban_strategic_weight() == 0.3
    monkeypatch.setenv("HERMES_DABAN_STRATEGIC_WEIGHT", "0")
    assert mt.daban_strategic_weight() == 0.0
    monkeypatch.setenv("HERMES_DABAN_STRATEGIC_WEIGHT", "1.5")   # 越界回退默认
    assert mt.daban_strategic_weight() == 0.5
    monkeypatch.setenv("HERMES_DABAN_STRATEGIC_WEIGHT", "abc")   # 非法回退默认
    assert mt.daban_strategic_weight() == 0.5


# ── S0-S6 概率状态机 ──────────────────────────────────────────────────────────

def test_market_state_maps_tier_to_base_state():
    out = mt.classify_market_state({"tier": "发酵", "retreat_signal": None})
    assert out["available"] is True
    assert out["dominant_state"] == "S2"   # 发酵 → S2 主峰
    assert out["market_state_prob"]["S2"] == max(out["market_state_prob"].values())
    assert abs(sum(out["market_state_prob"].values()) - 1.0) < 1e-3


def test_market_state_neutral_when_temperature_missing():
    out = mt.classify_market_state({"tier": "unknown", "context_status": "unknown"})
    assert out["available"] is False
    assert out["calibrated"] is False
    assert out["dominant_state"] is None
    assert out["market_state_prob"] == {}
    assert out["context_status"] == "unknown"


def test_retreat_and_fragility_push_state_to_ebbing():
    out = mt.classify_market_state(
        {"tier": "加速", "retreat_signal": "高度板今晨-6%"}, fragility_score=0.7)
    assert out["market_state_prob"]["S6"] > out["market_state_prob"]["S3"]
    assert out["risk_off"] == (out["dominant_state"] in mt.STATE_RISK_OFF)


def test_crowding_lifts_climax_state():
    base = mt.classify_market_state({"tier": "加速", "retreat_signal": None})
    crowded = mt.classify_market_state({"tier": "加速", "retreat_signal": None}, crowding_score=0.9)
    assert crowded["market_state_prob"]["S4"] > base["market_state_prob"]["S4"]


def test_sector_rotation_raises_divergence_state():
    temp = {"tier": "加速", "retreat_signal": None}
    rotated = mt.classify_market_state(
        temp, sector_rotation={"weakening_ratio": 0.5, "emerging_ratio": 0.3})
    base = mt.classify_market_state(temp)
    assert rotated["market_state_prob"]["S5"] > base["market_state_prob"]["S5"]


def test_state_hysteresis_holds_when_advantage_thin():
    # base=S3(加速) 但拥挤把 S4 抬到接近 S3；上一日已是 S4 → 优势不足不切回 S3
    out = mt.classify_market_state(
        {"tier": "加速", "retreat_signal": None}, crowding_score=0.9, previous_state="S4")
    assert out["raw_dominant_state"] == "S3"
    assert out["dominant_state"] == "S4"
    assert out["switched"] is False


def test_state_switches_when_advantage_clear():
    out = mt.classify_market_state({"tier": "冰点", "retreat_signal": None}, previous_state="S4")
    assert out["dominant_state"] == "S0"
    assert out["switched"] is True
    assert out["risk_off"] is True
