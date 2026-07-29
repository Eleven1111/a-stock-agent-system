import copy

import pytest

from tail_close_strategy import (
    TailCloseContractError,
    build_prepared_state,
    build_research_decision,
    canonical_hash,
    rank_sectors,
    validate_pit_bundle,
)
from tail_close_test_support import TRADING_DATE, bundle, config


def _prepared(cfg=None):
    actual = cfg or config()
    return build_prepared_state(bundle(prepare=True), actual)


def test_prepare_is_static_and_decision_recomputes_dynamic_gates():
    cfg = config()
    prepared = build_prepared_state(bundle(prepare=True), cfg)
    decision_input = bundle()
    decision_input["market"]["regime"] = "risk_off"

    decision = build_research_decision(
        decision_input,
        cfg,
        prepared_state=prepared,
        emitted_at=f"{TRADING_DATE}T14:50:10+08:00",
    )

    assert prepared["status"] == "ready"
    assert decision["status"] == "no_action"
    assert decision["signals"] == []
    assert "market_risk_off" in decision["market_gate"]["reasons"]


def test_decision_is_deterministic_and_keeps_one_security_per_mainline():
    cfg = config()
    payload = bundle()
    first = build_research_decision(
        payload,
        cfg,
        prepared_state=_prepared(cfg),
        emitted_at=f"{TRADING_DATE}T14:50:10+08:00",
    )
    second = build_research_decision(
        copy.deepcopy(payload),
        cfg,
        prepared_state=_prepared(cfg),
        emitted_at=f"{TRADING_DATE}T14:50:10+08:00",
    )

    assert first["decision_hash"] == second["decision_hash"]
    assert first["status"] == "research_signal"
    assert [item["code"] for item in first["signals"]] == ["600001"]
    signal = first["signals"][0]
    assert signal["strategy_id"] == "tail_close:mainline_continuation_v1"
    assert signal["research_only"] is True
    assert signal["live_weight"] == 0
    assert signal["provenance"]["snapshot_hash"] == "a" * 64


def test_decision_after_sla_is_no_action_late():
    decision = build_research_decision(
        bundle(),
        config(),
        prepared_state=_prepared(),
        emitted_at=f"{TRADING_DATE}T14:50:21+08:00",
    )

    assert decision["status"] == "no_action_late"
    assert decision["signals"] == []
    assert decision["broker_call_count"] == 0


def test_decision_before_1450_is_no_action_early():
    decision = build_research_decision(
        bundle(),
        config(),
        prepared_state=_prepared(),
        emitted_at=f"{TRADING_DATE}T14:40:00+08:00",
    )

    assert decision["status"] == "no_action_early"
    assert decision["signals"] == []


def test_prepare_rejection_cannot_reenter_at_decision():
    cfg = config()
    prepare_input = bundle(prepare=True)
    prepare_input["stocks"][0]["event_gate_passed"] = False
    prepared = build_prepared_state(prepare_input, cfg)

    decision = build_research_decision(
        bundle(),
        cfg,
        prepared_state=prepared,
        emitted_at=f"{TRADING_DATE}T14:50:10+08:00",
    )

    assert decision["signals"] == []
    rejection = next(
        item for item in decision["rejections"] if item["code"] == "600001"
    )
    assert "prepare_gate_rejected" in rejection["reasons"]


@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (
            lambda payload: payload["stocks"][0].update(
                {"available_time": f"{TRADING_DATE}T14:50:00+08:00"}
            ),
            "available_after_cutoff",
        ),
        (
            lambda payload: payload.update({"source_clock_offset_seconds": 3}),
            "source_clock_drift",
        ),
        (
            lambda payload: payload.update(
                {"source_clock_offset_seconds": float("nan")}
            ),
            "source_clock_drift_invalid",
        ),
        (
            lambda payload: payload.update(
                {"source_clock_offset_seconds": float("inf")}
            ),
            "source_clock_drift_invalid",
        ),
        (
            lambda payload: payload["stocks"][0]["minute_rows"][0].pop(
                "available_time"
            ),
            "pit_dual_time_incomplete",
        ),
        (
            lambda payload: payload.update(
                {"snapshot_sealed_at": f"{TRADING_DATE}T15:00:00+08:00"}
            ),
            "snapshot_sealed_after_cutoff",
        ),
        (
            lambda payload: payload["stocks"][0]["minute_rows"][0].update(
                {
                    "time": "14:30",
                    "event_time": "2026-07-27T14:30:30+08:00",
                    "available_time": "2026-07-27T14:30:30+08:00",
                }
            ),
            "minute_row_trading_date_mismatch",
        ),
        (
            lambda payload: [
                item.update(
                    {
                        "event_time": f"{TRADING_DATE}T14:00:00+08:00",
                        "available_time": f"{TRADING_DATE}T14:00:00+08:00",
                    }
                )
                for item in [
                    payload["market"],
                    *payload["sectors"],
                    *payload["stocks"],
                ]
            ],
            "current_record_stale",
        ),
    ],
)
def test_pit_contract_fails_closed(mutator, reason):
    payload = bundle()
    mutator(payload)

    with pytest.raises(TailCloseContractError, match=reason):
        validate_pit_bundle(
            payload,
            cutoff_time="14:49:59",
            maximum_clock_offset_seconds=2,
            maximum_current_record_age_seconds=10,
        )


def test_input_must_bind_to_frozen_config_hash():
    payload = bundle()
    payload["config_hash"] = "f" * 64

    with pytest.raises(TailCloseContractError, match="input_config_hash_mismatch"):
        build_research_decision(
            payload,
            config(),
            prepared_state=_prepared(),
            emitted_at=f"{TRADING_DATE}T14:50:10+08:00",
        )


@pytest.mark.parametrize(
    ("mutate_stock", "expected_reason"),
    [
        (
            lambda stock: stock.pop("ask_price"),
            "ask_price_missing",
        ),
        (
            lambda stock: stock.update({"near_limit_down": True}),
            "near_limit_down",
        ),
        (
            lambda stock: stock["minute_rows"][-1].update(
                {"close": 10.20, "volume": 1_000_000}
            ),
            "volume_selloff",
        ),
    ],
)
def test_stock_execution_and_continuity_gates_fail_closed(
    mutate_stock,
    expected_reason,
):
    payload = bundle()
    mutate_stock(payload["stocks"][0])

    decision = build_research_decision(
        payload,
        config(),
        prepared_state=_prepared(),
        emitted_at=f"{TRADING_DATE}T14:50:10+08:00",
    )

    rejection = next(
        item for item in decision["rejections"] if item["code"] == "600001"
    )
    assert expected_reason in rejection["reasons"]
    assert decision["signals"] == []


def test_decision_requires_successful_identity_bound_prepare():
    cfg = config()
    payload = bundle()

    with pytest.raises(TailCloseContractError, match="prepared_state_missing"):
        build_research_decision(
            payload,
            cfg,
            emitted_at=f"{TRADING_DATE}T14:50:10+08:00",
        )

    failed = _prepared(cfg)
    failed["status"] = "failed"
    failed_without_hash = {
        key: value for key, value in failed.items() if key != "prepared_hash"
    }
    failed["prepared_hash"] = canonical_hash(failed_without_hash)
    with pytest.raises(TailCloseContractError, match="prepared_state_not_ready"):
        build_research_decision(
            payload,
            cfg,
            prepared_state=failed,
            emitted_at=f"{TRADING_DATE}T14:50:10+08:00",
        )

    wrong_run = _prepared(cfg)
    wrong_run["run_id"] = "other-prepare-run"
    wrong_run_without_hash = {
        key: value for key, value in wrong_run.items() if key != "prepared_hash"
    }
    wrong_run["prepared_hash"] = canonical_hash(wrong_run_without_hash)
    with pytest.raises(TailCloseContractError, match="prepared_run_id_mismatch"):
        build_research_decision(
            payload,
            cfg,
            prepared_state=wrong_run,
            emitted_at=f"{TRADING_DATE}T14:50:10+08:00",
        )

    wrong_batch = _prepared(cfg)
    wrong_batch["batch_id"] = "other-batch"
    wrong_batch_without_hash = {
        key: value for key, value in wrong_batch.items() if key != "prepared_hash"
    }
    wrong_batch["prepared_hash"] = canonical_hash(wrong_batch_without_hash)
    with pytest.raises(TailCloseContractError, match="prepared_batch_id_mismatch"):
        build_research_decision(
            payload,
            cfg,
            prepared_state=wrong_batch,
            emitted_at=f"{TRADING_DATE}T14:50:10+08:00",
        )


def test_sector_ties_are_stable_and_missing_membership_is_rejected():
    cfg = config()
    sectors = bundle()["sectors"]
    for sector in sectors:
        sector.update(
            {
                "breadth": 0.7,
                "session_relative_return": 0.02,
                "tail_relative_return": 0.01,
                "persistence": 0.8,
                "liquidity_support": 0.7,
                "pit_amount": 1_000,
                "valid_member_count": 5,
            }
        )
    sectors.append(
        {
            **sectors[0],
            "sector_id": "missing",
            "valid_member_count": 2,
        }
    )

    ranked = rank_sectors(list(reversed(sectors)), cfg)

    assert [item["sector_id"] for item in ranked] == [
        "S0",
        "S1",
        "S2",
        "S3",
        "S4",
    ]
    assert "missing" not in {item["sector_id"] for item in ranked}
    assert canonical_hash(ranked) == canonical_hash(rank_sectors(sectors, cfg))
