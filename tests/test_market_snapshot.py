import json
import os

import market_snapshot
import pytest


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


def test_materialized_input_is_read_back_from_immutable_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    payload = {"schema": "candidate_inputs_v1", "quotes": {"600001": {"price": 10.0}}}

    record = market_snapshot.materialize_input_snapshot(
        "candidate-discovery-input",
        payload,
        trading_date="2026-06-12",
        batch_id="a-share-20260612",
        producer="candidate-discovery",
        source_versions={"tencent": "quote-v1"},
    )

    assert record["payload"] == payload
    assert market_snapshot.read_snapshot(record["snapshot_path"])["payload"] == payload
    assert record["consumed_from_snapshot"] is True


def _strict_snapshot_kwargs():
    return {
        "captured_at": "2026-07-28T14:55:03+08:00",
        "event_asof": "2026-07-28",
        "evidence_time": "2026-07-28T14:55:00+08:00",
        "decision_mode": "live",
        "stage_policy": market_snapshot.build_stage_policy(
            stage="tail-close-signal",
            cutoff_time="14:57:00",
            timezone_name="Asia/Shanghai",
        ),
        "event_time": "2026-07-28T14:55:00+08:00",
        "available_time": "2026-07-28T14:55:02+08:00",
        "watermark": {
            "coverage_asof": "2026-07-28T14:55:00+08:00",
            "provider_published_at": "2026-07-28T14:55:01+08:00",
            "complete": True,
        },
        "sealed_at": "2026-07-28T14:55:03+08:00",
        "max_clock_drift_seconds": 5,
    }


def _write_strict_snapshot(tmp_path, monkeypatch, **overrides):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    kwargs = _strict_snapshot_kwargs()
    kwargs.update(overrides)
    return market_snapshot.write_snapshot(
        "tail-close-signal-input",
        {"schema": "tail_close_inputs_v1", "quotes": [{"code": "600000", "price": 10.2}]},
        trading_date="2026-07-28",
        batch_id="tail-close-20260728",
        producer="tail-close-signal",
        producer_version="commit-abc",
        source_versions={"tencent": "quote-v1"},
        **kwargs,
    )


def test_legacy_snapshot_identity_ignores_capture_time(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    common = {
        "dataset": "legacy-input",
        "payload": {"price": 10.0},
        "trading_date": "2026-07-28",
        "batch_id": "legacy-20260728",
        "producer": "legacy-producer",
    }

    first = market_snapshot.write_snapshot(
        captured_at="2026-07-28T14:50:00+08:00",
        **common,
    )
    second = market_snapshot.write_snapshot(
        captured_at="2026-07-28T14:56:00+08:00",
        **common,
    )

    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["point_in_time"] is None
    assert "seal_hash" not in first


def test_legacy_point_in_time_shape_remains_unchanged():
    stage_policy = market_snapshot.build_stage_policy(
        stage="open-confirmation",
        cutoff_time="09:35:00",
        timezone_name="Asia/Shanghai",
    )

    point_in_time = market_snapshot.validate_point_in_time(
        event_asof="2026-07-28",
        evidence_time="2026-07-28T09:34:00+08:00",
        captured_at="2026-07-28T09:35:00+08:00",
        decision_mode="live",
        stage_policy=stage_policy,
    )

    assert point_in_time == {
        "schema": market_snapshot.PIT_STAGE_SCHEMA,
        "decision_mode": "live",
        "event_asof": "2026-07-28",
        "evidence_time": "2026-07-28T09:34:00+08:00",
        "captured_at": "2026-07-28T09:35:00+08:00",
        "stage_policy": stage_policy,
        "available_evidence_cutoff": "2026-07-28T09:35:00+08:00",
    }


def test_strict_point_in_time_snapshot_is_sealed_and_readable(tmp_path, monkeypatch):
    record = _write_strict_snapshot(
        tmp_path,
        monkeypatch,
        watermark={
            "coverage_asof": "2026-07-28T14:55:00+08:00",
            "provider_published_at": "2026-07-28T14:55:01+08:00",
            "complete": True,
            "provider_sequence": 12345,
        },
    )

    assert record["point_in_time"]["event_time"] == "2026-07-28T14:55:00+08:00"
    assert record["point_in_time"]["available_time"] == "2026-07-28T14:55:02+08:00"
    assert record["point_in_time"]["watermark"]["complete"] is True
    assert record["point_in_time"]["watermark"]["provider_sequence"] == 12345
    assert record["point_in_time"]["sealed_at"] == "2026-07-28T14:55:03+08:00"
    assert record["point_in_time"]["seal_hash"] == record["seal_hash"]
    assert len(record["seal_hash"]) == 64
    assert market_snapshot.read_snapshot(record)["seal_hash"] == record["seal_hash"]


def test_strict_fields_change_snapshot_identity(tmp_path, monkeypatch):
    first = _write_strict_snapshot(tmp_path, monkeypatch)
    second = _write_strict_snapshot(
        tmp_path,
        monkeypatch,
        event_time="2026-07-28T14:54:59+08:00",
        watermark={
            "coverage_asof": "2026-07-28T14:54:59+08:00",
            "provider_published_at": "2026-07-28T14:55:01+08:00",
            "complete": True,
        },
    )

    assert first["snapshot_id"] != second["snapshot_id"]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"available_time": "2026-07-28T14:57:01+08:00"}, "availability_after_cutoff"),
        (
            {
                "watermark": {
                    "coverage_asof": "2026-07-28T14:55:00+08:00",
                    "provider_published_at": "2026-07-28T14:55:01+08:00",
                    "complete": False,
                }
            },
            "watermark_incomplete",
        ),
        (
            {
                "available_time": "2026-07-28T14:55:20+08:00",
                "max_clock_drift_seconds": 5,
            },
            "clock_drift_exceeded",
        ),
        ({"max_clock_drift_seconds": float("nan")}, "clock_drift_limit_invalid"),
        ({"event_time": "2026-07-28T14:55:00"}, "event_time_timezone_missing"),
    ],
)
def test_strict_point_in_time_fail_closed(tmp_path, monkeypatch, overrides, reason):
    with pytest.raises(market_snapshot.PointInTimeViolation, match=reason):
        _write_strict_snapshot(tmp_path, monkeypatch, **overrides)


def test_strict_contract_rejects_partial_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    kwargs = _strict_snapshot_kwargs()
    kwargs.pop("watermark")

    with pytest.raises(
        market_snapshot.PointInTimeViolation,
        match="strict_point_in_time_contract_incomplete",
    ):
        market_snapshot.write_snapshot(
            "tail-close-signal-input",
            {"schema": "tail_close_inputs_v1"},
            trading_date="2026-07-28",
            batch_id="tail-close-20260728",
            producer="tail-close-signal",
            **kwargs,
        )


def test_read_snapshot_rejects_tampered_seal(tmp_path, monkeypatch):
    record = _write_strict_snapshot(tmp_path, monkeypatch)
    with open(record["snapshot_path"], encoding="utf-8") as handle:
        stored = json.load(handle)
    stored["point_in_time"]["available_time"] = "2026-07-28T14:54:59+08:00"
    with open(record["snapshot_path"], "w", encoding="utf-8") as handle:
        json.dump(stored, handle)

    with pytest.raises(ValueError, match="seal hash mismatch"):
        market_snapshot.read_snapshot(record)


def test_read_snapshot_rejects_removed_strict_contract(tmp_path, monkeypatch):
    record = _write_strict_snapshot(tmp_path, monkeypatch)
    with open(record["snapshot_path"], encoding="utf-8") as handle:
        stored = json.load(handle)
    stored["point_in_time"] = None
    stored.pop("seal_hash")
    with open(record["snapshot_path"], "w", encoding="utf-8") as handle:
        json.dump(stored, handle)

    with pytest.raises(ValueError, match="identity mismatch"):
        market_snapshot.read_snapshot(record)
