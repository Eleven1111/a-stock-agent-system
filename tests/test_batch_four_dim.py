"""批量四维打分 — 批量预取行情 + 线程池并行 + 顺序保持 + 失败隔离。"""

from datetime import date, timedelta

import batch_four_dim_scorer as batch
from state_store import atomic_write_json


def test_score_targets_prefetch_inject_and_order(monkeypatch):
    calls = {}

    def fake_score(code, name, quote=None, klines=None, strategy_id="four_dim", **kwargs):
        calls[code] = {"quote": quote, "strategy_id": strategy_id}
        return {"code": code, "name": name, "weighted": 7, "grade": "A",
                "confidence": "high", "advice": "x"}

    monkeypatch.setattr(batch.four_dim_scorer, "score_stock", fake_score)
    monkeypatch.setattr(batch, "_prefetch_quotes",
                        lambda targets: {"sh600011": {"price": 9.1}})

    targets = [
        {"code": "600011", "name": "华能国际", "selected_by": {"daban": True}},
        {"code": "002156", "name": "通富微电", "selected_by": {"trend": True}},
    ]
    out = batch.score_targets(targets)

    assert out["target_count"] == 2
    assert [r["code"] for r in out["results"]] == ["600011", "002156"]  # map 保持顺序
    assert calls["600011"]["quote"] == {"price": 9.1}   # 复用批量预取
    assert calls["600011"]["strategy_id"] == "daban:first_board_reseal"
    assert calls["002156"]["quote"] is None             # 预取未命中 → 传 None 自抓
    assert calls["002156"]["strategy_id"] == "trend_pullback"
    assert out["signal_count"] == 0
    assert out["signals"] == []
    assert out["research_candidate_count"] == 2


def test_high_grade_scores_remain_research_only_without_policy_decision(monkeypatch):
    """Raw factor scores must never become directional cron signals by themselves."""

    monkeypatch.setattr(
        batch.four_dim_scorer,
        "score_stock",
        lambda code, name, **kwargs: {
            "code": code,
            "name": name,
            "weighted": 9.0,
            "grade": "S",
            "confidence": "high",
            "advice": "强烈推荐",
        },
    )
    monkeypatch.setattr(batch, "_prefetch_quotes", lambda targets: {})

    out = batch.score_targets([("600011", "华能国际")])

    assert out["signals"] == []
    assert out["signal_count"] == 0
    assert [item["code"] for item in out["research_candidates"]] == ["600011"]
    assert out["research_candidates"][0]["directional_ready"] is False
    assert out["research_candidates"][0]["execution_action"] == "none"


def test_score_targets_failure_isolated(monkeypatch):
    def boom(code, name, quote=None, klines=None, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(batch.four_dim_scorer, "score_stock", boom)
    monkeypatch.setattr(batch, "_prefetch_quotes", lambda targets: {})

    out = batch.score_targets([("600011", "华能国际")])
    assert out["results"][0]["status"] == "failed"
    assert "network down" in out["results"][0]["error"]


def test_prefetch_quotes_swallows_errors(monkeypatch):
    # _prefetch_quotes 内部抓取失败应回退空 dict，不抛
    import a_stock_http
    monkeypatch.setattr(a_stock_http, "fetch_tencent_quote",
                        lambda codes: (_ for _ in ()).throw(RuntimeError("boom")))
    assert batch._prefetch_quotes([("600011", "华能国际")]) == {}


def test_empty_dynamic_pool_fails_closed():
    out = batch.score_targets([])

    assert out["status"] == "insufficient_data"
    assert out["target_count"] == 0
    assert out["signals"] == []


def test_load_pool_targets_rejects_stale_pool(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    stale = (date.today() - timedelta(days=1)).isoformat()
    atomic_write_json(
        batch._dated_pool_path(date.today().isoformat()),
        {
            "status": "ready",
            "asof": stale,
            "candidates": [{"code": "600011", "name": "华能国际"}],
        },
    )

    assert batch.load_pool_targets() == []


def test_load_pool_targets_uses_dated_snapshot_and_balances_lanes(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    asof = "2026-07-02"
    candidates = [
        {
            "code": f"6000{index:02d}", "name": str(index),
            "trend_rank": index + 1, "trend_score": 9 - index,
            "daban_rank": 6 - index, "daban_score": 4 + index,
        }
        for index in range(6)
    ]
    atomic_write_json(batch._dated_pool_path(asof), {
        "status": "ready", "asof": asof, "candidates": candidates,
    })
    atomic_write_json(batch.data_file("stock-triage", "candidate_pool_latest.json"), {
        "status": "ready", "asof": asof, "candidates": [],
    })

    targets = batch.load_pool_targets(limit=6, asof=asof)

    assert [target["research_lane"] for target in targets].count("trend") == 3
    assert [target["research_lane"] for target in targets].count("daban") == 3
    assert {target["strategy_id"].split(":")[0] for target in targets} == {"trend_pullback", "daban"}


def test_cache_only_scoring_never_prefetches_or_falls_back_to_network(monkeypatch):
    calls = {}

    monkeypatch.setattr(
        batch,
        "_prefetch_quotes",
        lambda targets: (_ for _ in ()).throw(AssertionError("network prefetch")),
    )
    monkeypatch.setattr(
        batch.local_market_history,
        "get_daily_bars",
        lambda codes, end_date, lookback, adjust_flag="qfq": [
            {"code": codes[0], "trading_date": "2026-07-02", "close": 10.0}
        ],
    )

    def fake_score(code, name, **kwargs):
        calls.update(kwargs)
        return {"code": code, "name": name, "weighted": 5, "grade": "B", "confidence": "low"}

    monkeypatch.setattr(batch.four_dim_scorer, "score_stock", fake_score)
    out = batch.score_targets(
        [{"code": "600011", "name": "华能国际", "price": 10.0, "provider": "candidate_snapshot"}],
        cache_only=True,
        asof="2026-07-02",
    )

    assert out["cache_only"] is True
    assert out["research_only"] is True
    assert out["live_effect"] == "none"
    assert calls["cache_only"] is True
    assert calls["asof"] == "2026-07-02"
    assert calls["quote"]["price"] == 10.0
    assert calls["klines"][0]["date"] == "2026-07-02"
