"""Cached institutional/chip evidence shared by policy and Serenity research."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from eastmoney_intelligence import (
    fetch_block_trades,
    fetch_dragon_tiger,
    fetch_holder_changes,
    fetch_lockups,
    fetch_margin_trading,
    fetch_reports,
    source_metadata,
)
from paths import cache_dir
from state_store import atomic_write_json, read_json


SCHEMA = "stock_intelligence_v2"
LEGACY_SCHEMAS = {"stock_intelligence_v1"}
DEFAULT_MAX_AGE_DAYS = 7
DATASET_POLICIES = {
    "lockups": {
        "required": True,
        "max_query_age_days": 4,
        "max_record_age_days": None,
        "allow_future_record": True,
    },
    "margin_trading": {
        "required": True,
        "max_query_age_days": 4,
        "max_record_age_days": 7,
        "allow_future_record": False,
    },
    "holder_changes": {
        "required": True,
        "max_query_age_days": 4,
        "max_record_age_days": 180,
        "allow_future_record": False,
    },
    "dragon_tiger": {
        "required": False,
        "max_query_age_days": 4,
        "max_record_age_days": 45,
        "allow_future_record": False,
    },
    "block_trades": {
        "required": False,
        "max_query_age_days": 4,
        "max_record_age_days": 45,
        "allow_future_record": False,
    },
    "reports": {
        "required": False,
        "max_query_age_days": 7,
        "max_record_age_days": 365,
        "allow_future_record": False,
    },
}
REQUIRED_DATASETS = tuple(
    name for name, policy in DATASET_POLICIES.items() if policy["required"]
)


def cache_file(code: str) -> str:
    return os.path.join(
        cache_dir("stock-triage"),
        "market_intelligence",
        f"{str(code).zfill(6)}.json",
    )


def last_good_file(code: str) -> str:
    return os.path.join(
        cache_dir("stock-triage"),
        "market_intelligence",
        "last_good",
        f"{str(code).zfill(6)}.json",
    )


def _current(value: date | str | None) -> date:
    return date.fromisoformat(str(value or date.today())[:10])


def _pct_change(latest: float, baseline: float) -> float | None:
    if not baseline:
        return None
    return (latest / baseline - 1) * 100


def _parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _dataset_rows(dataset: str, value: Any) -> list[dict[str, Any]]:
    if dataset == "lockups":
        lockups = value if isinstance(value, dict) else {}
        return [
            row
            for bucket in ("history", "upcoming")
            for row in (lockups.get(bucket) or [])
            if isinstance(row, dict)
        ]
    if dataset == "dragon_tiger":
        value = value if isinstance(value, dict) else {}
        return [
            row for row in (value.get("records") or []) if isinstance(row, dict)
        ]
    return [row for row in (value or []) if isinstance(row, dict)]


def _latest_record_date(dataset: str, value: Any) -> str | None:
    dates = [
        str(row.get("date"))[:10]
        for row in _dataset_rows(dataset, value)
        if _parse_day(row.get("date"))
    ]
    return max(dates) if dates else None


def _dataset_status(
    dataset: str,
    value: Any,
    *,
    queried_asof: date,
    error: dict[str, str] | None = None,
) -> dict[str, Any]:
    policy = DATASET_POLICIES[dataset]
    rows = _dataset_rows(dataset, value)
    return {
        "provider": "eastmoney",
        "status": "error" if error else ("ok" if rows else "empty"),
        "required": bool(policy["required"]),
        "queried_asof": queried_asof.isoformat(),
        "latest_record_date": _latest_record_date(dataset, value),
        "max_query_age_days": policy["max_query_age_days"],
        "max_record_age_days": policy["max_record_age_days"],
        "error": error,
    }


def _status_is_fresh(
    dataset: str,
    status: dict[str, Any],
    current: date,
) -> bool:
    policy = DATASET_POLICIES[dataset]
    if status.get("status") not in {"ok", "empty"}:
        return False
    queried = _parse_day(status.get("queried_asof"))
    if queried is None:
        return False
    query_age = (current - queried).days
    if query_age < 0 or query_age > int(policy["max_query_age_days"]):
        return False
    latest = _parse_day(status.get("latest_record_date"))
    max_record_age = policy["max_record_age_days"]
    if latest is None or max_record_age is None:
        return True
    record_age = (current - latest).days
    if record_age < 0:
        return bool(policy["allow_future_record"])
    return record_age <= int(max_record_age)


def _quality_from_statuses(
    statuses: dict[str, dict[str, Any]],
    *,
    current: date,
    errors: list[dict[str, str]] | None = None,
    global_stale: bool = False,
) -> dict[str, Any]:
    missing = sorted(
        name
        for name in DATASET_POLICIES
        if (statuses.get(name) or {}).get("status") not in {"ok", "empty"}
    )
    stale = sorted(
        name
        for name in DATASET_POLICIES
        if name not in missing
        and (global_stale or not _status_is_fresh(name, statuses.get(name) or {}, current))
    )
    missing_required = sorted(set(missing).intersection(REQUIRED_DATASETS))
    stale_required = sorted(set(stale).intersection(REQUIRED_DATASETS))
    directional_ready = not missing_required and not stale_required
    return {
        "status": "complete" if not missing and not stale else "partial",
        "missing_datasets": missing,
        "stale_datasets": stale,
        "missing_required_datasets": missing_required,
        "stale_required_datasets": stale_required,
        "directional_ready": directional_ready,
        "errors": list(errors or []),
    }


def assess_risks(
    payload: dict[str, Any],
    *,
    asof: date | str | None = None,
) -> dict[str, Any]:
    current = _current(asof)
    hard_risks: list[str] = []
    warnings: list[str] = []
    positives: list[str] = []
    details: dict[str, Any] = {}
    missing_datasets = list(
        (payload.get("data_quality") or {}).get("missing_datasets") or []
    )
    stale_datasets = list(
        (payload.get("data_quality") or {}).get("stale_datasets") or []
    )
    warnings.extend(f"missing_dataset:{item}" for item in missing_datasets)
    warnings.extend(f"stale_dataset:{item}" for item in stale_datasets)

    upcoming = list((payload.get("lockups") or {}).get("upcoming") or [])
    near_lockups = []
    for row in upcoming:
        try:
            days = (date.fromisoformat(str(row.get("date"))[:10]) - current).days
        except (TypeError, ValueError):
            continue
        if 0 <= days <= 30:
            near_lockups.append({**row, "days_until": days})
    max_ratio = max(
        (float(row.get("ratio_pct") or 0) for row in near_lockups),
        default=0.0,
    )
    details["lockup_30d_max_ratio_pct"] = round(max_ratio, 2)
    if max_ratio >= 10:
        hard_risks.append("major_lockup_within_30d")
    elif max_ratio >= 3:
        warnings.append("material_lockup_within_30d")

    margin = list(payload.get("margin_trading") or [])
    if len(margin) >= 2:
        latest = float(margin[0].get("financing_balance") or 0)
        baseline = float(margin[min(len(margin) - 1, 5)].get("financing_balance") or 0)
        growth = _pct_change(latest, baseline)
        details["financing_balance_change_pct"] = (
            round(growth, 2) if growth is not None else None
        )
        if growth is not None and growth >= 15:
            warnings.append("financing_balance_surge")
        elif growth is not None and growth <= -15:
            warnings.append("financing_balance_contraction")

    holders = list(payload.get("holder_changes") or [])
    recent_holder_moves = [
        float(row.get("holder_change_pct") or 0) for row in holders[:2]
    ]
    details["recent_holder_change_pct"] = recent_holder_moves
    if len(recent_holder_moves) >= 2 and all(value >= 3 for value in recent_holder_moves):
        warnings.append("holder_count_rising")
    elif len(recent_holder_moves) >= 2 and all(value <= -3 for value in recent_holder_moves):
        positives.append("holder_count_falling")

    institution = (payload.get("dragon_tiger") or {}).get("institution") or {}
    institution_net = float(institution.get("net_amount_wan") or 0)
    details["institution_lhb_net_wan"] = institution_net
    if institution_net <= -5000:
        warnings.append("institutional_lhb_net_sell")
    elif institution_net >= 5000:
        positives.append("institutional_lhb_net_buy")

    block_trades = list(payload.get("block_trades") or [])
    recent_discounts = [
        float(row.get("premium_pct") or 0)
        for row in block_trades[:5]
        if str(row.get("date") or "") >= (current.replace(day=1)).isoformat()
    ]
    details["recent_block_trade_premium_pct"] = recent_discounts
    if recent_discounts and min(recent_discounts) <= -8:
        warnings.append("deep_discount_block_trade")

    report_cutoff = (current - timedelta(days=180)).isoformat()
    reports = [
        row for row in (payload.get("reports") or [])
        if str(row.get("date") or "") >= report_cutoff
    ]
    latest_by_institution: dict[str, dict[str, Any]] = {}
    for row in reports:
        institution = str(row.get("institution") or row.get("title") or "")
        if institution and institution not in latest_by_institution:
            latest_by_institution[institution] = row
    consensus_reports = list(latest_by_institution.values())
    next_eps = [
        float(row.get("eps_next_year") or 0)
        for row in consensus_reports
        if float(row.get("eps_next_year") or 0) != 0
    ]
    details["report_count_180d"] = len(reports)
    details["consensus_institution_count"] = len(consensus_reports)
    details["consensus_next_eps"] = (
        round(sum(next_eps) / len(next_eps), 4) if next_eps else None
    )
    details["consensus_sample_size"] = len(next_eps)
    if next_eps and len(next_eps) < 3:
        warnings.append("thin_consensus_sample")

    return {
        "hard_risks": sorted(set(hard_risks)),
        "warnings": sorted(set(warnings)),
        "positives": sorted(set(positives)),
        "details": details,
    }


def collect(
    code: str,
    *,
    name: str = "",
    asof: date | str | None = None,
) -> dict[str, Any]:
    current = _current(asof)
    normalized = str(code)[-6:].zfill(6)
    errors: list[dict[str, str]] = []
    statuses: dict[str, dict[str, Any]] = {}

    def fetch(dataset: str, function, fallback):
        try:
            value = function()
        except Exception as exc:  # noqa: BLE001 - partial evidence is explicit
            error = {
                "dataset": dataset,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
            errors.append(error)
            statuses[dataset] = _dataset_status(
                dataset,
                fallback,
                queried_asof=current,
                error=error,
            )
            return fallback
        statuses[dataset] = _dataset_status(
            dataset,
            value,
            queried_asof=current,
        )
        return value

    payload = {
        "schema": SCHEMA,
        "code": normalized,
        "name": name or normalized,
        "asof": current.isoformat(),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lockups": fetch(
            "lockups",
            lambda: fetch_lockups(normalized, asof=current),
            {"history": [], "upcoming": []},
        ),
        "margin_trading": fetch(
            "margin_trading",
            lambda: fetch_margin_trading(normalized),
            [],
        ),
        "holder_changes": fetch(
            "holder_changes",
            lambda: fetch_holder_changes(normalized),
            [],
        ),
        "dragon_tiger": fetch(
            "dragon_tiger",
            lambda: fetch_dragon_tiger(normalized, asof=current),
            {
                "records": [],
                "seats": {"buy": [], "sell": []},
                "institution": {"net_amount_wan": 0.0},
            },
        ),
        "block_trades": fetch(
            "block_trades",
            lambda: fetch_block_trades(normalized),
            [],
        ),
        "reports": fetch("reports", lambda: fetch_reports(normalized), []),
    }
    payload["dataset_status"] = statuses
    payload["data_quality"] = _quality_from_statuses(
        statuses,
        current=current,
        errors=errors,
    )
    payload["source"] = source_metadata()
    payload["risk_summary"] = assess_risks(payload, asof=current)
    return payload


def write_cache(payload: dict[str, Any]) -> dict[str, Any]:
    code = str(payload.get("code") or "").zfill(6)
    if not code.strip("0") or payload.get("schema") != SCHEMA:
        raise ValueError("invalid stock intelligence payload")
    atomic_write_json(cache_file(code), payload)
    if (payload.get("data_quality") or {}).get("directional_ready") is True:
        atomic_write_json(last_good_file(code), payload)
    return payload


def _current_cache_view(
    record: dict[str, Any],
    *,
    current: date,
    max_age_days: int,
) -> dict[str, Any]:
    try:
        age_days = (current - date.fromisoformat(str(record.get("asof"))[:10])).days
    except (TypeError, ValueError):
        age_days = max_age_days + 1
    global_stale = age_days < 0 or age_days > max_age_days
    statuses = {
        name: dict(status)
        for name, status in (record.get("dataset_status") or {}).items()
        if isinstance(status, dict)
    }
    quality = _quality_from_statuses(
        statuses,
        current=current,
        errors=list((record.get("data_quality") or {}).get("errors") or []),
        global_stale=global_stale,
    )
    stale = global_stale or bool(quality["stale_required_datasets"])
    summary = record.get("risk_summary") or assess_risks(record, asof=current)
    warnings = list(summary.get("warnings") or [])
    hard_risks = list(summary.get("hard_risks") or [])
    warnings.extend(
        f"missing_dataset:{item}" for item in quality["missing_datasets"]
    )
    warnings.extend(
        f"stale_dataset:{item}" for item in quality["stale_datasets"]
    )
    if global_stale:
        warnings.append("stale_market_intelligence")
    if global_stale or "lockups" in quality["stale_datasets"]:
        hard_risks = []
    return {
        "available": True,
        "stale": stale,
        "directional_ready": quality["directional_ready"],
        "age_days": age_days,
        "asof": record.get("asof"),
        "fetched_at": record.get("fetched_at"),
        "missing_datasets": quality["missing_datasets"],
        "stale_datasets": quality["stale_datasets"],
        "hard_risks": sorted(set(hard_risks)),
        "warnings": sorted(set(warnings)),
        "positives": list(summary.get("positives") or []),
        "details": dict(summary.get("details") or {}),
        "source": dict(record.get("source") or {}),
        "data_quality": quality,
        "dataset_status": statuses,
        "snapshot_ref": dict(record.get("snapshot_ref") or {}),
        "fallback_used": False,
    }


def read_cache(
    code: str,
    *,
    asof: date | str | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> dict[str, Any]:
    record = read_json(cache_file(code), None)
    if not isinstance(record, dict):
        return {
            "available": False,
            "stale": None,
            "directional_ready": False,
            "missing_datasets": list(REQUIRED_DATASETS),
            "stale_datasets": [],
            "hard_risks": [],
            "warnings": [
                f"missing_dataset:{dataset}" for dataset in REQUIRED_DATASETS
            ],
            "positives": [],
        }
    current = _current(asof)
    if record.get("schema") in LEGACY_SCHEMAS:
        return {
            "available": True,
            "stale": True,
            "directional_ready": False,
            "age_days": None,
            "asof": record.get("asof"),
            "fetched_at": record.get("fetched_at"),
            "missing_datasets": list(REQUIRED_DATASETS),
            "stale_datasets": [],
            "hard_risks": [],
            "warnings": ["legacy_market_intelligence_schema"],
            "positives": [],
            "details": {},
            "source": dict(record.get("source") or {}),
            "data_quality": {
                "status": "partial",
                "directional_ready": False,
                "missing_required_datasets": list(REQUIRED_DATASETS),
                "stale_required_datasets": [],
            },
            "snapshot_ref": dict(record.get("snapshot_ref") or {}),
        }
    if record.get("schema") != SCHEMA:
        return {
            "available": False,
            "stale": None,
            "directional_ready": False,
            "missing_datasets": list(REQUIRED_DATASETS),
            "stale_datasets": [],
            "hard_risks": [],
            "warnings": ["unsupported_market_intelligence_schema"],
            "positives": [],
        }
    view = _current_cache_view(
        record,
        current=current,
        max_age_days=max_age_days,
    )
    if not view["directional_ready"] and not view["hard_risks"]:
        fallback = read_json(last_good_file(code), None)
        if (
            isinstance(fallback, dict)
            and fallback.get("schema") == SCHEMA
        ):
            fallback_view = _current_cache_view(
                fallback,
                current=current,
                max_age_days=max_age_days,
            )
            if fallback_view["directional_ready"] and not fallback_view["stale"]:
                fallback_view["fallback_used"] = True
                fallback_view["fallback_from"] = {
                    "asof": record.get("asof"),
                    "fetched_at": record.get("fetched_at"),
                    "data_quality": dict(record.get("data_quality") or {}),
                }
                fallback_view["warnings"] = sorted(set(
                    list(fallback_view["warnings"])
                    + ["using_last_known_good_market_intelligence"]
                ))
                return fallback_view
    return view


def main() -> int:
    parser = argparse.ArgumentParser(description="读取或采集筹码/机构证据")
    sub = parser.add_subparsers(dest="command", required=True)
    read_parser = sub.add_parser("read")
    read_parser.add_argument("--code", required=True)
    read_parser.add_argument("--asof")
    read_parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    read_parser.add_argument("--json", action="store_true")
    collect_parser = sub.add_parser("collect")
    collect_parser.add_argument("--code", required=True)
    collect_parser.add_argument("--name", default="")
    collect_parser.add_argument("--asof")
    collect_parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.command == "read":
        result = read_cache(
            args.code,
            asof=args.asof,
            max_age_days=args.max_age_days,
        )
    else:
        result = collect(
            args.code,
            name=args.name,
            asof=args.asof,
        )
        write_cache(result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
