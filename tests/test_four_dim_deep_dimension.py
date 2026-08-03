"""四维深度面 — Serenity 回流接入（fresh / 回退PE / 过期衰减）。"""

from datetime import date, timedelta

import deep_research_cache as drc
import four_dim_scorer as fds


def _fake_quote(pe=20.0, cap=300.0):
    def _f(code, market="sz"):
        return {"price": 10.0, "pe": pe, "market_cap": cap}
    return _f


def test_score_deep_uses_fresh_serenity(tmp_path, monkeypatch):
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试状态
    # 无条件设置了它，这里要用 HERMES_HOME 驱动本用例的 tmp_path，
    # 必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(fds, "fetch_tencent_realtime", _fake_quote(pe=20.0))
    drc.write_deep_research("002156", "通富微电",
                            {"total": 82.0, "rating": "谨慎看多（非投资建议）", "dimensions": {}},
                            asof=date.today().isoformat())
    out = fds.score_deep("002156", "通富微电")
    assert out["source"] == "serenity_deep"
    assert out["score"] == 8.2
    assert out["stale"] is False


def test_score_deep_fallback_pe_snapshot(tmp_path, monkeypatch):
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试状态
    # 无条件设置了它，这里要用 HERMES_HOME 驱动本用例的 tmp_path，
    # 必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(fds, "fetch_tencent_realtime", _fake_quote(pe=12.0))  # 0<pe<15 → 7.0
    out = fds.score_deep("600011", "华能国际")
    assert out["source"] == "valuation_snapshot"
    assert out["score"] == 7.0


def test_score_deep_stale_decays_toward_pe(tmp_path, monkeypatch):
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试状态
    # 无条件设置了它，这里要用 HERMES_HOME 驱动本用例的 tmp_path，
    # 必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    # 过渡带：max_age_days(90) < age <= max_age_days + exclude_after_extra_days(90+30=120)。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(fds, "fetch_tencent_realtime", _fake_quote(pe=12.0))  # pe_score=7.0
    old = (date.today() - timedelta(days=105)).isoformat()  # extra=15 → t=1/6
    drc.write_deep_research("000021", "深科技",
                            {"total": 90.0, "rating": "强烈看多（非投资建议）", "dimensions": {}},
                            asof=old)
    out = fds.score_deep("000021", "深科技")
    assert out["source"] == "serenity_deep_stale"
    assert out["score"] == 8.7  # 9.0 + (7.0-9.0)*(15/90)
    assert out["stale"] is True
    assert not out.get("excluded")


def test_score_deep_severe_stale_excludes_from_weighting(tmp_path, monkeypatch):
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试状态
    # 无条件设置了它，这里要用 HERMES_HOME 驱动本用例的 tmp_path，
    # 必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    # 重度过期：age > max_age_days + exclude_after_extra_days(120) → 退出加权。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(fds, "fetch_tencent_realtime", _fake_quote(pe=12.0))
    old = (date.today() - timedelta(days=135)).isoformat()  # extra=45 > 30
    drc.write_deep_research("000021", "深科技",
                            {"total": 90.0, "rating": "强烈看多（非投资建议）", "dimensions": {}},
                            asof=old)
    out = fds.score_deep("000021", "深科技")
    assert out["source"] == "serenity_deep_excluded"
    assert out["excluded"] is True
    assert out["stale"] is True
    assert out["score"] == 9.0  # 原始分保留供展示，不做衰减（已退出加权）
    assert "已退出加权" in out["detail"]


def test_score_deep_boundary_at_severe_threshold_still_counts(tmp_path, monkeypatch):
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试状态
    # 无条件设置了它，这里要用 HERMES_HOME 驱动本用例的 tmp_path，
    # 必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    # 边界：age == max_age_days + exclude_after_extra_days(120) 仍属过渡带，未退出。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(fds, "fetch_tencent_realtime", _fake_quote(pe=12.0))
    old = (date.today() - timedelta(days=120)).isoformat()  # extra=30 (衰减周期 max_age_days=90)
    drc.write_deep_research("000021", "深科技",
                            {"total": 90.0, "rating": "强烈看多（非投资建议）", "dimensions": {}},
                            asof=old)
    out = fds.score_deep("000021", "深科技")
    assert out["source"] == "serenity_deep_stale"
    assert not out.get("excluded")
    assert out["score"] == 8.3  # 9.0 + (7.0-9.0)*(30/90)，仍在过渡带内满权重计入
