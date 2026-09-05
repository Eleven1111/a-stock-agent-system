"""Freeze Strategy Shadow predictions and settle immutable forward samples.

Public interface stays deliberately small:

``run`` freezes the supplied Shadow artifact and advances every pending
prediction through its configured horizons. ``build_gate_dataset`` projects
only final, primary-horizon, approved samples for the research gate.

This store is research-only and intentionally separate from signal_ledger.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from paths import data_file
from research_artifact import json_sha256
import local_market_history
import dataset_contract
import forward_label_taxonomy
from execution_model import net_return_pct


POLICY_SCHEMA = "strategy_forward_settlement_policy_v1"
PREDICTION_SCHEMA = "strategy_forward_prediction_v1"
RUN_SCHEMA = "strategy_forward_settlement_run_v1"
SETTLEMENT_SCHEMA = "settled_forward_sample_v1"
ENGINE_VERSION = "strategy-forward-settlement-v1"
CATALOG_PATH = Path(__file__).resolve().parents[2] / "config" / "dataset_catalog.json"


def artifact_sha256(value: Mapping[str, Any]) -> str:
    """Hash an artifact without its self-referential ``*_sha256`` field."""
    body = dict(value)
    for key in ("prediction_sha256", "settlement_sha256", "terminal_sha256", "dataset_sha256"):
        body.pop(key, None)
    return json_sha256(body)


def _root() -> Path:
    return Path(data_file("stock-triage", "strategy_forward"))


def _load_object(path: str | Path) -> dict[str, Any]:
    with open(os.path.abspath(os.path.expanduser(str(path))), encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _policy(path: str) -> tuple[dict[str, Any], str]:
    value = _load_object(path)
    if value.get("schema") != POLICY_SCHEMA:
        raise ValueError("unsupported forward settlement policy")
    if value.get("entry_rule") != "next_trading_session_open_reference":
        raise ValueError("entry rule must be next_trading_session_open_reference")
    if value.get("horizons") != [1, 3]:
        raise ValueError("forward settlement horizons must be [1, 3]")
    semantic = {key: item for key, item in value.items() if key != "approved_policy_hashes"}
    return value, json_sha256(semantic)


def _immutable_write(path: Path, artifact: Mapping[str, Any]) -> bool:
    """Create once; identical reruns are idempotent and conflicts fail hard."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if _load_object(path) == dict(artifact):
            return False
        raise ValueError(f"immutable artifact conflict: {path}")
    encoded = json.dumps(artifact, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _load_object(path) == dict(artifact):
                return False
            raise ValueError(f"immutable artifact conflict: {path}")
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return True


def _six_digit_code(value: Any) -> str | None:
    code = str(value or "").strip()
    return code if len(code) == 6 and code.isdigit() else None


def _prediction_path(decision_date: str, strategy_id: str, code: str) -> Path:
    return _root() / "predictions" / decision_date / strategy_id / f"{code}.json"


def load_predictions() -> list[dict[str, Any]]:
    directory = _root() / "predictions"
    if not directory.exists():
        return []
    return [_load_object(path) for path in sorted(directory.glob("*/*/*.json"))]


def load_settlements() -> list[dict[str, Any]]:
    directory = _root() / "settlements"
    if not directory.exists():
        return []
    return [_load_object(path) for path in sorted(directory.glob("*/*.json"))]


def load_terminal_unresolved() -> list[dict[str, Any]]:
    directory = _root() / "terminal_unresolved"
    if not directory.exists():
        return []
    return [_load_object(path) for path in sorted(directory.glob("*.json"))]


def _freeze(shadow: Mapping[str, Any], policy: Mapping[str, Any], policy_hash: str) -> tuple[int, Counter]:
    decision_date = str(shadow.get("asof") or "")[:10]
    observed_at = str(shadow.get("generated_at") or "")
    if not decision_date or not observed_at:
        raise ValueError("shadow asof and generated_at are required")
    qualifications = shadow.get("evidence_qualification") or {}
    bindings = shadow.get("strategy_rule_bindings") or {}
    frozen = 0
    rejected: Counter = Counter()
    for strategy_id, strategy in (shadow.get("strategies") or {}).items():
        if not isinstance(strategy, Mapping) or strategy.get("status") != "signal":
            continue
        rows = [row for row in strategy.get("results") or []
                if isinstance(row, Mapping) and row.get("status") == "signal"]
        if strategy.get("forward_settlement_eligible") is not True:
            rejected["forward_settlement_ineligible"] += len(rows) or 1
            continue
        qualification = qualifications.get(strategy_id) or {}
        if (qualification.get("class") != "canonical_forward"
                or qualification.get("canonical_forward_eligible") is not True):
            rejected["evidence_not_canonical_forward"] += len(rows) or 1
            continue
        binding = bindings.get(strategy_id) or {}
        rules_hash = str(binding.get("sha256") or "")
        if not rules_hash:
            rejected["strategy_rules_unbound"] += len(rows) or 1
            continue
        for result in rows:
            if strategy_id == "ice_point_reversal":
                binding = result.get("tradeable_leader_binding")
                try:
                    score = float((binding or {}).get("leader_score_shadow"))
                    threshold = float((binding or {}).get("leader_score_threshold"))
                except (TypeError, ValueError):
                    score = threshold = -1.0
                if (result.get("forward_settlement_eligible") is not True
                        or not isinstance(binding, Mapping)
                        or str(binding.get("code") or "") != str(result.get("code") or "")
                        or binding.get("leader_confirmed") is not True
                        or threshold != 80.0 or score < threshold):
                    rejected["tradeable_leader_binding_unavailable"] += 1
                    continue
            code = _six_digit_code(result.get("code"))
            if code is None:
                rejected["non_tradeable_entity"] += 1
                continue
            result_hash = json_sha256(result)
            decision_id = json_sha256({
                "decision_date": decision_date, "strategy_id": strategy_id,
                "entity_id": code, "shadow_sha256": shadow.get("result_sha256"),
                "strategy_result_sha256": result_hash,
                "strategy_rules_sha256": rules_hash,
                "settlement_policy_sha256": policy_hash,
            })
            primary = int((policy.get("primary_horizon_by_strategy") or {}).get(strategy_id, 1))
            prediction: dict[str, Any] = {
                "schema": PREDICTION_SCHEMA,
                "engine_version": ENGINE_VERSION,
                "decision_id": decision_id,
                "decision_date": decision_date,
                "observed_at": observed_at,
                "strategy_id": str(strategy_id),
                "entity_id": code,
                "direction": "long",
                "entry_rule": policy["entry_rule"],
                "horizons": list(policy["horizons"]),
                "primary_horizon": primary,
                "benchmark": dict(policy["benchmark"]),
                "cost_model": dict(policy["cost_model"]),
                "settlement_policy_version": policy["version"],
                "settlement_policy_sha256": policy_hash,
                "strategy_rules_version": binding.get("version"),
                "strategy_rules_sha256": rules_hash,
                "strategy_result": dict(result),
                "strategy_result_sha256": result_hash,
                "shadow_path": str(shadow.get("artifact_path") or ""),
                "shadow_sha256": str(shadow.get("result_sha256") or ""),
                "evidence_path": str(shadow.get("input_path") or ""),
                "evidence_sha256": str(shadow.get("input_sha256") or ""),
                "evidence_class": "canonical_forward",
                "research_only": True,
                "execution_eligible": False,
                "live_order_sent": False,
            }
            prediction["prediction_sha256"] = artifact_sha256(prediction)
            if _immutable_write(_prediction_path(decision_date, str(strategy_id), code), prediction):
                frozen += 1
    return frozen, rejected


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _bars_by_code(codes: list[str], asof: str) -> dict[str, list[dict[str, Any]]]:
    rows = local_market_history.get_daily_bars(codes, asof, 40, adjust_flag="qfq")
    grouped = {code: [] for code in codes}
    for row in rows:
        grouped.setdefault(str(row.get("code")), []).append(dict(row))
    return grouped


def _settlement_path(decision_id: str, horizon: int) -> Path:
    return _root() / "settlements" / decision_id / f"t{horizon}.json"


def _terminal_path(decision_id: str) -> Path:
    return _root() / "terminal_unresolved" / f"{decision_id}.json"


def _after(rows: list[dict[str, Any]], decision_date: str) -> list[dict[str, Any]]:
    return sorted(
        (row for row in rows if str(row.get("trading_date") or "") > decision_date),
        key=lambda row: str(row.get("trading_date") or ""),
    )


def _settle_one(
    prediction: Mapping[str, Any], horizon: int, asof: str,
    policy: Mapping[str, Any], policy_hash: str,
) -> str:
    path = _settlement_path(str(prediction["decision_id"]), horizon)
    if path.exists():
        return "already_final"
    benchmark_code = str((policy.get("benchmark") or {}).get("code") or "")
    grouped = _bars_by_code([str(prediction["entity_id"]), benchmark_code], asof)
    stock = _after(grouped.get(str(prediction["entity_id"]), []), str(prediction["decision_date"]))
    benchmark = _after(grouped.get(benchmark_code, []), str(prediction["decision_date"]))
    if len(stock) < horizon or len(benchmark) < horizon:
        return "pending"
    entry_day = str(stock[0].get("trading_date") or "")
    exit_day = str(stock[horizon - 1].get("trading_date") or "")
    benchmark_entry_day = str(benchmark[0].get("trading_date") or "")
    benchmark_exit_day = str(benchmark[horizon - 1].get("trading_date") or "")
    # Same-session comparison is mandatory; a merely same-length series can
    # conceal a suspension or cache hole and would compare different periods.
    if entry_day != benchmark_entry_day or exit_day != benchmark_exit_day:
        return "pending"
    observed_at = datetime.fromisoformat(str(prediction["observed_at"]))
    if observed_at.date().isoformat() >= entry_day:
        raise ValueError("prediction was not frozen before the entry session")
    entry_raw = _positive(stock[0].get("open"))
    exit_raw = _positive(stock[horizon - 1].get("close"))
    bench_entry = _positive(benchmark[0].get("open"))
    bench_exit = _positive(benchmark[horizon - 1].get("close"))
    if None in {entry_raw, exit_raw, bench_entry, bench_exit}:
        return "pending"
    costs = policy["cost_model"]
    entry_price = float(entry_raw) * (1 + float(costs["entry_slippage_bps"]) / 10_000)
    exit_price = float(exit_raw) * (1 - float(costs["exit_slippage_bps"]) / 10_000)
    gross = exit_price / entry_price - 1
    reference_gross = float(exit_raw) / float(entry_raw) - 1
    benchmark_gross = float(bench_exit) / float(bench_entry) - 1
    priced = net_return_pct(
        gross_return_pct=gross * 100,
        notional=float(costs["assumed_notional"]),
        asof=entry_day,
    )
    net = float(priced["net_return_pct"]) / 100
    snapshot = {
        "stock_sessions": stock[:horizon],
        "benchmark_sessions": benchmark[:horizon],
    }
    sample: dict[str, Any] = {
        "schema": SETTLEMENT_SCHEMA,
        "engine_version": ENGINE_VERSION,
        "settlement_id": json_sha256({
            "decision_id": prediction["decision_id"], "horizon": horizon,
            "prediction_sha256": prediction["prediction_sha256"],
            "settlement_policy_sha256": policy_hash,
        }),
        "decision_id": prediction["decision_id"],
        "prediction_sha256": prediction["prediction_sha256"],
        "strategy_id": prediction["strategy_id"],
        "entity_id": prediction["entity_id"],
        "decision_date": prediction["decision_date"],
        "decision_available_at": prediction["observed_at"],
        "horizon_sessions": horizon,
        "is_primary_horizon": horizon == int(prediction["primary_horizon"]),
        "entry_rule": prediction["entry_rule"],
        "entry_date": entry_day,
        "entry_price_raw": float(entry_raw),
        "entry_price_after_slippage": entry_price,
        "exit_date": exit_day,
        "exit_price_raw": float(exit_raw),
        "exit_price_after_slippage": exit_price,
        "reference_forward_return": reference_gross,
        "gross_forward_return": gross,
        "net_forward_return": net,
        "benchmark": {
            "code": benchmark_code, "name": (policy.get("benchmark") or {}).get("name"),
            "entry_date": benchmark_entry_day, "entry_price": float(bench_entry),
            "exit_date": benchmark_exit_day, "exit_price": float(bench_exit),
            "gross_return": benchmark_gross,
        },
        "gross_alpha": gross - benchmark_gross,
        "net_alpha": net - benchmark_gross,
        "cost_model": {**dict(costs), **priced},
        "outcome_available_at": f"{exit_day}T15:00:00+08:00",
        "bar_snapshot": snapshot,
        "bar_snapshot_sha256": json_sha256(snapshot),
        "evidence_class": prediction["evidence_class"],
        "shadow_sha256": prediction["shadow_sha256"],
        "evidence_sha256": prediction["evidence_sha256"],
        "strategy_rules_sha256": prediction["strategy_rules_sha256"],
        "settlement_policy_sha256": policy_hash,
        "status": "final",
        "research_only": True,
        "execution_eligible": False,
    }
    sample["settlement_sha256"] = artifact_sha256(sample)
    _immutable_write(path, sample)
    return "settled"


def _terminal_due(
    prediction: Mapping[str, Any], asof: str, policy: Mapping[str, Any]
) -> tuple[bool, list[str]]:
    benchmark_code = str((policy.get("benchmark") or {}).get("code") or "")
    rows = _bars_by_code([benchmark_code], asof).get(benchmark_code, [])
    sessions = [
        str(row.get("trading_date") or "") for row in rows
        if str(row.get("trading_date") or "") > str(prediction["decision_date"])
    ]
    threshold = max(int(item) for item in prediction.get("horizons") or [3]) + int(
        policy["terminal_grace_sessions"]
    )
    return len(sessions) >= threshold, sorted(sessions)


def _write_terminal(
    prediction: Mapping[str, Any], horizons: list[int], asof: str,
    policy_hash: str, observed_sessions: list[str],
) -> bool:
    terminal: dict[str, Any] = {
        "schema": "settled_forward_terminal_unresolved_v1",
        "engine_version": ENGINE_VERSION,
        "decision_id": prediction["decision_id"],
        "prediction_sha256": prediction["prediction_sha256"],
        "strategy_id": prediction["strategy_id"],
        "entity_id": prediction["entity_id"],
        "decision_date": prediction["decision_date"],
        "primary_horizon": prediction["primary_horizon"],
        "unresolved_horizons": sorted(horizons),
        "reason": "market_data_unavailable_or_session_mismatch",
        "observed_market_sessions": observed_sessions,
        "terminal_asof": asof,
        "settlement_policy_sha256": policy_hash,
        "status": "terminal_unresolved",
        "research_only": True,
        "execution_eligible": False,
    }
    terminal["terminal_sha256"] = artifact_sha256(terminal)
    return _immutable_write(_terminal_path(str(prediction["decision_id"])), terminal)


def _settle(asof: str, policy: Mapping[str, Any], policy_hash: str) -> tuple[int, int, int]:
    settled = 0
    pending = 0
    terminal_count = 0
    for prediction in load_predictions():
        if prediction.get("settlement_policy_sha256") != policy_hash:
            continue
        if _terminal_path(str(prediction["decision_id"])).exists():
            continue
        pending_horizons: list[int] = []
        for horizon in prediction.get("horizons") or []:
            result = _settle_one(prediction, int(horizon), asof, policy, policy_hash)
            settled += result == "settled"
            if result == "pending":
                pending_horizons.append(int(horizon))
        if pending_horizons:
            due, sessions = _terminal_due(prediction, asof, policy)
            if due:
                terminal_count += _write_terminal(
                    prediction, pending_horizons, asof, policy_hash, sessions
                )
            else:
                pending += len(pending_horizons)
    return settled, pending, terminal_count


def run(asof: str, shadow_path: str, *, policy_path: str) -> dict[str, Any]:
    """Freeze one Shadow artifact and advance pending settlements."""
    policy, policy_hash = _policy(policy_path)
    shadow = _load_object(shadow_path)
    if shadow.get("schema") != "strategy_shadow_daily_v1":
        raise ValueError("strategy_shadow_daily_v1 required")
    shadow = {**shadow, "artifact_path": os.path.abspath(os.path.expanduser(shadow_path))}
    frozen, rejected = _freeze(shadow, policy, policy_hash)
    settled, pending, terminal_count = _settle(str(asof), policy, policy_hash)
    return {
        "schema": RUN_SCHEMA,
        "asof": str(asof),
        "policy_sha256": policy_hash,
        "frozen": frozen,
        "settled": settled,
        "pending": pending,
        "terminal_unresolved": terminal_count,
        "rejected": dict(sorted(rejected.items())),
        "research_only": True,
        "trading_action": "none",
    }


def build_gate_dataset(strategy_id: str, *, policy_path: str) -> dict[str, Any]:
    """Project only approved final primary samples, failing closed on coverage."""
    policy, policy_hash = _policy(policy_path)
    if policy_hash not in set(policy.get("approved_policy_hashes") or []):
        raise ValueError("settlement_policy_not_approved")
    approved_rules = set(
        (policy.get("approved_strategy_rule_hashes") or {}).get(strategy_id) or []
    )
    if not approved_rules:
        raise ValueError("strategy_rules_not_approved")
    predictions = []
    for row in load_predictions():
        if row.get("strategy_id") != strategy_id:
            continue
        if row.get("prediction_sha256") != artifact_sha256(row):
            raise ValueError("prediction_hash_mismatch")
        if (row.get("settlement_policy_sha256") == policy_hash
                and row.get("strategy_rules_sha256") in approved_rules
                and row.get("evidence_class") == "canonical_forward"):
            predictions.append(row)
    if not predictions:
        raise ValueError("no_eligible_forward_predictions")
    by_prediction = {row["prediction_sha256"]: row for row in predictions}
    samples = []
    for row in load_settlements():
        if row.get("prediction_sha256") not in by_prediction:
            continue
        if row.get("settlement_sha256") != artifact_sha256(row):
            raise ValueError("settlement_hash_mismatch")
        if row.get("bar_snapshot_sha256") != json_sha256(row.get("bar_snapshot") or {}):
            raise ValueError("bar_snapshot_hash_mismatch")
        if (row.get("status") == "final"
                and row.get("is_primary_horizon") is True
                and row.get("evidence_class") == "canonical_forward"
                and row.get("settlement_policy_sha256") == policy_hash
                and row.get("strategy_rules_sha256") in approved_rules):
            samples.append(row)
    terminal_rows = [
        row for row in load_terminal_unresolved()
        if row.get("prediction_sha256") in by_prediction
        and int(row.get("primary_horizon")) in set(row.get("unresolved_horizons") or [])
    ]
    coverage = (len(samples) + len(terminal_rows)) / len(predictions)
    if coverage < float(policy["minimum_coverage_ratio"]):
        raise ValueError(
            "settled_forward_coverage_insufficient:"
            f"{len(samples) + len(terminal_rows)}/{len(predictions)}"
        )
    ambiguity = len(terminal_rows) / len(predictions)
    if ambiguity > float(policy["maximum_terminal_ambiguity_ratio"]):
        raise ValueError(
            f"settled_forward_terminal_ambiguity:{len(terminal_rows)}/{len(predictions)}"
        )
    rows = []
    for sample in sorted(samples, key=lambda row: (row["decision_date"], row["entity_id"])):
        prediction = by_prediction[sample["prediction_sha256"]]
        rows.append({
            "entity_id": sample["entity_id"],
            "strategy_id": strategy_id,
            "decision_id": sample["decision_id"],
            "src": sample["decision_date"],
            "decision_available_at": sample["decision_available_at"],
            "entry_date": sample["entry_date"],
            "dst": sample["exit_date"],
            "outcome_available_at": sample["outcome_available_at"],
            "horizon_sessions": sample["horizon_sessions"],
            "is_primary_horizon": True,
            "gross_forward_return": sample["gross_forward_return"],
            "net_forward_return": sample["net_forward_return"],
            "benchmark_forward_return": sample["benchmark"]["gross_return"],
            "gross_alpha": sample["gross_alpha"],
            "net_alpha": sample["net_alpha"],
            "prediction_ref": prediction["decision_id"],
            "prediction_sha256": sample["prediction_sha256"],
            "shadow_sha256": sample["shadow_sha256"],
            "evidence_sha256": sample["evidence_sha256"],
            "strategy_rules_sha256": sample["strategy_rules_sha256"],
            "settlement_policy_sha256": policy_hash,
            "bar_snapshot_sha256": sample["bar_snapshot_sha256"],
        })
    dataset: dict[str, Any] = {
        "schema": "settled_forward_samples_v1",
        "dataset_id": "settled_forward_samples_v1",
        "engine_version": ENGINE_VERSION,
        # These rows measure a price path, not a tradeable result: the primary
        # horizon of 1 exits at the close of the session it entered on. Consumers
        # asking "can this be traded?" must go through
        # forward_label_taxonomy.assert_execution_evidence and will be refused.
        "label_kind": forward_label_taxonomy.LABEL_PRICE_PATH,
        "execution_evidence": False,
        "research_clock": (
            forward_label_taxonomy.describe_price_path_label(samples[0], policy)
            if samples else None
        ),
        "strategy_id": strategy_id,
        "settlement_policy_sha256": policy_hash,
        "approved_strategy_rule_hashes": sorted(approved_rules),
        "considered": len(predictions),
        "terminal_unresolved": len(terminal_rows),
        "terminal_ambiguity_ratio": ambiguity,
        "coverage_ratio": coverage,
        "rows": rows,
        "research_only": True,
        "execution_eligible": False,
    }
    catalog = dataset_contract.load_catalog(CATALOG_PATH)
    contract = dataset_contract.resolve_dataset(catalog, "settled_forward_samples_v1")
    dataset["contract_hash"] = contract["contract_hash"]
    dataset["catalog_hash"] = catalog["catalog_hash"]
    dataset["validation"] = dataset_contract.validate_records(
        rows, contract, coverage_ratio=coverage
    )
    dataset["dataset_sha256"] = artifact_sha256(dataset)
    return dataset


__all__ = [
    "artifact_sha256", "build_gate_dataset", "load_predictions", "load_settlements",
    "load_terminal_unresolved", "run",
]
