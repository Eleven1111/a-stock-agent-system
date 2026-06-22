import json
from datetime import date, datetime

import portfolio_research_history as history


def _confirmation(*, decision="watch", reason="strategy_unverified"):
    today = date.today().isoformat()
    generated_at = datetime.now().isoformat(timespec="seconds")
    return {
        "schema": "open_confirmation_v3",
        "asof": today,
        "generated_at": generated_at,
        "input_snapshot": {
            "snapshot_id": "snapshot-1",
            "payload_hash": "payload-hash",
            "source_versions": {"tencent": "v2", "cninfo": "v1"},
        },
        "signals": [{
            "code": "sh600001",
            "name": "研究候选",
            "strategy_id": "trend_pullback",
            "open_score": 88.0,
            "open_daban_score": 20.0,
            "open_trend_score": 88.0,
            "auction_score": 80.0,
            "decision": decision,
            "quality_report": {"status": "passed"},
            "policy_decision": {
                "requested_action": "buy",
                "decision": decision,
                "reasons": [reason],
            },
            "research_evidence": {"asof": today},
        }],
    }


def test_record_snapshot_preserves_research_intent_but_not_live_permission(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    saved = history.record_open_confirmation(_confirmation())

    assert saved["status"] == "recorded"
    snapshot = saved["snapshot"]
    candidate = snapshot["candidates"][0]
    assert candidate["decision"] == "buy"
    assert candidate["live_decision"] == "watch"
    assert candidate["eligible"] is True
    assert snapshot["source_versions"] == {"tencent": "v2", "cninfo": "v1"}


def test_non_research_policy_block_stays_ineligible(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    saved = history.record_open_confirmation(
        _confirmation(decision="watch", reason="market_risk_off")
    )

    candidate = saved["snapshot"]["candidates"][0]
    assert candidate["decision"] == "watch"
    assert candidate["eligible"] is False


def test_same_day_snapshot_is_idempotent_and_preserves_first_on_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    payload = _confirmation()

    first = history.record_open_confirmation(payload)
    second = history.record_open_confirmation(payload)
    changed = json.loads(json.dumps(payload))
    changed["signals"][0]["open_score"] = 99.0

    assert second["status"] == "reused"
    assert second["snapshot"]["snapshot_sha256"] == first["snapshot"]["snapshot_sha256"]
    conflict = history.record_open_confirmation(changed)

    assert conflict["status"] == "conflict_preserved"
    assert conflict["snapshot"]["snapshot_sha256"] == first["snapshot"]["snapshot_sha256"]
    assert conflict["attempted_snapshot_sha256"] != first["snapshot"]["snapshot_sha256"]


def test_historical_backfill_is_not_mislabeled_point_in_time(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    payload = _confirmation()
    payload["asof"] = "2020-01-02"

    result = history.record_open_confirmation(payload)

    assert result["status"] == "skipped_non_live_date"
    assert history.load_snapshots() == []


def test_build_input_joins_immutable_snapshots_with_outcome_bars(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    saved = history.record_open_confirmation(_confirmation())
    snapshot_date = saved["snapshot"]["date"]
    market_data = {
        "bars_by_code": {
            "600001": [{
                "date": snapshot_date,
                "open": 10.0,
                "high": 10.2,
                "low": 9.9,
                "close": 10.1,
                "volume": 1000,
            }],
        },
        "benchmark_bars": [{
            "date": snapshot_date,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 10000,
        }],
    }

    result = history.build_portfolio_input(
        history.load_snapshots(),
        market_data,
        rules_locked_at=f"{snapshot_date}T09:34:00+08:00",
    )

    assert result["schema"] == "portfolio_backtest_input_v1"
    assert result["snapshots"][0]["date"] == snapshot_date
    assert result["bars_by_code"]["600001"][0]["close"] == 10.1
    assert result["benchmark_bars"][0]["close"] == 100.5
