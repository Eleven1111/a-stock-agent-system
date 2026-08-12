"""Materialize catalog datasets from runtime artifacts.

The catalog declares what a dataset *means*; until something emits rows that
satisfy that contract, an `analysis_plan` node has nothing to read. This module
is that missing half: it projects two already-accumulating runtime artifacts
into contract-conforming rows and refuses to emit anything the contract would
reject.

Two rules run through every projection here:

- **Point-in-time or nothing.** A row is built only from values observable at
  its own feature cutoff; the outcome timestamp is derived from the outcome
  date, never from "now".
- **An empty projection is a failure, not full coverage.** Zero candidates
  raises instead of reporting ``coverage_ratio = 1.0`` over an empty set —
  a ratio computed on nothing has burned this repository before.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence

from dataset_contract import DatasetContractError, validate_records
from execution_model import net_return_pct


DIRECTION_DATASET_ID = "cross_sectional_direction_rows_v1"
SETTLED_DATASET_ID = "settled_signal_outcomes_v1"

# A 股收盘 15:00；结果的可得时点由结果日推出，绝不用 datetime.now()——否则
# 同一份历史数据在不同运行日会产出不同的行。
MARKET_CLOSE_LOCAL_TIME = "T15:00:00+08:00"
# 已结算信号不记手数，税后收益必须挂显式名义本金，与 performance_tracker 同口径。
SETTLEMENT_NOTIONAL = 20000.0


def _outcome_available_at(outcome_end: str) -> str:
    return f"{str(outcome_end)[:10]}{MARKET_CLOSE_LOCAL_TIME}"


def _as_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _close_on_or_before(bars: Sequence[Mapping[str, Any]], cutoff: date):
    """Latest bar at or before ``cutoff`` — the last close observable then."""
    best = None
    for bar in bars:
        bar_day = _as_date(bar.get("date"))
        try:
            close = float(bar.get("close"))
        except (TypeError, ValueError):
            continue
        if bar_day is None or close <= 0 or bar_day > cutoff:
            continue
        if best is None or bar_day > best[0]:
            best = (bar_day, close)
    return best


def _forward_bar(bars: Sequence[Mapping[str, Any]], cutoff: date, horizon: int):
    """The ``horizon``-th trading bar strictly after ``cutoff``."""
    later = sorted(
        (day, float(bar["close"]))
        for bar in bars
        if (day := _as_date(bar.get("date"))) is not None
        and day > cutoff
        and _positive_close(bar)
    )
    if len(later) < horizon:
        return None
    return later[horizon - 1]


def _positive_close(bar: Mapping[str, Any]) -> bool:
    try:
        return float(bar.get("close")) > 0
    except (TypeError, ValueError):
        return False


def _direction_row(
    candidate: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    *,
    cutoff: date,
    generated_at: str,
    snapshot_ref: str,
    horizon_days: int,
) -> dict[str, Any] | None:
    entry = _close_on_or_before(bars, cutoff)
    forward = _forward_bar(bars, cutoff, horizon_days)
    if entry is None or forward is None:
        return None
    outcome_day, outcome_close = forward
    return {
        "entity_id": str(candidate.get("code") or ""),
        "src": cutoff.isoformat(),
        "dst": outcome_day.isoformat(),
        "score": float(candidate.get("score") or 0.0),
        "forward_return": outcome_close / entry[1] - 1.0,
        "score_available_at": str(generated_at),
        "outcome_available_at": _outcome_available_at(outcome_day.isoformat()),
        "snapshot_ref": snapshot_ref,
    }


def build_direction_rows(
    snapshots: Sequence[Mapping[str, Any]],
    bars_by_code: Mapping[str, Sequence[Mapping[str, Any]]],
    contract: Mapping[str, Any],
    *,
    horizon_days: int = 1,
) -> dict[str, Any]:
    """Project research snapshots plus later bars into direction dataset rows."""
    if horizon_days < 1:
        raise DatasetContractError("horizon_days_invalid")
    rows: list[dict[str, Any]] = []
    considered = 0
    for snapshot in snapshots:
        cutoff = _as_date(snapshot.get("date"))
        snapshot_ref = str(snapshot.get("snapshot_sha256") or "").strip()
        generated_at = str(snapshot.get("generated_at") or "")
        for candidate in snapshot.get("candidates") or []:
            considered += 1
            if cutoff is None or not snapshot_ref:
                continue
            row = _direction_row(
                candidate,
                bars_by_code.get(str(candidate.get("code") or ""), []),
                cutoff=cutoff,
                generated_at=generated_at,
                snapshot_ref=snapshot_ref,
                horizon_days=horizon_days,
            )
            if row is not None:
                rows.append(row)
    return _sealed_payload(rows, considered, contract)


def _settled_row(
    record: Mapping[str, Any], *, notional: float
) -> dict[str, Any] | None:
    cutoff = _as_date(record.get("signal_date"))
    outcome_day = _as_date(record.get("settled_on"))
    gross = record.get("t1_close_ret")
    snapshot_ref = str(record.get("snapshot_ref") or record.get("signal_id") or "").strip()
    if cutoff is None or outcome_day is None or gross is None or not snapshot_ref:
        return None
    if outcome_day < cutoff:
        return None
    try:
        priced = net_return_pct(
            gross_return_pct=float(gross),
            notional=notional,
            asof=cutoff.isoformat(),
        )
    except ValueError:
        return None
    return {
        "entity_id": str(record.get("code") or ""),
        "strategy_id": str(record.get("strategy_id") or "default"),
        "src": cutoff.isoformat(),
        "dst": outcome_day.isoformat(),
        "score": float(record.get("score") or 0.0),
        "forward_return": float(gross) / 100.0,
        "net_forward_return": float(priced["net_return_pct"]) / 100.0,
        "score_available_at": str(record.get("recorded_at") or f"{cutoff.isoformat()}T09:30:00+08:00"),
        "outcome_available_at": _outcome_available_at(outcome_day.isoformat()),
        "snapshot_ref": snapshot_ref,
    }


def build_settled_signal_rows(
    records: Sequence[Mapping[str, Any]],
    contract: Mapping[str, Any],
    *,
    notional: float = SETTLEMENT_NOTIONAL,
) -> dict[str, Any]:
    """Project settled ledger signals into gross/net outcome rows per strategy."""
    settled = [
        record for record in records
        if isinstance(record, Mapping) and record.get("t1_close_ret") is not None
    ]
    rows = [
        row for row in (_settled_row(record, notional=notional) for record in settled)
        if row is not None
    ]
    return _sealed_payload(rows, len(settled), contract)


def _sealed_payload(
    rows: Sequence[Mapping[str, Any]],
    considered: int,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate against the contract; an empty projection fails closed."""
    if considered <= 0:
        raise DatasetContractError("no_source_records")
    if not rows:
        raise DatasetContractError("no_projectable_records")
    coverage_ratio = len(rows) / considered
    validation = validate_records(rows, contract, coverage_ratio=coverage_ratio)
    return {
        "schema": "dataset_projection_v1",
        "dataset_id": validation["dataset_id"],
        "contract_hash": validation["contract_hash"],
        "rows": list(rows),
        "considered": considered,
        "coverage_ratio": coverage_ratio,
        "validation": validation,
    }
