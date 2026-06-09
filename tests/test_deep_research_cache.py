"""深度投研缓存 — 评分映射 / 新鲜度衰减 / 读写往返。"""

from datetime import date, timedelta

import deep_research_cache as drc


def test_scorecard_to_deep_score_basic():
    sc = {"total": 70.0, "rating": "谨慎看多（非投资建议）",
          "dimensions": {"industry_space": {"score_1_to_5": 4}}}
    out = drc.scorecard_to_deep_score(sc)
    assert out["score"] == 7.0
    assert out["base"] == 7.0
    assert out["rating"].startswith("谨慎看多")
    assert out["dimensions"]["industry_space"] == 4


def test_scorecard_missing_total_returns_none_score():
    out = drc.scorecard_to_deep_score({"rating": "x"})
    assert out["score"] is None


def test_valuation_adjustment_bands():
    assert drc.valuation_adjustment(None) == 0.0
    assert drc.valuation_adjustment(60) == 0.6
    assert drc.valuation_adjustment(25) == 0.3
    assert drc.valuation_adjustment(0) == 0.0
    assert drc.valuation_adjustment(-10) == -0.3
    assert drc.valuation_adjustment(-30) == -0.6


def test_scorecard_with_valuation_base_upside():
    sc = {"total": 60.0, "rating": "中性观察", "dimensions": {}}
    val = {"scenarios": [{"scenario": "base", "upside_downside_pct": 55.0}]}
    out = drc.scorecard_to_deep_score(sc, val)
    assert out["upside_pct"] == 55.0
    assert out["score"] == 6.6  # 6.0 + 0.6


def test_decay_stale_score_fresh():
    assert drc.decay_stale_score(8.0, 5.0, age_days=10, max_age_days=90) == 8.0


def test_decay_stale_score_half_decay():
    # age = max + 45 → extra=45, t=0.5 → 8 + (5-8)*0.5 = 6.5
    assert drc.decay_stale_score(8.0, 5.0, age_days=135, max_age_days=90) == 6.5


def test_decay_stale_score_full_decay():
    # extra >= max → t=1 → fallback
    assert drc.decay_stale_score(8.0, 5.0, age_days=300, max_age_days=90) == 5.0


def test_write_read_roundtrip_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sc = {"total": 82.0, "rating": "谨慎看多（非投资建议）",
          "dimensions": {"valuation_odds": {"score_1_to_5": 3}}}
    drc.write_deep_research("002156", "通富微电", sc, asof=date.today().isoformat())
    rec = drc.read_deep_research("002156")
    assert rec is not None
    assert rec["found"] is True
    assert rec["stale"] is False
    assert rec["deep_score"] == 8.2
    assert rec["code"] == "002156"
    assert rec["dimensions"]["valuation_odds"] == 3


def test_read_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    old = (date.today() - timedelta(days=200)).isoformat()
    drc.write_deep_research("600011", "华能国际",
                            {"total": 70, "rating": "中性观察", "dimensions": {}}, asof=old)
    rec = drc.read_deep_research("600011", max_age_days=90)
    assert rec["stale"] is True
    assert rec["age_days"] >= 200


def test_read_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert drc.read_deep_research("000001") is None
