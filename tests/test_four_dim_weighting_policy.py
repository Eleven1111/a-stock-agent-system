"""Four-dimension policy: do not let sentiment dominate normal scoring."""

import four_dim_scorer as fds


def _quote():
    return {
        "price": 10.0,
        "change_pct": 10.0,
        "turnover": 25.0,
        "amount": 5e8,
        "pe": 30.0,
        "market_cap": 100.0,
    }


def _klines():
    return [{"close": 10.0, "high": 10.2, "low": 9.8, "volume": 1000} for _ in range(60)]


def test_normal_four_dim_weights_reduce_sentiment_and_raise_catalyst():
    assert fds.WEIGHTS == {
        "technical": 0.30,
        "sentiment": 0.15,
        "catalyst": 0.30,
        "deep": 0.25,
    }


def test_unavailable_catalyst_keeps_weight_and_does_not_amplify_sentiment(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        fds,
        "score_technical",
        lambda *args, **kwargs: {"score": 5.0, "ma5": 10.0, "price": 10.0, "detail": "flat"},
    )
    monkeypatch.setattr(
        fds,
        "score_sentiment",
        lambda *args, **kwargs: {"score": 10.0, "change_pct": 10.0, "detail": "limit-up"},
    )
    monkeypatch.setattr(
        fds,
        "score_catalyst",
        lambda *args, **kwargs: {
            "score": 4.5,
            "available": False,
            "news_count": 0,
            "detail": "catalyst unavailable",
        },
    )
    monkeypatch.setattr(
        fds,
        "score_deep",
        lambda *args, **kwargs: {"score": 5.0, "source": "valuation_snapshot", "pe": 30.0, "detail": "pe"},
    )

    result = fds.score_stock("600001", "测试股", quote=_quote(), klines=_klines(), market_ctx={})

    assert result["effective_weights"]["sentiment"] == "15%"
    assert result["effective_weights"]["catalyst"] == "30%"
    assert "catalyst" not in result["excluded_dims"]
    assert "catalyst" in result["degraded_dims"]
    assert result["weighted"] < 6.0


def test_weak_catalyst_and_deep_cap_normal_s_grade(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        fds,
        "score_technical",
        lambda *args, **kwargs: {"score": 10.0, "ma5": 10.0, "price": 10.0, "detail": "strong"},
    )
    monkeypatch.setattr(
        fds,
        "score_sentiment",
        lambda *args, **kwargs: {"score": 10.0, "change_pct": 10.0, "detail": "limit-up"},
    )
    monkeypatch.setattr(
        fds,
        "score_catalyst",
        lambda *args, **kwargs: {"score": 5.0, "available": True, "news_count": 0, "detail": "none"},
    )
    monkeypatch.setattr(
        fds,
        "score_deep",
        lambda *args, **kwargs: {"score": 10.0, "source": "serenity_deep", "pe": 30.0, "detail": "deep strong"},
    )

    result = fds.score_stock("600001", "测试股", quote=_quote(), klines=_klines(), market_ctx={})

    assert result["weighted"] >= 8.0
    assert result["grade"] != "S"
    assert "insufficient_catalyst_or_deep_for_s" in result["score_gates"]
