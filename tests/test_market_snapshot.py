import json
import os

import market_snapshot


def test_snapshot_is_immutable_and_versioned(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    payload = {
        "schema": "quotes_v1",
        "items": [
            {
                "code": "002156",
                "price": 11.2,
                "provider": "tencent",
                "fetched_at": "2026-06-12T09:35:01+08:00",
            }
        ],
    }

    first = market_snapshot.write_snapshot(
        "open-confirmation",
        payload,
        trading_date="2026-06-12",
        batch_id="a-share-20260612",
        producer="open-confirmation",
        producer_version="commit-123",
        source_versions={"tencent": "quote-v1"},
    )
    second = market_snapshot.write_snapshot(
        "open-confirmation",
        payload,
        trading_date="2026-06-12",
        batch_id="a-share-20260612",
        producer="open-confirmation",
        producer_version="commit-123",
        source_versions={"tencent": "quote-v1"},
    )

    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["payload_hash"] == second["payload_hash"]
    assert first["source_versions"] == {"tencent": "quote-v1"}
    assert first["producer_version"] == "commit-123"
    assert os.path.exists(first["snapshot_path"])
    assert json.load(open(first["snapshot_path"], encoding="utf-8"))["payload"] == payload


def test_changed_payload_creates_new_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    common = {
        "dataset": "global-preopen",
        "trading_date": "2026-06-12",
        "batch_id": "a-share-20260612",
        "producer": "global-preopen",
    }

    first = market_snapshot.write_snapshot(payload={"price": 1}, **common)
    second = market_snapshot.write_snapshot(payload={"price": 2}, **common)

    assert first["snapshot_id"] != second["snapshot_id"]
