#!/usr/bin/env python3
"""Run the six research strategies in an isolated, non-live shadow lane."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "skills" / "common"
if str(COMMON) not in sys.path:
    sys.path.insert(0, str(COMMON))

import assist_arbitrage  # noqa: E402
import divergence_reseal  # noqa: E402
import ice_point_reversal  # noqa: E402
import preleader_arbitrage  # noqa: E402
import rank_surprise  # noqa: E402
import reverse_volume  # noqa: E402
from paths import data_file  # noqa: E402
from research_artifact import json_sha256  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402

STRATEGY_IDS = (
    "rank_surprise", "divergence_reseal", "assist_arbitrage",
    "preleader_arbitrage", "reverse_volume", "ice_point_reversal",
)


def _today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _input_path() -> str:
    return data_file("stock-triage", "candidate_pool_latest.json")


def _output_path(asof: str) -> str:
    return data_file("stock-triage", os.path.join("strategy_shadow", f"{asof}.json"))


def _load(path: str) -> Any:
    with open(os.path.abspath(os.path.expanduser(path)), encoding="utf-8") as handle:
        return json.load(handle)


def _records(payload: Any, asof: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, Mapping):
        rows = payload.get("events") or payload.get("candidates") or []
    else:
        rows = []
    if not isinstance(rows, list):
        raise ValueError("strategy shadow input requires an events/candidates list")
    output = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        row.setdefault("date", asof)
        row.setdefault("code", row.get("market_code") or row.get("symbol"))
        row.setdefault("sector", row.get("industry") or row.get("theme"))
        row.setdefault("attribute", row.get("sector") or row.get("industry"))
        output.append(row)
    return output


def _naked_code(value: Any) -> str:
    """候选池与竞价产物必须用同一把尺子归一化代码。

    只归一化一侧会让合并静默落空，而 sidecar 仍被记为「已使用」——artifact
    于是声称用了它其实没用上的证据，比干脆缺证据更难发现。
    """
    code = str(value or "").strip().lower()
    return code.removeprefix("sh").removeprefix("sz").zfill(6) if code else ""


def _merge_auction_evidence(payload: Any, asof: str) -> tuple[Any, list[str]]:
    """Merge same-day auction/market artifacts without inventing evidence."""
    if not isinstance(payload, Mapping):
        return payload, []
    merged = dict(payload)
    base_rows = _records(payload, asof)
    by_code = {_naked_code(row.get("code") or row.get("market_code")): row for row in base_rows}
    used = []
    auction_path = data_file("daban-stock-picker", "auction_shortlist_latest.json")
    auction = read_json(auction_path, None)
    if isinstance(auction, Mapping) and str(auction.get("asof") or "")[:10] == asof:
        used.append(auction_path)
        auction_rows = []
        for key in ("factors", "shortlist", "research_candidates", "rejected"):
            value = auction.get(key)
            if isinstance(value, list):
                auction_rows.extend(value)
        for raw in auction_rows:
            if not isinstance(raw, Mapping):
                continue
            code = _naked_code(raw.get("code") or raw.get("market_code"))
            if code and code in by_code:
                by_code[code].update(dict(raw))
    selection_path = data_file("stock-triage", "hot_money_selection_latest.json")
    selection = read_json(selection_path, None)
    if isinstance(selection, Mapping) and str(selection.get("asof") or "")[:10] == asof:
        used.append(selection_path)
        if not merged.get("market_state"):
            merged["market_state"] = selection.get("market_state")
    merged["candidates"] = list(by_code.values())
    return merged, used


def _unavailable(strategy_id: str, reason: str) -> dict[str, Any]:
    return {"strategy_id": strategy_id, "status": "unavailable", "reasons": [reason],
            "results": [], "research_only": True, "execution_eligible": False}


def _run_one(strategy_id: str, records: list[dict[str, Any]], market_state: Any) -> dict[str, Any]:
    if not records:
        return _unavailable(strategy_id, "input_records_empty")
    runners: dict[str, Callable[[], list[dict[str, Any]]]] = {
        "rank_surprise": lambda: rank_surprise.evaluate_universe(records, market_state=market_state),
        "divergence_reseal": lambda: divergence_reseal.evaluate_universe(records),
        "assist_arbitrage": lambda: assist_arbitrage.evaluate_universe(records),
        "preleader_arbitrage": lambda: preleader_arbitrage.evaluate_universe(records, pretable={}),
        "reverse_volume": lambda: reverse_volume.evaluate_universe(records, market_state=market_state),
        "ice_point_reversal": lambda: ice_point_reversal.evaluate_universe(records, market_state=market_state),
    }
    modules = {
        "rank_surprise": rank_surprise, "divergence_reseal": divergence_reseal,
        "assist_arbitrage": assist_arbitrage, "preleader_arbitrage": preleader_arbitrage,
        "reverse_volume": reverse_volume, "ice_point_reversal": ice_point_reversal,
    }
    try:
        results = runners[strategy_id]()
    except Exception as exc:  # noqa: BLE001 - preserve the failure in the artifact
        return _unavailable(strategy_id, f"runner_error:{type(exc).__name__}:{exc}")
    module = modules[strategy_id]
    summary = module.summarize(results)
    signal_codes = [{"code": code, "date": date}
                    for code, date in sorted(module.signal_codes(results))]
    counts = summary.get("status_counts") or {}
    status = (
        "unavailable" if counts.get("unavailable", 0) == len(results) and results
        else "signal" if counts.get("signal", 0) else "no_signal"
    )
    return {"strategy_id": strategy_id, "status": status, "summary": summary,
            "signal_codes": signal_codes, "results": results,
            "research_only": True, "execution_eligible": False}


def run(input_path: str, *, asof: str | None = None) -> dict[str, Any]:
    requested_asof = asof or _today()
    payload = _load(input_path)
    source_asof = payload.get("asof") or payload.get("date") if isinstance(payload, Mapping) else None
    if not source_asof:
        raise ValueError("input asof is required")
    if str(source_asof)[:10] != requested_asof:
        raise ValueError(f"input asof mismatch: expected {requested_asof}, got {source_asof}")
    payload, sidecars = _merge_auction_evidence(payload, requested_asof)
    records = _records(payload, requested_asof)
    market_state = payload.get("market_state") if isinstance(payload, Mapping) else None
    source_hash = json_sha256(payload)
    path = _output_path(requested_asof)
    existing = read_json(path, None)
    if isinstance(existing, Mapping) and existing.get("input_sha256") == source_hash:
        return dict(existing)
    if isinstance(existing, Mapping):
        raise ValueError("strategy shadow output already exists with a different input")
    result = {
        "schema": "strategy_shadow_daily_v1", "asof": requested_asof,
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "input_path": os.path.abspath(os.path.expanduser(input_path)),
        "input_sha256": source_hash, "record_count": len(records),
        "evidence_sidecars": sidecars,
        "research_only": True, "execution_eligible": False, "live_order_sent": False,
        "strategies": {sid: _run_one(sid, records, market_state) for sid in STRATEGY_IDS},
    }
    result["result_sha256"] = json_sha256(result)
    atomic_write_json(path, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="每日六策略 shadow 评估器")
    parser.add_argument("--input", default=_input_path())
    parser.add_argument("--asof", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    output = run(args.input, asof=args.asof)
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(f"strategy-shadow-daily {output['asof']}: {output['record_count']} records")


if __name__ == "__main__":
    main()
