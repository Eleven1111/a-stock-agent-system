from __future__ import annotations

import pytest
from state_store import atomic_write_json

from scripts import portfolio_risk_precompute as precompute


def test_network_empty_histories_emit_degraded_fail_closed_bundle(tmp_path, monkeypatch):
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    asof = "2026-07-20"
    atomic_write_json(
        precompute.shortlist_path(asof),
        {"asof": asof, "status": "ready", "shortlist": [{"code": "sh600001"}]},
    )
    atomic_write_json(
        precompute.data_file("stock-triage", "portfolio.json"),
        {"cash": 100_000, "positions": []},
    )
    monkeypatch.setattr(
        precompute,
        "fetch_tencent_kline",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            precompute.DataSourceError("tencent", "unavailable")
        ),
    )

    result = precompute.build_batch(asof)

    assert result["status"] == "degraded"
    assert result["complete_count"] == 0
    assert result["evidence_by_code"]["600001"]["coverage"] < 0.95


def test_historical_risk_live_fetch_is_blocked():
    with pytest.raises(precompute.DataSourceError, match="replay"):
        precompute._require_same_day_live("2026-07-20")
