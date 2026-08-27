#!/usr/bin/env python3
"""Freeze the bounded post-close evidence cohort for all six strategies."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = str(Path(__file__).resolve().parents[1])
try:
    from ._repo_bootstrap import ensure_repo_importable  # type: ignore[import-not-found]
except ImportError:
    from _repo_bootstrap import ensure_repo_importable  # type: ignore[no-redef]

ensure_repo_importable(ROOT)
import skills.common  # noqa: F401,E402 -- owns the canonical flat common-module path

import local_market_history  # noqa: E402
import market_adapters  # noqa: E402
import preleader_pretable_store  # noqa: E402
import sentiment_daily  # noqa: E402
import strategy_evidence  # noqa: E402
from paths import data_file  # noqa: E402
from research_artifact import json_sha256  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402


SHANGHAI = ZoneInfo("Asia/Shanghai")
TRACKING_DAYS = 60


def _today() -> str:
    return datetime.now(SHANGHAI).date().isoformat()


def _candidate_path() -> str:
    return data_file("stock-triage", "candidate_pool_latest.json")


def _auction_path() -> str:
    return data_file("daban-stock-picker", "auction_shortlist_latest.json")


def _selection_path() -> str:
    return data_file("stock-triage", "hot_money_selection_latest.json")


def _output_path(asof: str) -> str:
    return data_file("stock-triage", os.path.join("strategy_evidence", f"{asof}.json"))


def _load_required(path: str) -> dict[str, Any]:
    payload = read_json(path, None)
    if not isinstance(payload, dict):
        raise ValueError(f"required artifact unavailable: {path}")
    return payload


def _rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("candidates") or payload.get("events") or []
    if not isinstance(value, list):
        raise ValueError("candidate artifact requires candidates/events list")
    return [dict(row) for row in value if isinstance(row, Mapping)]


def _frame_rows(value: Any) -> list[dict[str, Any]]:
    if hasattr(value, "to_dict"):
        records = value.to_dict("records")
    elif isinstance(value, list):
        records = value
    else:
        records = []
    return [dict(row) for row in records if isinstance(row, Mapping)]


def _previous_artifact(asof: str) -> dict[str, Any]:
    directory = Path(_output_path(asof)).parent
    choices = sorted(path for path in directory.glob("*.json") if path.stem < asof)
    return read_json(str(choices[-1]), {}) if choices else {}


def _active_tracking(
    asof: str, previous: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]
) -> dict[str, dict[str, str]]:
    cutoff = date.fromisoformat(asof) - timedelta(days=TRACKING_DAYS)
    tracking: dict[str, dict[str, str]] = {}
    for code, raw in dict(previous.get("tracked_leaders") or {}).items():
        if not isinstance(raw, Mapping):
            continue
        last_seen = str(raw.get("last_seen") or "")
        try:
            keep = date.fromisoformat(last_seen) >= cutoff
        except ValueError:
            keep = False
        if keep:
            tracking[strategy_evidence.naked_code(code)] = {
                "first_seen": str(raw.get("first_seen") or last_seen),
                "last_seen": last_seen,
            }
    heights = [float(row.get("board_height") or 0) for row in candidates]
    highest = max(heights, default=0.0)
    for row in candidates:
        code = strategy_evidence.naked_code(row.get("code") or row.get("market_code"))
        if not code or highest <= 0 or float(row.get("board_height") or 0) != highest:
            continue
        current = tracking.get(code) or {"first_seen": asof}
        tracking[code] = {"first_seen": current["first_seen"], "last_seen": asof}
    return dict(sorted(tracking.items()))


def _market(code: str) -> str:
    if code.startswith("6"):
        return "sh"
    if code.startswith(("4", "8", "9")):
        return "bj"
    return "sz"


def _fetch_minutes(
    codes: Sequence[str], fetcher: Callable[..., Sequence[Mapping[str, Any]]],
    *, delay_seconds: float,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    rows: dict[str, list[dict[str, Any]]] = {}
    missing: list[str] = []
    for code in codes:
        try:
            value = fetcher(code, market=_market(code))
        except (OSError, RuntimeError, TypeError, ValueError):
            value = []
        normalized = [dict(row) for row in value if isinstance(row, Mapping)]
        if normalized:
            rows[code] = normalized
        else:
            missing.append(code)
        if delay_seconds > 0:
            time.sleep(delay_seconds)
    return rows, missing


def _prepare_inputs(
    requested_asof: str,
    candidate_file: str,
    max_codes: int,
    limitup_fetcher: Callable[[str], Any],
    bar_loader: Callable[[Sequence[str], str, int], list[dict[str, Any]]],
) -> dict[str, Any]:
    candidate_payload = _load_required(candidate_file)
    source_asof = str(candidate_payload.get("asof") or candidate_payload.get("date") or "")[:10]
    if source_asof != requested_asof:
        raise ValueError(f"candidate asof mismatch: expected {requested_asof}, got {source_asof}")
    candidates = _rows(candidate_payload)
    auction = read_json(_auction_path(), {})
    selection = read_json(_selection_path(), {})
    limitup_rows = _frame_rows(limitup_fetcher(requested_asof.replace("-", "")))
    if not limitup_rows and any(row.get("first_seal") for row in candidates):
        raise ValueError("official limitup pool unavailable while candidate events exist")
    previous = _previous_artifact(requested_asof)
    tracking = _active_tracking(requested_asof, previous, candidates)
    preliminary = strategy_evidence.select_cohort(
        candidates, auction, limitup_rows, tracked_codes=tracking, max_codes=max_codes
    )
    history_codes = sorted({
        *preliminary,
        *(strategy_evidence.naked_code(row.get("code") or row.get("market_code"))
          for row in candidates),
    } - {""})
    bars = bar_loader(history_codes, requested_asof, 80) if history_codes else []
    s1_targets = strategy_evidence.rank_surprise_targets(
        requested_asof, candidates, auction, bars
    )
    cohort = strategy_evidence.select_cohort(
        candidates, auction, limitup_rows, tracked_codes=tracking,
        extra_codes=s1_targets, max_codes=max_codes,
    )
    return {
        "candidate_payload": candidate_payload, "candidates": candidates,
        "auction": auction, "selection": selection, "limitup_rows": limitup_rows,
        "previous": previous, "tracking": tracking, "bars": bars,
        "s1_targets": s1_targets, "cohort": cohort,
    }


def _materialize(
    requested_asof: str,
    candidate_file: str,
    prepared: Mapping[str, Any],
    *,
    max_codes: int,
    minute_delay_seconds: float,
    minute_fetcher: Callable[..., Sequence[Mapping[str, Any]]],
    sentiment_loader: Callable[[], list[dict[str, Any]]],
) -> dict[str, Any]:
    cohort = list(prepared["cohort"])
    minutes, missing_minutes = _fetch_minutes(
        cohort, minute_fetcher, delay_seconds=minute_delay_seconds
    )
    previous = dict(prepared["previous"])
    prior_records = {
        strategy_evidence.naked_code(row.get("code")): dict(row)
        for row in previous.get("records") or []
        if isinstance(row, Mapping) and strategy_evidence.naked_code(row.get("code"))
    }
    pretable, pretable_status = preleader_pretable_store.load_previous_pretable(requested_asof)
    result = strategy_evidence.build_evidence(
        requested_asof,
        candidates=prepared["candidates"], auction=prepared["auction"],
        selection=prepared["selection"], limitup_rows=prepared["limitup_rows"],
        minute_rows=minutes, daily_bars=prepared["bars"],
        sentiment_series=sentiment_loader(), tracked_codes=prepared["tracking"],
        extra_codes=prepared["s1_targets"], previous_records=prior_records,
        preleader_pretable=pretable, preleader_pretable_status=pretable_status,
        max_codes=max_codes,
    )
    result.update({
        "generated_at": datetime.now(SHANGHAI).isoformat(),
        "candidate_path": os.path.abspath(os.path.expanduser(candidate_file)),
        "source_sha256": json_sha256({
            "candidate": prepared["candidate_payload"], "auction": prepared["auction"],
            "selection": prepared["selection"], "limitup_rows": prepared["limitup_rows"],
        }),
        "minute_requested_count": len(cohort), "minute_covered_count": len(minutes),
        "minute_missing_codes": missing_minutes,
        "rank_surprise_prefilter_codes": prepared["s1_targets"],
        "tracked_leaders": prepared["tracking"],
    })
    result["result_sha256"] = json_sha256(result)
    atomic_write_json(_output_path(requested_asof), result)
    return result


def run(
    *,
    asof: str | None = None,
    candidate_path: str | None = None,
    max_codes: int = strategy_evidence.DEFAULT_MAX_CODES,
    minute_delay_seconds: float = 0.10,
    limitup_fetcher: Callable[[str], Any] = market_adapters.fetch_hot_money_limitup_pool,
    minute_fetcher: Callable[..., Sequence[Mapping[str, Any]]] = market_adapters.fetch_tencent_minute,
    bar_loader: Callable[[Sequence[str], str, int], list[dict[str, Any]]] = local_market_history.get_daily_bars,
    sentiment_loader: Callable[[], list[dict[str, Any]]] = sentiment_daily.load_summary,
) -> dict[str, Any]:
    requested_asof = asof or _today()
    path = _output_path(requested_asof)
    existing = read_json(path, None)
    if isinstance(existing, Mapping):
        return dict(existing)
    candidate_file = candidate_path or _candidate_path()
    prepared = _prepare_inputs(
        requested_asof, candidate_file, max_codes, limitup_fetcher, bar_loader
    )
    return _materialize(
        requested_asof, candidate_file, prepared, max_codes=max_codes,
        minute_delay_seconds=minute_delay_seconds, minute_fetcher=minute_fetcher,
        sentiment_loader=sentiment_loader,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="六策略统一证据数据集（收盘后、有界、NON-LIVE）")
    parser.add_argument("--asof", default=None)
    parser.add_argument("--candidate", default=None)
    parser.add_argument("--max-codes", type=int, default=strategy_evidence.DEFAULT_MAX_CODES)
    parser.add_argument("--minute-delay-seconds", type=float, default=0.10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    output = run(
        asof=args.asof,
        candidate_path=args.candidate,
        max_codes=args.max_codes,
        minute_delay_seconds=args.minute_delay_seconds,
    )
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(
            f"strategy-evidence-daily {output['asof']}: "
            f"{output['cohort_count']} records, minute {output['minute_covered_count']}/"
            f"{output['minute_requested_count']}"
        )


if __name__ == "__main__":
    main()
