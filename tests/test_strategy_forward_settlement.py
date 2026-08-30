"""Settled Forward Samples 的冻结、结算与 gate dataset 契约。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skills.common import strategy_forward_settlement as forward
from skills.common import local_market_history
from skills.common.execution_model import net_return_pct


def _policy(tmp_path: Path) -> Path:
    path = tmp_path / "forward-policy.json"
    path.write_text(json.dumps({
        "schema": "strategy_forward_settlement_policy_v1",
        "version": "test-v1",
        "entry_rule": "next_trading_session_open_reference",
        "horizons": [1, 3],
        "primary_horizon_by_strategy": {
            "rank_surprise": 1,
            "divergence_reseal": 1,
            "assist_arbitrage": 1,
            "preleader_arbitrage": 1,
            "reverse_volume": 3,
            "ice_point_reversal": 3
        },
        "benchmark": {"code": "000300", "name": "CSI300"},
        "cost_model": {
            "assumed_notional": 20000.0,
            "entry_slippage_bps": 10.0,
            "exit_slippage_bps": 10.0
        },
        "terminal_grace_sessions": 5,
        "minimum_coverage_ratio": 0.95,
        "maximum_terminal_ambiguity_ratio": 0.02,
        "approved_policy_hashes": [],
        "approved_strategy_rule_hashes": {
            "rank_surprise": ["r" * 64],
            "divergence_reseal": [],
            "assist_arbitrage": [],
            "preleader_arbitrage": [],
            "reverse_volume": [],
            "ice_point_reversal": []
        }
    }), encoding="utf-8")
    return path


def _approve_current_policy(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    semantic = {key: value for key, value in payload.items()
                if key != "approved_policy_hashes"}
    digest = forward.json_sha256(semantic)
    payload["approved_policy_hashes"] = [digest]
    path.write_text(json.dumps(payload), encoding="utf-8")
    return digest


def _shadow(tmp_path: Path, *, qualification_class="canonical_forward") -> Path:
    path = tmp_path / "shadow.json"
    eligible = qualification_class == "canonical_forward"
    path.write_text(json.dumps({
        "schema": "strategy_shadow_daily_v1",
        "asof": "2026-08-24",
        "generated_at": "2026-08-24T23:40:00+08:00",
        "input_path": str(tmp_path / "evidence.json"),
        "input_sha256": "e" * 64,
        "result_sha256": "s" * 64,
        "evidence_qualification": {
            "rank_surprise": {
                "class": qualification_class,
                "canonical_forward_eligible": eligible,
                "reasons": [],
            },
            "ice_point_reversal": {
                "class": "canonical_forward",
                "canonical_forward_eligible": True,
                "reasons": [],
            },
        },
        "strategies": {
            "rank_surprise": {
                "strategy_id": "rank_surprise", "status": "signal",
                "forward_settlement_eligible": True,
                "results": [{"code": "600001", "date": "2026-08-24", "status": "signal"}],
            },
            "ice_point_reversal": {
                "strategy_id": "ice_point_reversal", "status": "signal",
                "forward_settlement_eligible": True,
                "results": [{"code": "MARKET", "date": "2026-08-24", "status": "signal"}],
            },
        },
        "strategy_rule_bindings": {
            "rank_surprise": {"version": "rules-v1", "sha256": "r" * 64},
            "ice_point_reversal": {"version": "rules-v1", "sha256": "i" * 64},
        },
        "research_only": True,
        "execution_eligible": False,
        "live_order_sent": False,
    }), encoding="utf-8")
    return path


def test_run_freezes_only_canonical_settleable_security_predictions(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    report = forward.run(
        "2026-08-24", str(_shadow(tmp_path)), policy_path=str(_policy(tmp_path))
    )

    assert report["schema"] == "strategy_forward_settlement_run_v1"
    assert report["frozen"] == 1
    assert report["rejected"] == {"non_tradeable_entity": 1}
    predictions = forward.load_predictions()
    assert [(row["strategy_id"], row["entity_id"]) for row in predictions] == [
        ("rank_surprise", "600001")
    ]
    prediction = predictions[0]
    assert prediction["entry_rule"] == "next_trading_session_open_reference"
    assert prediction["horizons"] == [1, 3]
    assert prediction["research_only"] is True
    assert prediction["execution_eligible"] is False
    assert prediction["shadow_sha256"] == "s" * 64
    assert prediction["evidence_sha256"] == "e" * 64
    assert prediction["strategy_rules_sha256"] == "r" * 64
    assert prediction["prediction_sha256"] == forward.artifact_sha256(prediction)


def test_run_rejects_exploratory_reconstruction_even_if_top_level_flags_claim_canonical(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    report = forward.run(
        "2026-08-24",
        str(_shadow(tmp_path, qualification_class="exploratory_reconstruction")),
        policy_path=str(_policy(tmp_path)),
    )
    assert report["frozen"] == 0
    assert report["rejected"] == {"evidence_not_canonical_forward": 1,
                                   "non_tradeable_entity": 1}
    assert forward.load_predictions() == []


def _seed_bars():
    rows = []
    stock = [
        ("2026-08-24", 9.5, 9.8),
        ("2026-08-25", 10.0, 10.5),
        ("2026-08-26", 10.6, 10.8),
        ("2026-08-27", 10.9, 11.0),
    ]
    benchmark = [
        ("2026-08-24", 99.0, 99.5),
        ("2026-08-25", 100.0, 101.0),
        ("2026-08-26", 101.0, 101.5),
        ("2026-08-27", 101.5, 102.0),
    ]
    for code, values in (("600001", stock), ("000300", benchmark)):
        for day, open_price, close_price in values:
            rows.append({
                "code": code, "trading_date": day, "adjust_flag": "qfq",
                "open": open_price, "high": max(open_price, close_price),
                "low": min(open_price, close_price), "close": close_price,
                "source": "fixture", "source_version": "v1",
                "updated_at": f"{day}T16:00:00+08:00",
            })
    local_market_history.upsert_daily_bars(rows)


def test_run_settles_t1_t3_from_next_session_open_with_same_session_benchmark_and_costs(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    policy = _policy(tmp_path)
    shadow = _shadow(tmp_path)
    forward.run("2026-08-24", str(shadow), policy_path=str(policy))
    _seed_bars()

    report = forward.run("2026-08-27", str(shadow), policy_path=str(policy))
    assert report["settled"] == 2
    assert report["pending"] == 0
    samples = forward.load_settlements()
    assert [sample["horizon_sessions"] for sample in samples] == [1, 3]

    t1, t3 = samples
    assert (t1["entry_date"], t1["entry_price_raw"], t1["exit_date"],
            t1["exit_price_raw"]) == ("2026-08-25", 10.0, "2026-08-25", 10.5)
    assert (t3["entry_date"], t3["exit_date"], t3["exit_price_raw"]) == (
        "2026-08-25", "2026-08-27", 11.0
    )
    assert t1["benchmark"]["entry_date"] == t1["entry_date"]
    assert t1["benchmark"]["exit_date"] == t1["exit_date"]
    assert t1["benchmark"]["gross_return"] == pytest.approx(0.01)

    slippage_entry = 10.0 * 1.001
    slippage_exit = 10.5 * 0.999
    expected_gross_pct = (slippage_exit / slippage_entry - 1) * 100
    expected_net = net_return_pct(
        gross_return_pct=expected_gross_pct, notional=20000.0, asof="2026-08-25"
    )["net_return_pct"] / 100
    assert t1["gross_forward_return"] == pytest.approx(expected_gross_pct / 100)
    assert t1["net_forward_return"] == pytest.approx(expected_net)
    assert t1["net_alpha"] == pytest.approx(expected_net - 0.01)
    assert t1["outcome_available_at"] == "2026-08-25T15:00:00+08:00"
    assert t1["bar_snapshot_sha256"]
    assert t1["settlement_sha256"] == forward.artifact_sha256(t1)


def test_build_gate_dataset_only_projects_final_primary_approved_samples(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    policy = _policy(tmp_path)
    approved = _approve_current_policy(policy)
    shadow = _shadow(tmp_path)
    forward.run("2026-08-24", str(shadow), policy_path=str(policy))
    _seed_bars()
    forward.run("2026-08-27", str(shadow), policy_path=str(policy))

    dataset = forward.build_gate_dataset("rank_surprise", policy_path=str(policy))
    assert dataset["schema"] == "settled_forward_samples_v1"
    assert dataset["settlement_policy_sha256"] == approved
    assert dataset["considered"] == 1
    assert dataset["coverage_ratio"] == 1.0
    assert len(dataset["rows"]) == 1
    row = dataset["rows"][0]
    assert row["horizon_sessions"] == 1
    assert row["is_primary_horizon"] is True
    assert row["strategy_rules_sha256"] == "r" * 64
    assert row["prediction_sha256"]
    assert row["bar_snapshot_sha256"]
    assert dataset["dataset_sha256"] == forward.artifact_sha256(dataset)

    settlement_path = next(
        (tmp_path / "state" / "skills" / "stock-triage" / "data" /
         "strategy_forward" / "settlements").glob("*/t1.json")
    )
    tampered = json.loads(settlement_path.read_text(encoding="utf-8"))
    tampered["net_forward_return"] = 99.0
    settlement_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="settlement_hash_mismatch"):
        forward.build_gate_dataset("rank_surprise", policy_path=str(policy))


def test_immutable_prediction_is_idempotent_but_changed_same_day_signal_conflicts(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    policy = _policy(tmp_path)
    shadow = _shadow(tmp_path)
    first = forward.run("2026-08-24", str(shadow), policy_path=str(policy))
    second = forward.run("2026-08-24", str(shadow), policy_path=str(policy))
    assert first["frozen"] == 1
    assert second["frozen"] == 0

    payload = json.loads(shadow.read_text(encoding="utf-8"))
    payload["strategies"]["rank_surprise"]["results"][0]["surprise"] = 9.9
    shadow.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="immutable artifact conflict"):
        forward.run("2026-08-24", str(shadow), policy_path=str(policy))


def test_missing_market_bars_remain_pending_then_become_terminal_without_fake_returns(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    policy = _policy(tmp_path)
    shadow = _shadow(tmp_path)
    forward.run("2026-08-24", str(shadow), policy_path=str(policy))
    dates = [
        "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28",
        "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03",
    ]
    local_market_history.upsert_daily_bars([{
        "code": "000300", "trading_date": day, "adjust_flag": "qfq",
        "open": 100 + index, "high": 101 + index, "low": 99 + index,
        "close": 100.5 + index, "source": "fixture", "source_version": "v1",
    } for index, day in enumerate(dates)])

    pending = forward.run("2026-08-27", str(shadow), policy_path=str(policy))
    assert pending["pending"] == 2
    assert pending["terminal_unresolved"] == 0
    assert forward.load_settlements() == []

    terminal = forward.run("2026-09-03", str(shadow), policy_path=str(policy))
    assert terminal["pending"] == 0
    assert terminal["terminal_unresolved"] == 1
    unresolved = forward.load_terminal_unresolved()
    assert unresolved[0]["reason"] == "market_data_unavailable_or_session_mismatch"
    assert "gross_forward_return" not in unresolved[0]
    _approve_current_policy(policy)
    with pytest.raises(ValueError, match="terminal_ambiguity"):
        forward.build_gate_dataset("rank_surprise", policy_path=str(policy))
