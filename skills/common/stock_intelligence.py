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


SCHEMA = "stock_intelligence_v1"
DEFAULT_MAX_AGE_DAYS = 7


def cache_file(code: str) -> str:
    return os.path.join(
        cache_dir("stock-triage"),
        "market_intelligence",
        f"{str(code).zfill(6)}.json",
    )


def _current(value: date | str | None) -> date:
    return date.fromisoformat(str(value or date.today())[:10])


def _pct_change(latest: float, baseline: float) -> float | None:
    if not baseline:
        return None
    return (latest / baseline - 1) * 100


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
    warnings.extend(f"missing_dataset:{item}" for item in missing_datasets)

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

    def fetch(dataset: str, function, fallback):
        try:
            return function()
        except Exception as exc:  # noqa: BLE001 - partial evidence is explicit
            errors.append({
                "dataset": dataset,
                "error": str(exc),
                "error_type": type(exc).__name__,
            })
            return fallback

    payload = {
        "schema": SCHEMA,
        "code": normalized,
        "name": name or normalized,
        "asof": current.isoformat(),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source_metadata(),
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
    payload["data_quality"] = {
        "status": "partial" if errors else "complete",
        "missing_datasets": sorted(error["dataset"] for error in errors),
        "errors": errors,
    }
    payload["risk_summary"] = assess_risks(payload, asof=current)
    return payload


def write_cache(payload: dict[str, Any]) -> dict[str, Any]:
    code = str(payload.get("code") or "").zfill(6)
    if not code.strip("0") or payload.get("schema") != SCHEMA:
        raise ValueError("invalid stock intelligence payload")
    atomic_write_json(cache_file(code), payload)
    return payload


def read_cache(
    code: str,
    *,
    asof: date | str | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> dict[str, Any]:
    record = read_json(cache_file(code), None)
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        return {
            "available": False,
            "stale": None,
            "hard_risks": [],
            "warnings": [],
            "positives": [],
        }
    current = _current(asof)
    try:
        age_days = (current - date.fromisoformat(str(record.get("asof"))[:10])).days
    except (TypeError, ValueError):
        age_days = max_age_days + 1
    stale = age_days < 0 or age_days > max_age_days
    summary = record.get("risk_summary") or assess_risks(record, asof=current)
    warnings = list(summary.get("warnings") or [])
    hard_risks = list(summary.get("hard_risks") or [])
    if stale:
        warnings.append("stale_market_intelligence")
        hard_risks = []
    return {
        "available": True,
        "stale": stale,
        "age_days": age_days,
        "asof": record.get("asof"),
        "fetched_at": record.get("fetched_at"),
        "hard_risks": sorted(set(hard_risks)),
        "warnings": sorted(set(warnings)),
        "positives": list(summary.get("positives") or []),
        "details": dict(summary.get("details") or {}),
        "source": dict(record.get("source") or {}),
        "data_quality": dict(record.get("data_quality") or {}),
        "snapshot_ref": dict(record.get("snapshot_ref") or {}),
    }


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
