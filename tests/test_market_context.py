"""大盘 context 回流 — 缓存读写 / 态势判定 / overlay 降档 / no-op。"""

from datetime import datetime, timedelta

import market_context as mc


def _impact(score_map=None, alerts=None):
    return {"alerts": alerts or [], "sector_impact": score_map or {}, "summary": "x"}


def test_write_read_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mc.write_market_context(_impact({"半导体": -3}))
    ctx = mc.read_market_context()
    assert ctx is not None
    assert ctx["sector_impact"]["半导体"] == -3


def test_read_expired_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mc.write_market_context(_impact({"半导体": -3}))
    future = datetime.now() + timedelta(hours=30)
    assert mc.read_market_context(max_age_hours=18, now=future) is None


def test_read_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert mc.read_market_context() is None


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
    assert mc.market_regime(None)["regime"] == "neutral"


def test_overlay_downgrades_on_risk_off():
    result = {"grade": "S", "advice": "强烈推荐"}
    out = mc.apply_market_overlay(result, _impact({"AI算力": -4, "半导体": -4}))
    assert out["grade"] == "A"
    assert "大盘承压" in out["advice"]
    assert out["market_overlay"]["grade_from"] == "S"
    assert result["grade"] == "S"  # 不 mutate 入参


def test_overlay_noop_without_ctx():
    result = {"grade": "A", "advice": "推荐"}
    out = mc.apply_market_overlay(result, None)
    assert out == result


def test_downgrade_floor_is_d():
    assert mc.downgrade("D") == "D"
    assert mc.downgrade("C") == "D"
