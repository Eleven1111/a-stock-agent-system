from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import a_share_rules
import a_stock_http
import market_adapters
import pytest
from validation_program import (
    DailyEvidenceRegistry,
    ValidationError,
    build_validation_report,
    build_walk_forward_folds,
)


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


candidate_discovery = _load(
    "p2_arch_candidate_discovery",
    "skills/stock-triage/scripts/candidate_discovery.py",
)
portfolio_backtest = _load(
    "p2_arch_portfolio_backtest",
    "skills/chanlun-backtest/scripts/portfolio_backtest.py",
)


def _pit(day: str, mode: str = "live"):
    return {
        "schema": "pit_stage_contract_v1",
        "decision_mode": mode,
        "event_asof": day,
        "evidence_time": f"{day}T15:00:00+08:00",
        "captured_at": f"{day}T15:05:00+08:00",
        "stage_policy": {
            "schema": "pit_stage_contract_v1",
            "stage": "candidate_discovery",
            "cutoff_time": "15:30:00",
            "timezone": "Asia/Shanghai",
            "publication_delay_seconds": 0,
        },
    }


def _snapshot(path: Path, day: str, payload=None) -> dict:
    payload = payload or {"quotes": {"600001": {"price": 10.0}}}
    payload_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    record = {
        "schema": "market_snapshot_v1",
        "snapshot_id": f"snap-{payload_hash[:24]}",
        "snapshot_path": str(path),
        "payload_hash": payload_hash,
        "payload": payload,
        "point_in_time": _pit(day),
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    return record


def _strict_backtest_payload():
    bars = [
        {"date": "2026-07-09", "open": 10, "high": 10.1, "low": 9.9, "close": 10, "volume": 100_000},
        {"date": "2026-07-10", "open": 10, "high": 10.6, "low": 9.9, "close": 10.5, "volume": 100_000},
        {"date": "2026-07-13", "open": 10.5, "high": 11.1, "low": 10.4, "close": 11, "volume": 100_000},
    ]
    pit = _pit("2026-07-09", "replay")
    pit["evidence_time"] = "2026-07-09T09:34:00+08:00"
    pit["captured_at"] = "2026-07-09T09:35:00+08:00"
    pit["stage_policy"]["cutoff_time"] = "09:35:00"
    return {
        "schema": "portfolio_backtest_input_v1",
        "strategy_id": "strict-cost-v1",
        "weights": {"score": 1.0},
        "policy": {
            "initial_cash": 100_000,
            "top_n": 1,
            "max_positions": 1,
            "minimum_holding_sessions": 1,
            "commission": 0.0,
            "stamp_tax": 0.0,
            "slippage": 0.0,
            "lot_size": 100,
        },
        "snapshots": [{
            "date": "2026-07-09",
            "generated_at": "2026-07-09T09:35:00+08:00",
            "source_versions": {"quotes": "fixture-v1"},
            "candidates": [{
                "code": "600001",
                "name": "示例股份",
                "score": 90,
                "evidence_asof": "2026-07-09T09:34:00+08:00",
                "strict_execution": True,
                "decision_mode": "replay",
                "point_in_time": pit,
                "listing_date": "2020-01-01",
                "listing_stage": "normal",
                "is_st": False,
            }],
        }],
        "bars_by_code": {"600001": bars},
        "benchmark_bars": bars,
    }


def test_star_and_chinext_st_risk_warning_limit_is_twenty_percent():
    for code in ("300001", "301001", "688001"):
        rule = a_share_rules.resolve_price_limit_rule(
            code=code,
            asof="2026-07-10",
            listing_date="2020-01-01",
            listing_stage="normal",
            is_st=True,
            direction="buy",
        )
        assert rule["limit_pct"] == 20.0
    main = a_share_rules.resolve_price_limit_rule(
        code="600001", asof="2026-07-10", listing_date="2020-01-01",
        listing_stage="normal", is_st=True, direction="buy",
    )
    assert main["limit_pct"] == 5.0


@pytest.mark.parametrize("adjustment", ["qfq", "hfq", "QFQ", "HFQ"])
def test_adjusted_historical_replay_is_not_directionally_eligible(
    monkeypatch, adjustment
):
    calls = []
    monkeypatch.setattr(
        market_adapters,
        "_daily_series_attempts",
        lambda *args: calls.append(args) or (),
    )
    result = market_adapters.fetch_a_share_daily_series(
        "600001",
        event_asof="2026-07-09",
        adjustment=adjustment,
        decision_mode="replay",
        fetched_at="2026-07-10T01:00:00+00:00",
        use_cache=False,
    )
    assert result["status"] == "blocked"
    assert result["reason"] == "adjustment_replay_unsafe"
    assert result["directional_eligible"] is False
    assert calls == []


@pytest.mark.parametrize(
    ("bars", "reason"),
    [
        ([{"date": "2026-07-10", "open": 9, "high": 10, "low": 9, "close": 10, "volume": 1}], "future_bar"),
        ([{"date": "2026-07-09evil", "open": 9, "high": 10, "low": 9, "close": 10, "volume": 1}], "series_invalid"),
        ([{"date": "2026-07-09", "open": True, "high": True, "low": True, "close": True, "volume": True}], "series_invalid"),
        ([{"date": "2026-07-09", "open": 9, "high": 8, "low": 9, "close": 10, "volume": 1}], "series_invalid"),
        ([
            {"date": "2026-07-09", "open": 9, "high": 10, "low": 9, "close": 10, "volume": 1},
            {"date": "2026-07-09", "open": 9, "high": 10, "low": 9, "close": 10, "volume": 1},
        ], "series_order_invalid"),
    ],
)
def test_replay_series_rejects_future_malformed_or_duplicate_bars(bars, reason):
    result = market_adapters.select_series_provider(
        [("fixture", "v1", lambda: bars)],
        adjustment="unadjusted",
        event_asof="2026-07-09",
        fetched_at="2026-07-09T15:00:00+08:00",
        decision_mode="replay",
    )
    assert result["status"] == "blocked"
    assert result["reason"] == reason


@pytest.mark.parametrize(
    ("event_asof", "fetched_at", "reason"),
    [
        ("2099-01-01", "2026-07-10T01:00:00+00:00", "future_event_asof"),
        ("2026-07-09", "2099-01-01T01:00:00+00:00", "future_fetched_at"),
    ],
)
def test_series_request_rejects_future_event_or_fetch_time(
    event_asof, fetched_at, reason
):
    result = market_adapters.select_series_provider(
        [("fixture", "v1", lambda: [{
            "date": "2026-07-09", "open": 9, "high": 10,
            "low": 9, "close": 10, "volume": 1,
        }])],
        adjustment="unadjusted",
        event_asof=event_asof,
        fetched_at=fetched_at,
        decision_mode="replay",
    )

    assert result["status"] == "blocked"
    assert result["reason"] == reason


def test_candidate_output_propagates_strict_execution_provenance():
    result = {"candidates": [{"code": "600001", "name": "示例股份"}]}
    quotes = {
        "600001": {
            "listed_date": "2020-01-01",
            "is_st": False,
            "provider": "tencent",
            "provider_version": "tencent-adapter-v2",
            "fetched_at": "2026-07-10T07:05:00+00:00",
            "transport_trust": "lower",
            "directional_eligible": False,
        }
    }
    klines = {
        "600001": [{
            "date": "2026-07-10",
            "series_provenance": {
                "provider": "adata",
                "provider_version": "adata-adapter-v1",
                "adjustment": "qfq",
                "event_asof": "2026-07-10",
                "fetched_at": "2026-07-10T07:04:00+00:00",
                "decision_mode": "live",
            },
        }]
    }
    candidate_discovery._propagate_execution_contracts(
        result,
        quotes,
        klines,
        point_in_time=_pit("2026-07-10"),
        decision_mode="live",
    )
    candidate = result["candidates"][0]
    assert candidate["strict_execution"] is True
    assert candidate["listing_date"] == "2020-01-01"
    assert candidate["listing_stage"] == "normal"
    assert candidate["is_st"] is False
    assert candidate["point_in_time"]["event_asof"] == "2026-07-10"
    assert candidate["series_provenance"]["provider"] == "adata"
    assert candidate["transport_provenance"]["directional_eligible"] is False


def test_canonical_tencent_adapter_retains_lower_trust_metadata(monkeypatch):
    parts = [""] * 46
    parts[1], parts[2], parts[3], parts[4] = "示例", "600001", "10", "9"
    parts[5], parts[6], parts[31], parts[32] = "9.5", "100", "1", "11.1"
    parts[33], parts[34], parts[37], parts[38], parts[39], parts[45] = (
        "10", "9", "1", "2", "3", "4"
    )

    class Client:
        def request_text(self, request, encoding):
            assert request.full_url.startswith("http://")
            assert encoding == "gbk"
            return type("Result", (), {
                "data": f'v_sh600001="{"~".join(parts)}"',
                "fetched_at": "2026-07-10T01:00:00+00:00",
                "attempts": 1,
            })()

    result = a_stock_http.fetch_tencent_quotes_result(["sh600001"], client=Client())
    quote = result.data["sh600001"]
    assert quote["provider"] == "tencent"
    assert quote["provider_version"] == "tencent-adapter-v2"
    assert quote["transport_trust"] == "lower"
    assert quote["directional_eligible"] is False
    monkeypatch.setattr(a_stock_http, "fetch_tencent_quotes_result", lambda codes: result)
    assert a_stock_http.fetch_tencent_quote(["sh600001"])["sh600001"] == quote


def test_caller_supplied_corroboration_cannot_upgrade_lower_trust_quote(monkeypatch):
    result = type("Result", (), {
        "data": {"600001": {
            "provider": "tencent",
            "provider_version": "tencent-adapter-v2",
            "fetched_at": "2026-07-10T01:00:00+00:00",
            "transport_trust": "lower",
            "transport_reason": "transport_lower_trust",
            "directional_eligible": False,
            "price": 10.0,
        }}
    })()
    monkeypatch.setattr(market_adapters, "_fetch_tencent_quotes_result", lambda codes: result)
    blocked = market_adapters.fetch_tencent_quote_with_provenance(["600001"])
    assert blocked["600001"]["directional_eligible"] is False
    still_blocked = market_adapters.fetch_tencent_quote_with_provenance(
        ["600001"],
        decision_stage="candidate_discovery",
        corroborating_quotes={"600001": {
            "provider": "secure-fixture",
            "provider_version": "secure-v1",
            "price": 10.01,
            "fetched_at": "2026-07-10T00:59:30+00:00",
            "decision_stage": "candidate_discovery",
            "transport_trust": "authenticated",
            "directional_eligible": True,
        }},
    )
    assert still_blocked["600001"]["directional_eligible"] is False
    assert still_blocked["600001"]["transport_reason"] == "transport_lower_trust"
    assert still_blocked["600001"]["corroboration_status"] == (
        "rejected_untrusted_input"
    )


@pytest.mark.parametrize(
    "override",
    [
        {"provider": "TENCENT"},
        {"fetched_at": "2026-07-10T00:57:59+00:00"},
        {"fetched_at": "2026-07-10T01:00:01+00:00"},
        {"provider_version": ""},
        {"decision_stage": "open_confirmation"},
    ],
)
def test_lower_trust_quote_rejects_nonindependent_stale_or_mismatched_evidence(
    monkeypatch, override
):
    result = type("Result", (), {
        "data": {"600001": {
            "provider": "tencent",
            "provider_version": "tencent-adapter-v2",
            "fetched_at": "2026-07-10T01:00:00+00:00",
            "transport_trust": "lower",
            "directional_eligible": False,
            "price": 10.0,
        }}
    })()
    monkeypatch.setattr(market_adapters, "_fetch_tencent_quotes_result", lambda codes: result)
    corroboration = {
        "provider": "secure-fixture",
        "provider_version": "secure-v1",
        "price": 10.01,
        "fetched_at": "2026-07-10T00:59:30+00:00",
        "decision_stage": "candidate_discovery",
        "transport_trust": "authenticated",
        "directional_eligible": True,
    }
    corroboration.update(override)

    quote = market_adapters.fetch_tencent_quote_with_provenance(
        ["600001"],
        decision_stage="candidate_discovery",
        corroborating_quotes={"600001": corroboration},
    )["600001"]

    assert quote["directional_eligible"] is False
    assert quote["transport_reason"] != "independent_authenticated_corroboration"


def test_corroboration_freshness_hard_limit_cannot_be_relaxed(monkeypatch):
    result = type("Result", (), {
        "data": {"600001": {
            "provider": "tencent",
            "provider_version": "tencent-adapter-v2",
            "fetched_at": "2026-07-10T01:00:00+00:00",
            "transport_trust": "lower",
            "directional_eligible": False,
            "price": 10.0,
        }}
    })()
    monkeypatch.setattr(market_adapters, "_fetch_tencent_quotes_result", lambda codes: result)

    quote = market_adapters.fetch_tencent_quote_with_provenance(
        ["600001"],
        decision_stage="candidate_discovery",
        maximum_corroboration_age_seconds=1000,
        corroborating_quotes={"600001": {
            "provider": "secure-fixture",
            "provider_version": "secure-v1",
            "price": 10.01,
            "fetched_at": "2026-07-10T00:50:00+00:00",
            "decision_stage": "candidate_discovery",
            "transport_trust": "authenticated",
            "directional_eligible": True,
        }},
    )["600001"]

    assert quote["directional_eligible"] is False
    assert quote["transport_reason"] != "independent_authenticated_corroboration"


@pytest.mark.parametrize("maximum_age", [-1, float("inf"), True, "120"])
def test_invalid_corroboration_age_configuration_fails_closed(
    monkeypatch, maximum_age
):
    result = type("Result", (), {
        "data": {"600001": {
            "provider": "tencent",
            "provider_version": "tencent-adapter-v2",
            "fetched_at": "2026-07-10T01:00:00+00:00",
            "transport_trust": "lower",
            "directional_eligible": False,
            "price": 10.0,
        }}
    })()
    monkeypatch.setattr(market_adapters, "_fetch_tencent_quotes_result", lambda codes: result)

    quote = market_adapters.fetch_tencent_quote_with_provenance(
        ["600001"],
        decision_stage="candidate_discovery",
        maximum_corroboration_age_seconds=maximum_age,
        corroborating_quotes={"600001": {
            "provider": "secure-fixture",
            "provider_version": "secure-v1",
            "price": 10.01,
            "fetched_at": "2026-07-10T01:00:00+00:00",
            "decision_stage": "candidate_discovery",
            "transport_trust": "authenticated",
            "directional_eligible": True,
        }},
    )["600001"]

    assert quote["directional_eligible"] is False


def test_nonpositive_prices_cannot_satisfy_corroboration(monkeypatch):
    result = type("Result", (), {
        "data": {"600001": {
            "provider": "tencent",
            "provider_version": "tencent-adapter-v2",
            "fetched_at": "2026-07-10T01:00:00+00:00",
            "transport_trust": "lower",
            "directional_eligible": False,
            "price": -10.0,
        }}
    })()
    monkeypatch.setattr(market_adapters, "_fetch_tencent_quotes_result", lambda codes: result)

    quote = market_adapters.fetch_tencent_quote_with_provenance(
        ["600001"],
        decision_stage="candidate_discovery",
        corroborating_quotes={"600001": {
            "provider": "secure-fixture",
            "provider_version": "secure-v1",
            "price": -1000.0,
            "fetched_at": "2026-07-10T00:59:30+00:00",
            "decision_stage": "candidate_discovery",
            "transport_trust": "authenticated",
            "directional_eligible": True,
        }},
    )["600001"]

    assert quote["directional_eligible"] is False


def test_missing_primary_provider_cannot_prove_independence(monkeypatch):
    result = type("Result", (), {
        "data": {"600001": {
            "provider": "",
            "provider_version": "tencent-adapter-v2",
            "fetched_at": "2026-07-10T01:00:00+00:00",
            "transport_trust": "lower",
            "directional_eligible": False,
            "price": 10.0,
        }}
    })()
    monkeypatch.setattr(market_adapters, "_fetch_tencent_quotes_result", lambda codes: result)

    quote = market_adapters.fetch_tencent_quote_with_provenance(
        ["600001"],
        decision_stage="candidate_discovery",
        corroborating_quotes={"600001": {
            "provider": "secure-fixture",
            "provider_version": "secure-v1",
            "price": 10.01,
            "fetched_at": "2026-07-10T00:59:30+00:00",
            "decision_stage": "candidate_discovery",
            "transport_trust": "authenticated",
            "directional_eligible": True,
        }},
    )["600001"]

    assert quote["directional_eligible"] is False


def test_tencent_orderbook_snapshot_also_retains_lower_trust_metadata(monkeypatch):
    parts = [""] * 46
    parts[1], parts[2], parts[3], parts[4] = "示例", "600001", "10", "9"
    parts[5], parts[6], parts[31], parts[32] = "9.5", "100", "1", "11.1"
    parts[33], parts[34], parts[37], parts[38], parts[39], parts[45] = (
        "10", "9", "1", "2", "3", "4"
    )
    response = type("Result", (), {
        "data": f'v_sh600001="{"~".join(parts)}"',
        "fetched_at": "2026-07-10T01:00:00+00:00",
    })()
    monkeypatch.setattr(a_stock_http, "request_text", lambda *a, **k: response)

    quote = a_stock_http.fetch_tencent_snapshot(["sh600001"])["sh600001"]

    assert quote["provider"] == "tencent"
    assert quote["fetched_at"] == response.fetched_at
    assert quote["transport_trust"] == "lower"
    assert quote["directional_eligible"] is False


def test_strict_backtest_primary_equity_deducts_full_execution_fees():
    payload = _strict_backtest_payload()
    result = portfolio_backtest.run_portfolio(payload)
    trade = result["trades"][0]
    gross_final_equity = 100_000 + trade["shares"] * (
        trade["exit_price"] - trade["entry_price"]
    )
    assert trade["cost_estimate"]["applied_to_equity"] is True
    assert result["metrics"]["final_equity"] == pytest.approx(
        100_000 + trade["pnl"], abs=0.01
    )
    assert result["metrics"]["final_equity"] < gross_final_equity
    assert trade["net_return"] == trade["pnl"] / trade["entry_cost"]
    assert trade["pnl"] == pytest.approx(
        trade["pnl_estimate"]["estimated_net_pnl"], abs=0.0001
    )


def test_invalid_capacity_cannot_be_reported_evaluated():
    report = build_validation_report(
        precommitted_variants=["base"],
        precommitted_folds=["fold-0"],
        variant_results={"base": {"status": "passed"}},
        fold_results={"fold-0": {"status": "passed"}},
        returns=[0.01, 0.02],
        weights=[{"A": 1.0}],
        cost_stress_bps=[0, 10],
        capacity_inputs=[{"capital": 1_000_000, "required_notional": 10_000, "adv": 0}],
    )
    assert report["status"] == "not_evaluated"
    assert "capacity_unknown" in report["reasons"]
    assert report["capacity_curve"] == []


def test_walk_forward_contains_explicit_calibration_role():
    folds = build_walk_forward_folds(
        30,
        train_size=12,
        calibration_size=2,
        test_size=4,
        step=4,
        purge=1,
        embargo=1,
    )
    first = folds[0]
    assert first["train_end"] < first["calibration_start"]
    assert first["calibration_end"] < first["test_start"]
    assert first["roles"] == ["train", "calibration", "test"]


def test_daily_registry_requires_real_content_addressed_pit_snapshot(tmp_path):
    registry = DailyEvidenceRegistry(tmp_path / "daily.jsonl")
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"point_in_time":true}', encoding="utf-8")
    try:
        registry.append(
            "2026-07-10", invalid, event_asof="2026-07-10T15:05:00+08:00"
        )
    except ValidationError as exc:
        assert exc.code == "daily_evidence_invalid"
    else:
        raise AssertionError("non-snapshot evidence must be rejected")

    artifact = tmp_path / "pit.json"
    snapshot = _snapshot(artifact, "2026-07-10")
    record = registry.append(
        "2026-07-10", artifact, event_asof="2026-07-10T15:05:00+08:00"
    )
    assert record["snapshot_id"] == snapshot["snapshot_id"]
    assert record["payload_sha256"] == snapshot["payload_hash"]
