from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path

import pytest

import four_dim_pit_replay as replay


def _sessions(count: int) -> list[str]:
    current = date(2025, 1, 2)
    values = []
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def _bars(code: str, sessions: list[str], *, base: float, step: float = 0.1):
    result = []
    previous = base
    for index, session in enumerate(sessions):
        close = base + index * step
        result.append({
            "code": code,
            "trading_date": session,
            "adjust_flag": "qfq",
            "open": close - 0.05,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "preclose": previous,
            "volume": 1_000_000 + index,
            "amount": 10_000_000 + index,
            "turn": 2.0,
            "pct_chg": (close / previous - 1) * 100,
            "source": "test_qfq",
            "source_version": "1",
            "updated_at": f"{session}T16:00:00+08:00",
        })
        previous = close
    return result


def _policy():
    return {
        "schema": replay.POLICY_SCHEMA,
        "version": "test-v1",
        "adjust_flag": "qfq",
        "minimum_history_sessions": 60,
        "technical_lookback_sessions": 60,
        "horizons": [1, 3],
        "benchmark": {"code": "000300", "name": "CSI300"},
        "cost_model": {
            "assumed_notional": 20_000.0,
            "entry_slippage_bps": 10.0,
            "exit_slippage_bps": 10.0,
        },
        "walk_forward": {
            "train_size": 60,
            "calibration_size": 8,
            "test_size": 8,
            "step": 11,
            "purge": 3,
            "embargo": 3,
            "mode": "expanding",
        },
        "variants": [
            {"variant_id": "technical_ge_6", "minimum_score": 6.0, "daily_top_n": 1},
        ],
    }


def test_replay_is_strictly_point_in_time_and_other_dimensions_fail_closed():
    sessions = _sessions(100)
    rows = [*_bars("000300", sessions, base=100, step=0.05),
            *_bars("600000", sessions, base=10, step=0.1)]
    seen_max_dates = []
    seen_lengths = []

    def scorer(_code, history):
        seen_max_dates.append(history[-1]["trading_date"])
        seen_lengths.append(len(history))
        assert all(row["trading_date"] <= history[-1]["trading_date"] for row in history)
        return {"score": 7.0, "detail": "test"}

    artifact = replay.replay(
        rows, policy=_policy(), policy_sha256="policy-test", score_adapter=scorer
    )

    assert artifact["evidence_class"] == "exploratory_reconstruction"
    assert artifact["dimensions"]["technical"]["available"] is True
    assert artifact["dimensions"]["sentiment"]["qualification"] == "unavailable"
    assert artifact["dimensions"]["catalyst"]["qualification"] == "unavailable"
    assert artifact["dimensions"]["deep"]["qualification"] == "unavailable"
    assert artifact["weight_calibration"]["status"] == "unavailable"
    assert artifact["research_gate_eligible"] is False
    assert artifact["automatic_live_weight_change"] is False
    assert len(artifact["implementation_bindings"]["technical_scorer_sha256"]) == 64
    assert artifact["implementation_bindings"]["technical_adapter_semantics"] == (
        replay.TECHNICAL_ADAPTER_SEMANTICS
    )
    assert artifact["samples"]
    assert seen_max_dates
    assert set(seen_lengths) == {60}
    assert all(row["feature_bar_max_date"] == row["decision_date"] for row in artifact["samples"])


def test_settlement_uses_next_open_exact_horizon_and_same_benchmark_sessions():
    sessions = _sessions(100)
    rows = [*_bars("000300", sessions, base=100, step=0.05),
            *_bars("600000", sessions, base=10, step=0.1)]
    artifact = replay.replay(
        rows,
        policy=_policy(),
        policy_sha256="policy-test",
        score_adapter=lambda _code, _history: {"score": 7.0, "detail": "test"},
    )
    t1 = next(row for row in artifact["samples"] if row["horizon_sessions"] == 1)
    t3 = next(row for row in artifact["samples"] if row["horizon_sessions"] == 3
              and row["decision_date"] == t1["decision_date"])
    decision_index = sessions.index(t1["decision_date"])

    assert t1["entry_date"] == sessions[decision_index + 1]
    assert t1["exit_date"] == sessions[decision_index + 1]
    assert t3["entry_date"] == sessions[decision_index + 1]
    assert t3["exit_date"] == sessions[decision_index + 3]
    assert t1["entry_price_after_slippage"] > t1["entry_price_raw"]
    assert t1["exit_price_after_slippage"] < t1["exit_price_raw"]
    assert t1["net_forward_return"] < t1["gross_forward_return"]
    assert artifact["dataset_validation"]["status"] == "valid"


def test_walk_forward_has_horizon_purge_and_only_test_dates_are_scored():
    sessions = _sessions(100)
    rows = [*_bars("000300", sessions, base=100), *_bars("600000", sessions, base=10)]
    artifact = replay.replay(
        rows,
        policy=_policy(),
        policy_sha256="policy-test",
        score_adapter=lambda _code, _history: {"score": 7.0},
    )
    folds = artifact["split"]["folds"]
    assert all(fold["purge"] >= 3 for fold in folds)
    test_dates = {
        sessions[index]
        for fold in folds
        for index in range(fold["test_start"], fold["test_end"])
    }
    assert {row["decision_date"] for row in artifact["samples"]} <= test_dates
    assert artifact["control_counts"]["unresolved_outcomes"] == 0


def test_immutable_artifact_is_content_addressed_and_tamper_evident(tmp_path):
    sessions = _sessions(100)
    rows = [*_bars("000300", sessions, base=100), *_bars("600000", sessions, base=10)]
    artifact = replay.replay(
        rows,
        policy=_policy(),
        policy_sha256="policy-test",
        score_adapter=lambda _code, _history: {"score": 7.0},
    )
    first = replay.immutable_write(tmp_path, artifact)
    second = replay.immutable_write(tmp_path, artifact)
    assert first == second
    assert first.name == f"{artifact['artifact_sha256']}.json"

    changed = json.loads(first.read_text(encoding="utf-8"))
    changed["research_only"] = False
    with pytest.raises(ValueError, match="artifact_sha256_mismatch"):
        replay.immutable_write(tmp_path, changed)


def test_policy_rejects_purge_shorter_than_t3(tmp_path):
    policy = _policy()
    policy["walk_forward"]["purge"] = 2
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(ValueError, match="purge"):
        replay._policy(path)


def test_policy_rejects_variants_with_different_names_but_identical_rules(tmp_path):
    policy = _policy()
    policy["variants"] = [
        {"variant_id": "alias_a", "minimum_score": 6.0, "daily_top_n": 20},
        {"variant_id": "alias_b", "minimum_score": 6.0, "daily_top_n": 20},
    ]
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(ValueError, match="redundant variant policy"):
        replay._policy(path)


def test_realised_identical_sample_sets_are_marked_redundant():
    sessions = _sessions(100)
    rows = [*_bars("000300", sessions, base=100), *_bars("600000", sessions, base=10)]
    policy = _policy()
    policy["variants"] = [
        {"variant_id": "top_5", "minimum_score": -3.0, "daily_top_n": 5},
        {"variant_id": "top_10", "minimum_score": -3.0, "daily_top_n": 10},
    ]
    artifact = replay.replay(
        rows,
        policy=policy,
        policy_sha256="policy-test",
        score_adapter=lambda _code, _history: {"score": 7.0},
    )
    comparison = artifact["variant_comparison"]
    assert comparison["status"] == "redundant"
    assert comparison["comparison_eligible"] is False
    assert comparison["redundant_groups"][0]["variant_ids"] == ["top_10", "top_5"]
    statuses = {artifact["variant_metrics"][vid]["comparison"]["status"] for vid in ("top_5", "top_10")}
    assert statuses == {"redundant", "representative_or_distinct"}


def test_rank_capacity_variants_produce_distinct_sample_sets_when_universe_allows():
    sessions = _sessions(100)
    rows = [*_bars("000300", sessions, base=100)]
    for index in range(3):
        rows.extend(_bars(f"60000{index}", sessions, base=10 + index))
    policy = _policy()
    policy["variants"] = [
        {"variant_id": "top_1", "minimum_score": -3.0, "daily_top_n": 1},
        {"variant_id": "top_2", "minimum_score": -3.0, "daily_top_n": 2},
    ]
    artifact = replay.replay(
        rows,
        policy=policy,
        policy_sha256="policy-test",
        score_adapter=lambda code, _history: {"score": float(code[-1]) + 5.0},
    )
    comparison = artifact["variant_comparison"]
    assert comparison["status"] == "distinct"
    assert comparison["comparison_eligible"] is True
    assert comparison["sample_count_by_variant"]["top_2"] == (
        2 * comparison["sample_count_by_variant"]["top_1"]
    )


def _numeric_leaves(value, prefix=""):
    leaves = {}
    if isinstance(value, dict):
        for key, item in value.items():
            leaves.update(_numeric_leaves(item, f"{prefix}.{key}" if prefix else key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            leaves.update(_numeric_leaves(item, f"{prefix}[{index}]"))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        leaves[prefix] = value
    return leaves


def test_disabling_display_only_chan_is_numerically_equivalent():
    sessions = _sessions(60)
    bars = _bars("600000", sessions, base=10, step=0.1)
    # Load the isolated canonical scorer Adapter, which installs the historical
    # fail-closed registry policy and disables Chan by default.
    replay._canonical_score_adapter("600000", bars)
    module = replay._SCORER_MODULE

    class FakeChan:
        @staticmethod
        def analyze(_bars):
            return {"signals": [{"idx": 59, "type": "third_buy", "strategy_id": "legacy"}]}

    quote = {
        "price": bars[-1]["close"],
        "change_pct": bars[-1]["pct_chg"],
        "turnover": bars[-1]["turn"],
        "amount": bars[-1]["amount"],
        "provider": "test_qfq",
        "asof": bars[-1]["trading_date"],
    }
    original_chan = module._chan
    try:
        module._chan = FakeChan()
        display_only = module.score_technical("600000", "test", quote=quote, klines=bars)
        module._chan = None
        disabled = module.score_technical("600000", "test", quote=quote, klines=bars)
    finally:
        module._chan = original_chan

    assert "未过闸" in display_only["detail"]
    assert "未过闸" not in disabled["detail"]
    assert _numeric_leaves(display_only) == _numeric_leaves(disabled)
    assert disabled["score"] == display_only["score"]
    assert replay.TECHNICAL_ADAPTER_SEMANTICS["chan_structure"].startswith("disabled_")


def test_default_policy_bounds_full_history_to_35_held_out_cross_sections():
    policy_path = Path(__file__).resolve().parents[1] / "config" / "four_dim_pit_replay.json"
    policy, _digest = replay._policy(policy_path)
    folds = replay.build_walk_forward_folds(268 - max(policy["horizons"]), **policy["walk_forward"])
    assert len(folds) == 7
    assert sum(fold["test_end"] - fold["test_start"] for fold in folds) == 35
    assert policy["technical_lookback_sessions"] == 60
