"""批量四维打分 — 批量预取行情 + 线程池并行 + 顺序保持 + 失败隔离。"""

from datetime import date, timedelta

import batch_four_dim_scorer as batch
from state_store import atomic_write_json


def test_score_targets_prefetch_inject_and_order(monkeypatch):
    calls = {}

    def fake_score(code, name, quote=None, klines=None):
        calls[code] = quote
        return {"code": code, "name": name, "weighted": 7, "grade": "A",
                "confidence": "high", "advice": "x"}

    monkeypatch.setattr(batch.four_dim_scorer, "score_stock", fake_score)
    monkeypatch.setattr(batch, "_prefetch_quotes",
                        lambda targets: {"sh600011": {"price": 9.1}})

    targets = [("600011", "华能国际"), ("002156", "通富微电")]
    out = batch.score_targets(targets)

    assert out["target_count"] == 2
    assert [r["code"] for r in out["results"]] == ["600011", "002156"]  # map 保持顺序
    assert calls["600011"] == {"price": 9.1}   # 复用批量预取
    assert calls["002156"] is None             # 预取未命中 → 传 None 自抓
    assert out["signal_count"] == 2


def test_score_targets_failure_isolated(monkeypatch):
    def boom(code, name, quote=None, klines=None):
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
        batch.data_file("stock-triage", "candidate_pool_latest.json"),
        {
            "status": "ready",
            "asof": stale,
            "candidates": [{"code": "600011", "name": "华能国际"}],
        },
    )

    assert batch.load_pool_targets() == []
