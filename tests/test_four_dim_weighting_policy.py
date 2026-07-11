"""Four-dimension policy: do not let sentiment dominate normal scoring."""

from datetime import datetime

import four_dim_scorer as fds
from paths import data_file
from state_store import atomic_write_json


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


def test_real_strategy_ids_resolve_to_lane_weights():
    daban = fds.resolve_weights("daban:first_board_reseal")
    trend = fds.resolve_weights("trend_pullback")

    assert round(daban["sentiment"], 2) == 0.35
    assert round(trend["technical"], 2) == 0.35
    assert round(trend["deep"], 2) == 0.30


def test_historical_reference_uses_recent_window_and_sector(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    hist_path = data_file("stock-triage", "signal_history.json")
    atomic_write_json(
        hist_path,
        [
            {
                "code": "600001",
                "name": "旧样本",
                "grade": "A",
                "strategy_id": "trend_pullback",
                "sector": "半导体",
                "signal_date": "2026-04-01",
                "outcome": "win",
                "t1_close_ret": 8.0,
            },
            {
                "code": "600002",
                "name": "近期胜",
                "grade": "A",
                "strategy_id": "trend_pullback",
                "sector": "半导体",
                "signal_date": "2026-06-10",
                "outcome": "win",
                "t1_close_ret": 3.0,
            },
            {
                "code": "600003",
                "name": "近期负",
                "grade": "A",
                "strategy_id": "trend_pullback",
                "sector": "半导体",
                "signal_date": "2026-06-11",
                "outcome": "loss",
                "t1_close_ret": -2.0,
            },
        ],
    )

    ref = fds._load_historical_reference(
        "A",
        "trend_pullback",
        sector="半导体",
        now=datetime(2026, 6, 18),
    )

    assert ref["window_days"] == 30
    assert ref["grade_samples"] == 2
    assert ref["strategy_samples"] == 2
    assert ref["sector_samples"] == 2
    assert ref["grade_win_rate"] == 50.0


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


def _mock_four_dims(monkeypatch, change_pct):
    monkeypatch.setattr(fds, "score_technical",
                        lambda *a, **k: {"score": 9.0, "ma5": 10.0, "price": 10.0, "detail": "s"})
    monkeypatch.setattr(fds, "score_sentiment",
                        lambda *a, **k: {"score": 9.0, "change_pct": change_pct, "detail": "涨停"})
    monkeypatch.setattr(fds, "score_catalyst",
                        lambda *a, **k: {"score": 8.0, "available": True, "news_count": 3, "detail": "ok"})
    monkeypatch.setattr(fds, "score_deep",
                        lambda *a, **k: {"score": 8.0, "source": "serenity_deep", "pe": 30.0, "detail": "ok"})


def test_chase_limitup_gate_suppresses_daban_lane(monkeypatch, tmp_path):
    # daban 通道追当日已涨停票 → 追涨停护栏触发、降级（issue #28 证伪结论落地）
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _mock_four_dims(monkeypatch, change_pct=10.0)
    result = fds.score_stock("600001", "测试股", quote=_quote(), klines=_klines(),
                             market_ctx={}, strategy_id="daban:first_board_reseal")
    assert "chase_limitup_negative_ev" in result["score_gates"]
    assert result["grade"] not in ("S", "A")   # 追涨停被抑制，不给高评级


def test_chase_gate_skips_trend_lane_on_limit_up(monkeypatch, tmp_path):
    # trend 通道即使当日涨停也不触发（trend 不追板，护栏只管 daban）
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _mock_four_dims(monkeypatch, change_pct=10.0)
    result = fds.score_stock("600001", "测试股", quote=_quote(), klines=_klines(),
                             market_ctx={}, strategy_id="trend_pullback")
    assert "chase_limitup_negative_ev" not in result["score_gates"]


def test_chase_gate_skips_daban_when_not_limit_up(monkeypatch, tmp_path):
    # daban 通道但当日未涨停(+5%) → 不触发，正常打板预判不受影响
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _mock_four_dims(monkeypatch, change_pct=5.0)
    result = fds.score_stock("600001", "测试股", quote=_quote(), klines=_klines(),
                             market_ctx={}, strategy_id="daban:first_board_reseal")
    assert "chase_limitup_negative_ev" not in result["score_gates"]


def test_raw_four_dim_score_is_explicitly_research_only(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _mock_four_dims(monkeypatch, change_pct=5.0)

    result = fds.score_stock(
        "600001",
        "测试股",
        quote=_quote(),
        klines=_klines(),
        market_ctx={},
        strategy_id="trend_pullback",
    )

    assert result["directional_ready"] is False
    assert result["execution_action"] == "none"
    assert "研究" in result["advice"]
    assert "推荐" not in result["advice"]


def test_short_term_score_never_emits_execution_instruction(monkeypatch):
    bars = [
        {"close": 10.0, "high": 10.2, "low": 9.8, "volume": 1000}
        for _ in range(30)
    ]
    monkeypatch.setattr(
        fds,
        "fetch_tencent_realtime",
        lambda *args, **kwargs: {"price": 10.0, "change_pct": 1.0},
    )
    monkeypatch.setattr(fds, "fetch_tencent_kline", lambda *args, **kwargs: bars)
    monkeypatch.setattr(fds, "calc_ma", lambda values, period: [9.5] * len(values))
    monkeypatch.setattr(
        fds,
        "calc_macd",
        lambda values, **kwargs: (
            [0.0] * (len(values) - 2) + [0.0, 1.0],
            [0.0] * (len(values) - 2) + [0.5, 0.5],
            [0.0] * len(values),
        ),
    )
    monkeypatch.setattr(fds, "calc_rsi", lambda values, period: [20.0] * len(values))
    monkeypatch.setattr(fds, "calc_volume_ratio", lambda values: 2.0)
    monkeypatch.setattr(fds, "calc_atr", lambda *args, **kwargs: [1.0] * len(bars))
    monkeypatch.setattr(fds, "_chan", None)

    result = fds.score_short_term_entry("600001", "测试股")

    assert result["directional_ready"] is False
    assert result["execution_action"] == "none"
    assert result["suggestion"] == "等待完整政策复核"
    assert "immediate_buy" not in result["entry_conditions"]
    assert all("执行" not in item["label"] for item in result["entry_conditions"].values())
