"""Frozen T01/T02 contract for the research-only tail-close strategy."""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

from config_registry import ConfigError, config_sha256, load_registered


PRIMARY_ID = "tail_close:mainline_continuation_v1"
SIBLING_ID = "tail_close:after_hours_fixed_v1"
REQUIRED_ROOTS = {
    "schema",
    "version",
    "strategies",
    "runtime",
    "universe",
    "market_gate",
    "sector_gate",
    "stock_gate",
    "ranking",
    "execution",
    "exit",
    "portfolio",
    "validation",
    "safety",
}


def _config() -> dict:
    return load_registered("tail_close_strategy")


def _write(tmp_path, payload: dict):
    path = tmp_path / "tail_close_strategy.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return path


def test_registered_tail_close_config_has_frozen_roots_and_stable_hash():
    payload = _config()

    assert REQUIRED_ROOTS <= set(payload)
    digest = config_sha256(payload)
    assert len(digest) == 64
    assert digest == config_sha256(dict(reversed(list(payload.items()))))


@pytest.mark.parametrize("missing", sorted(REQUIRED_ROOTS))
def test_registry_rejects_each_missing_required_root(tmp_path, missing):
    payload = _config()
    payload.pop(missing)

    with pytest.raises(ConfigError, match="required root"):
        load_registered("tail_close_strategy", path=_write(tmp_path, payload))


@pytest.mark.parametrize(
    ("path", "missing"),
    [
        (("runtime",), "deadline"),
        (("runtime",), "maximum_clock_offset_seconds"),
        (("runtime", "plugin_contract"), "shared_runtime_owners"),
        (("stock_gate",), "minimum_tail_minutes"),
        (("stock_gate",), "maximum_single_minute_gain_share"),
        (("stock_gate",), "minimum_last10_return"),
        (("stock_gate",), "maximum_single_minute_loss"),
        (("stock_gate",), "volume_selloff_multiple"),
        (("sector_gate",), "minimum_breadth"),
        (("execution",), "observed_system_latency_seconds"),
        (("execution",), "manual_review_latency_seconds"),
        (("execution",), "research_notional_per_signal_cny"),
        (("execution",), "after_hours_queue_discount"),
        (("execution",), "maximum_limit_premium"),
        (("validation", "oos"), "maximum_censored_ratio"),
        (("validation", "shadow"), "maximum_fill_rate_error"),
        (("portfolio",), "research_portfolio_notional_cny"),
        (("portfolio",), "maximum_single_position_pct"),
        (("portfolio",), "maximum_sector_exposure_pct"),
        (("safety",), "broker_call_count"),
        (("strategies", PRIMARY_ID), "automatic_ordering"),
    ],
)
def test_registry_rejects_missing_nested_contract_field(tmp_path, path, missing):
    payload = _config()
    target = payload
    for key in path:
        target = target[key]
    target.pop(missing)

    with pytest.raises(ConfigError, match="required fields"):
        load_registered("tail_close_strategy", path=_write(tmp_path, payload))


@pytest.mark.parametrize(
    "mutation",
    [
        "strategy_key",
        "strategy_id",
        "strategy_lane",
    ],
)
def test_registry_rejects_wrong_strategy_identity(tmp_path, mutation):
    payload = _config()
    if mutation == "strategy_key":
        payload["strategies"]["tail_close:wrong_v1"] = payload["strategies"].pop(
            PRIMARY_ID
        )
    else:
        payload["strategies"][PRIMARY_ID][mutation] = "wrong"

    with pytest.raises(ConfigError, match="strategy"):
        load_registered("tail_close_strategy", path=_write(tmp_path, payload))


@pytest.mark.parametrize(
    ("strategy_id", "field", "invalid"),
    [
        (PRIMARY_ID, "decision_time", "14:51:00"),
        (PRIMARY_ID, "pit_cutoff", "14:50:00"),
        (PRIMARY_ID, "entry_session", "after_hours_fixed_price"),
        (SIBLING_ID, "decision_time", "14:50:00"),
        (SIBLING_ID, "pit_cutoff", "14:49:59"),
        (SIBLING_ID, "entry_session", "continuous_auction"),
    ],
)
def test_registry_rejects_wrong_strategy_session(
    tmp_path,
    strategy_id,
    field,
    invalid,
):
    payload = _config()
    payload["strategies"][strategy_id][field] = invalid

    with pytest.raises(ConfigError, match="fixed"):
        load_registered("tail_close_strategy", path=_write(tmp_path, payload))


def test_runtime_freezes_prepare_decision_deadline_and_cancel():
    runtime = _config()["runtime"]

    assert runtime["prepare_cutoff"] == "14:34:59"
    assert runtime["prepare_maximum_current_record_age_seconds"] == 60
    assert runtime["maximum_clock_offset_seconds"] == 2
    assert runtime["decision_cutoff"] == "14:49:59"
    assert runtime["decision_maximum_current_record_age_seconds"] == 10
    assert runtime["deadline"] == "14:50:20"
    assert runtime["cancel"] == "14:56:30"
    assert runtime["forbidden_dependencies"] == [
        "15:00_eod_snapshot",
        "15:05_after_hours_sibling",
        "15:07_candidate_discovery",
    ]


def test_executable_r0_parameters_are_frozen_for_plugin_consumers():
    payload = _config()

    assert payload["stock_gate"]["minimum_tail_minutes"] == 15
    assert payload["stock_gate"]["maximum_single_minute_gain_share"] == 0.5
    assert payload["stock_gate"]["minimum_last10_return"] == -0.01
    assert payload["stock_gate"]["maximum_single_minute_loss"] == -0.005
    assert payload["stock_gate"]["volume_selloff_multiple"] == 2.0
    assert payload["sector_gate"]["minimum_breadth"] == 0.55
    assert payload["execution"]["maximum_limit_premium"] == 0.003
    assert payload["execution"]["observed_system_latency_seconds"] == 0
    assert payload["execution"]["manual_review_latency_seconds"] == 0
    assert payload["execution"]["research_notional_per_signal_cny"] == 100000
    assert payload["execution"]["after_hours_queue_discount"] == 0.5
    assert payload["validation"]["oos"]["maximum_censored_ratio"] == 0.05
    assert payload["validation"]["shadow"]["maximum_fill_rate_error"] == 0.02
    assert payload["portfolio"]["research_portfolio_notional_cny"] == 1000000
    assert payload["portfolio"]["maximum_single_position_pct"] == 10
    assert payload["portfolio"]["maximum_sector_exposure_pct"] == 20


def test_plugin_contract_is_pure_and_shared_runtime_owns_governance():
    contract = _config()["runtime"]["plugin_contract"]

    assert contract["methods"] == [
        "prepare",
        "gate",
        "rank",
        "simulate_execution",
        "label_outcome",
    ]
    assert contract["side_effects"] == "none"
    assert set(contract["shared_runtime_owners"]) == {
        "registry",
        "policy",
        "portfolio",
        "ledger",
        "validation",
    }


def test_primary_and_after_hours_sibling_have_independent_evidence_gates():
    payload = _config()
    sibling = payload["strategies"][SIBLING_ID]
    sibling_gate = payload["validation"]["after_hours_sibling"]

    assert sibling["enabled"] is False
    assert sibling["readiness"] == "not_ready"
    assert sibling["evidence_inheritance"] == "forbidden"
    assert sibling_gate == {
        "strategy_id": SIBLING_ID,
        "status": "not_ready",
        "independent_config_hash_required": True,
        "independent_oos_required": True,
        "independent_shadow_required": True,
        "independent_promotion_required": True,
        "inherit_primary_evidence": False,
    }


def test_oos_shadow_and_manual_pilot_gates_are_not_preapproved():
    validation = _config()["validation"]

    assert validation["oos"]["status"] == "not_started"
    assert validation["shadow"]["status"] == "not_started"
    assert validation["shadow"]["minimum_real_trading_days"] == 60
    assert validation["manual_pilot"]["status"] == "not_eligible"
    assert validation["manual_pilot"]["requires_oos_pass"] is True
    assert validation["manual_pilot"]["requires_shadow_pass"] is True
    assert (
        validation["manual_pilot"]["requires_explicit_human_approval"] is True
    )


def test_safety_contract_has_zero_broker_calls_and_zero_automatic_orders():
    payload = _config()
    safety = payload["safety"]

    assert safety["research_only"] is True
    assert safety["live_weight"] == 0
    assert safety["broker_access"] == "forbidden"
    assert safety["broker_call_count"] == 0
    assert safety["automatic_ordering"] == "forbidden"
    assert safety["automatic_order_count"] == 0
    assert safety["policy_and_portfolio_bypass"] == "forbidden"
    assert safety["registry_write_owner"] == "shared_runtime_only"
    assert safety["ledger_write_owner"] == "shared_runtime_only"
    assert safety["validation_write_owner"] == "shared_runtime_only"
    assert safety["manual_pilot_reconciliation_enabled"] is False

    for strategy in payload["strategies"].values():
        assert strategy["promotion_state"] == "research_only"
        assert strategy["live_weight"] == 0
        assert strategy["broker_access"] == "forbidden"
        assert strategy["automatic_ordering"] == "forbidden"


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("research_only", False),
        ("live_weight", 0.01),
        ("broker_access", "allowed"),
        ("broker_call_count", 1),
        ("automatic_ordering", "allowed"),
        ("automatic_order_count", 1),
    ],
)
def test_registry_rejects_any_live_execution_capability(tmp_path, field, invalid):
    payload = deepcopy(_config())
    payload["safety"][field] = invalid

    with pytest.raises(ConfigError, match="zero live execution"):
        load_registered("tail_close_strategy", path=_write(tmp_path, payload))
