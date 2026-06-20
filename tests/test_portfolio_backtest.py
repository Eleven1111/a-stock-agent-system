import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "chanlun-backtest" / "scripts" / "portfolio_backtest.py"
SPEC = importlib.util.spec_from_file_location("portfolio_backtest", SCRIPT)
portfolio_backtest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(portfolio_backtest)


def _bars(code, closes, *, sealed_entry=False):
    dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    rows = []
    previous = closes[0]
    for index, (date, close) in enumerate(zip(dates, closes)):
        open_price = previous if index else close
        if sealed_entry and date == "2026-01-06":
            open_price = high = low = close = round(previous * 1.10, 2)
        else:
            high = max(open_price, close) * 1.01
            low = min(open_price, close) * 0.99
        rows.append({
            "code": code,
            "date": date,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": 100000,
        })
        previous = close
    return rows


def _payload(*, top_n=2):
    return {
        "schema": "portfolio_backtest_input_v1",
        "strategy_id": "four_dim_combined_v1",
        "rules_locked_at": "2026-01-05T09:00:00+08:00",
        "weights": {"technical": 0.8, "catalyst": 0.2},
        "policy": {
            "initial_cash": 100000,
            "top_n": top_n,
            "max_positions": 2,
            "minimum_holding_sessions": 1,
            "commission": 0.0,
            "stamp_tax": 0.0,
            "slippage": 0.0,
            "lot_size": 100,
        },
        "snapshots": [{
            "date": "2026-01-05",
            "generated_at": "2026-01-05T09:35:00+08:00",
            "source_versions": {"quotes": "fixture-v1"},
            "candidates": [
                {
                    "code": "600001",
                    "name": "可成交",
                    "lane": "trend",
                    "score": 90,
                    "evidence_asof": "2026-01-05T09:34:00+08:00",
                    "components": {"technical": 10, "catalyst": 0},
                },
                {
                    "code": "600002",
                    "name": "一字板",
                    "lane": "daban",
                    "score": 80,
                    "evidence_asof": "2026-01-05T09:34:00+08:00",
                    "components": {"technical": 0, "catalyst": 10},
                },
            ],
        }],
        "bars_by_code": {
            "600001": _bars("600001", [10.0, 10.5, 11.0, 11.0]),
            "600002": _bars("600002", [10.0, 11.0, 10.0, 10.0], sealed_entry=True),
        },
        "benchmark_bars": _bars("000300", [100.0, 101.0, 102.0, 103.0]),
    }


def test_replay_enforces_t1_and_rejects_sealed_limit_up():
    result = portfolio_backtest.run_portfolio(_payload())

    assert result["metrics"]["closed_trades"] == 1
    assert result["trades"][0]["code"] == "600001"
    assert result["trades"][0]["entry_date"] == "2026-01-06"
    assert result["trades"][0]["exit_date"] == "2026-01-07"
    assert result["trades"][0]["net_return"] == pytest.approx(0.1)
    assert result["metrics"]["final_equity"] == pytest.approx(105000.0)
    assert result["metrics"]["benchmark_return"] == pytest.approx(0.02)
    assert result["metrics"]["turnover"] > 0
    assert any(
        row["code"] == "600002" and row["reason"] == "entry_limit_up_sealed"
        for row in result["rejections"]
    )


def test_sealed_limit_down_delays_exit_until_first_sellable_session():
    payload = _payload(top_n=1)
    bars = payload["bars_by_code"]["600001"]
    bars[2].update({"open": 9.45, "high": 9.45, "low": 9.45, "close": 9.45})
    bars[3].update({"open": 9.5, "high": 10.0, "low": 9.4, "close": 9.8})

    result = portfolio_backtest.run_portfolio(payload)

    assert result["trades"][0]["exit_date"] == "2026-01-08"
    assert result["trades"][0]["exit_price"] == pytest.approx(9.8)


def test_future_dated_evidence_is_rejected():
    payload = _payload(top_n=1)
    payload["snapshots"][0]["candidates"][0]["evidence_asof"] = (
        "2026-01-06T09:34:00+08:00"
    )

    result = portfolio_backtest.run_portfolio(payload)

    assert result["metrics"]["closed_trades"] == 0
    assert result["rejections"][0]["reason"] == "future_dated_evidence"


def test_zero_holding_sessions_fails_closed():
    payload = _payload()
    payload["policy"]["minimum_holding_sessions"] = 0

    with pytest.raises(ValueError, match="minimum_holding_sessions"):
        portfolio_backtest.run_portfolio(payload)


def test_rules_must_be_locked_before_first_oos_snapshot():
    payload = _payload()
    payload["rules_locked_at"] = "2026-01-05T10:00:00+08:00"

    with pytest.raises(ValueError, match="rules_locked_at"):
        portfolio_backtest.analyze_payload(payload, split_date="2026-01-05")


def test_missing_snapshot_source_versions_fails_closed():
    payload = _payload()
    payload["snapshots"][0]["source_versions"] = {}

    with pytest.raises(ValueError, match="source_versions"):
        portfolio_backtest.run_portfolio(payload)


def test_policy_rejected_candidate_cannot_reenter_through_ranking():
    payload = _payload(top_n=1)
    payload["snapshots"][0]["candidates"][0]["decision"] = "avoid"

    result = portfolio_backtest.run_portfolio(payload)

    assert not result["trades"]
    assert any(row["reason"] == "policy_not_directional" for row in result["rejections"])


def test_component_ablation_replays_selection_instead_of_adjusting_returns():
    payload = _payload(top_n=1)
    payload["bars_by_code"]["600002"] = _bars(
        "600002", [10.0, 10.0, 9.0, 9.0], sealed_entry=False
    )

    report = portfolio_backtest.analyze_payload(payload, split_date="2026-01-05")

    assert report["oos"]["trades"][0]["code"] == "600001"
    assert report["ablations"]["without_technical"]["trades"][0]["code"] == "600002"
    assert report["ablations"]["without_technical"]["metrics"]["total_return"] < 0


def test_oos_curve_starts_at_first_oos_entry_not_dataset_start():
    payload = _payload(top_n=1)
    payload["snapshots"] = [{
        **payload["snapshots"][0],
        "date": "2026-01-06",
        "generated_at": "2026-01-06T09:35:00+08:00",
        "candidates": [{
            **payload["snapshots"][0]["candidates"][0],
            "evidence_asof": "2026-01-06T09:34:00+08:00",
        }],
    }]

    report = portfolio_backtest.analyze_payload(payload, split_date="2026-01-06")

    assert [row["date"] for row in report["oos"]["equity_curve"]] == [
        "2026-01-07",
        "2026-01-08",
    ]
    assert list(report["oos"]["benchmark_curve"]) == ["2026-01-07", "2026-01-08"]


def test_artifact_digest_detects_result_tampering(tmp_path):
    payload = _payload(top_n=1)
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    report = portfolio_backtest.analyze_payload(payload, split_date="2026-01-05")
    artifact_path = tmp_path / "artifact.json"

    artifact = portfolio_backtest.write_research_artifact(
        str(artifact_path),
        input_path=str(input_path),
        payload=payload,
        report=report,
    )
    verified = portfolio_backtest.verify_research_artifact(
        str(artifact_path), expected_sha256=artifact["artifact_sha256"]
    )
    assert verified["valid"] is True

    artifact["result"]["oos"]["metrics"]["total_return"] = 9.99
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    tampered = portfolio_backtest.verify_research_artifact(str(artifact_path))
    assert tampered["valid"] is False
    assert "result_sha256_mismatch" in tampered["errors"]


def test_portfolio_gate_uses_verified_artifact(tmp_path):
    payload = _payload(top_n=2)
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    report = portfolio_backtest.analyze_payload(payload, split_date="2026-01-05")
    artifact_path = tmp_path / "artifact.json"
    portfolio_backtest.write_research_artifact(
        str(artifact_path),
        input_path=str(input_path),
        payload=payload,
        report=report,
    )

    gate = portfolio_backtest.evaluate_research_gate(
        report,
        artifact_path=str(artifact_path),
        min_oos_samples=1,
    )

    assert gate["decision"] in {"passed_for_reference", "failed"}
    assert not gate["blocking_reasons"]
