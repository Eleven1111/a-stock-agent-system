"""四维情绪面接入情绪上下文 + score_stock 大盘 overlay 端到端。"""

import four_dim_scorer as fds


_QUOTE = {"price": 11.0, "change_pct": 10.0, "pe": 30.0,
          "market_cap": 100.0, "turnover": 12.0, "amount": 5e8}


def test_sentiment_with_ctx_boost(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ctx = {"lianban_ladder": {"002156": {"lianban": 2, "sector": "半导体", "seal_yi": 1.5}},
           "sector_limitups": {"半导体": 5}}
    out = fds.score_sentiment("002156", "通富微电", quote=dict(_QUOTE), signal_ctx=ctx)
    base = fds.score_sentiment("002156", "通富微电", quote=dict(_QUOTE), signal_ctx={})
    assert out["context_boost"] == 3.0  # 1.5+0.5+1.0
    assert out["score"] >= base["score"]
    assert out["sector"] == "半导体"


def test_sentiment_without_ctx_unchanged(tmp_path, monkeypatch):
    """无上下文时行为与历史一致：涨停+3、换手>10 +0.5 → 8.5。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    out = fds.score_sentiment("002156", "通富微电", quote=dict(_QUOTE), signal_ctx={})
    assert out["score"] == 8.5
    assert out["context_boost"] == 0.0
    assert out["social_attention_delta"] == 0.0


def test_sentiment_social_attention_is_bounded_and_auditable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ctx = {
        "social_attention": {
            "schema": "social_attention_snapshot_v1",
            "stocks": {
                "002156": {
                    "attention_score": 92.0,
                    "attention_velocity": 60.0,
                    "cross_source_count": 2,
                    "eligible_for_boost": True,
                    "crowding_risk": "high",
                    "price_change_pct": 5.2,
                }
            },
        }
    }
    out = fds.score_sentiment(
        "002156",
        "通富微电",
        quote=dict(_QUOTE),
        signal_ctx=ctx,
    )

    assert out["social_attention_delta"] == 0.5
    assert out["social_attention"]["cross_source_count"] == 2
    assert "社会关注" in out["detail"]


def test_score_stock_applies_market_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    klines = [{"close": 10.0, "high": 10.2, "low": 9.8, "volume": 1000} for _ in range(60)]
    monkeypatch.setattr(fds, "fetch_tencent_kline", lambda *a, **k: klines)
    monkeypatch.setattr(fds, "fetch_serper_news", lambda *a, **k: None)

    risk_off = {"alerts": [{"level": "🔴 高", "sectors": ["全市场"], "msg": "VIX 35"}],
                "sector_impact": {"AI算力": -3, "半导体": -3, "全市场": -3},
                "summary": "risk off"}
    res = fds.score_stock("002156", "通富微电", quote=dict(_QUOTE), klines=klines,
                          market_ctx=risk_off)
    assert res["market_overlay"]["regime"] == "risk_off"
    assert "大盘承压" in res["advice"]

    res2 = fds.score_stock("002156", "通富微电", quote=dict(_QUOTE), klines=klines,
                           market_ctx={"alerts": [], "sector_impact": {}, "summary": ""})
    assert res2["market_overlay"]["regime"] == "neutral"
    assert "大盘承压" not in res2["advice"]
