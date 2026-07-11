"""大盘 context 回流 — 缓存读写 / 态势判定 / overlay 降档 / no-op。"""

from datetime import datetime, timedelta

import market_context as mc


def _impact(score_map=None, alerts=None):
    return {"alerts": alerts or [], "sector_impact": score_map or {}, "summary": "x"}


def test_write_read_roundtrip(tmp_path, monkeypatch):
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试状态
    # 无条件设置了它，这里要用 HERMES_HOME 驱动本用例的 tmp_path，
    # 必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mc.write_market_context(_impact({"半导体": -3}))
    ctx = mc.read_market_context()
    assert ctx is not None
    assert ctx["sector_impact"]["半导体"] == -3


def test_read_expired_returns_stale_state(tmp_path, monkeypatch):
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试状态
    # 无条件设置了它，这里要用 HERMES_HOME 驱动本用例的 tmp_path，
    # 必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mc.write_market_context(_impact({"半导体": -3}))
    future = datetime.now() + timedelta(hours=30)
    ctx = mc.read_market_context(max_age_hours=18, now=future)
    assert ctx["context_status"] == "stale"
    assert ctx["context_fresh"] is False
    assert mc.market_regime(ctx)["regime"] == "stale"


def test_read_missing_returns_unknown_state(tmp_path, monkeypatch):
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试状态
    # 无条件设置了它，这里要用 HERMES_HOME 驱动本用例的 tmp_path，
    # 必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ctx = mc.read_market_context()
    assert ctx["context_status"] == "unknown"
    assert ctx["context_fresh"] is False
    assert mc.market_regime(ctx)["regime"] == "unknown"


def test_read_invalid_timestamp_returns_unknown_state(tmp_path, monkeypatch):
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mc.atomic_write_json(
        mc.context_file(),
        {
            "schema": "market_context_v1",
            "generated_at": "not-a-timestamp",
            "status": "ok",
        },
    )

    ctx = mc.read_market_context()

    assert ctx["context_status"] == "unknown"
    assert ctx["context_fresh"] is False
    assert "generated_at" in ctx["unavailable_reason"]


def test_regime_risk_off_by_score():
    ctx = _impact({"AI算力": -3, "半导体": -3, "消费电子": -2})
    assert mc.market_regime(ctx)["regime"] == "risk_off"


def test_regime_risk_off_by_market_wide_red():
    ctx = _impact({"全市场": -1},
                  alerts=[{"level": "🔴 高", "sectors": ["全市场"], "msg": "VIX极度恐慌"}])
    assert mc.market_regime(ctx)["regime"] == "risk_off"


def test_regime_neutral_and_risk_on():
    assert mc.market_regime(_impact({"半导体": 1}))["regime"] == "neutral"
    assert mc.market_regime(_impact({"半导体": 4, "AI算力": 4}))["regime"] == "risk_on"
    assert mc.market_regime(None)["regime"] == "unknown"


def test_regime_treats_upstream_error_as_unknown():
    regime = mc.market_regime({"status": "error", "sector_impact": {}})

    assert regime["regime"] == "unknown"
    assert regime["score"] is None


def test_overlay_downgrades_on_risk_off():
    result = {"grade": "S", "advice": "强烈推荐"}
    out = mc.apply_market_overlay(result, _impact({"AI算力": -4, "半导体": -4}))
    assert out["grade"] == "A"
    assert "大盘承压" in out["advice"]
    assert out["market_overlay"]["grade_from"] == "S"
    assert result["grade"] == "S"  # 不 mutate 入参


def test_overlay_fails_closed_without_ctx():
    result = {"grade": "A", "advice": "推荐"}
    out = mc.apply_market_overlay(result, None)
    assert out["grade"] == "D"
    assert out["market_overlay"]["regime"] == "unknown"
    assert "仅供研究" in out["advice"]
    assert result == {"grade": "A", "advice": "推荐"}


def test_downgrade_floor_is_d():
    assert mc.downgrade("D") == "D"
    assert mc.downgrade("C") == "D"
