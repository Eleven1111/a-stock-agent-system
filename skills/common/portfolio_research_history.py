"""Immutable daily inputs for portfolio-level strategy validation."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from paths import data_file
from research_artifact import json_sha256
from state_store import atomic_write_json, read_json


SNAPSHOT_SCHEMA = "portfolio_research_snapshot_v1"
INPUT_SCHEMA = "portfolio_backtest_input_v1"
POSITIVE_ACTIONS = {"buy", "add", "conditional_buy"}
RESEARCH_ONLY_REASONS = {"strategy_unverified"}


def _code(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text[2:] if text.startswith(("sh", "sz")) else text.zfill(6)


def _snapshot_dir() -> str:
    return data_file("stock-triage", "portfolio_research_snapshots")


def snapshot_file(asof: str) -> str:
    return os.path.join(_snapshot_dir(), f"{asof}.json")


def _parse_datetime(value: Any) -> datetime:
    return datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))


def _research_decision(item: Mapping[str, Any]) -> str:
    live_decision = str(item.get("decision") or "watch").lower()
    policy = item.get("policy_decision") or {}
    requested = str(policy.get("requested_action") or live_decision).lower()
    reasons = {str(reason) for reason in policy.get("reasons") or []}
    if (
        live_decision == "watch"
        and requested in POSITIVE_ACTIONS
        and reasons
        and reasons.issubset(RESEARCH_ONLY_REASONS)
    ):
        return requested
    return live_decision


def _candidate(item: Mapping[str, Any], generated_at: str) -> dict[str, Any]:
    decision = _research_decision(item)
    quality = item.get("quality_report") or {}
    components = {
        name: float(value)
        for name, value in {
            "open_daban": item.get("open_daban_score"),
            "open_trend": item.get("open_trend_score"),
            "auction": item.get("auction_score"),
            "social_attention": item.get("social_attention_bonus"),
        }.items()
        if value is not None
    }
    return {
        "code": _code(item.get("code")),
        "name": str(item.get("name") or _code(item.get("code"))),
        "strategy_id": str(item.get("strategy_id") or "default"),
        "lane": (
            "daban"
            if str(item.get("strategy_id") or "").startswith("daban")
            else "trend"
        ),
        "score": float(item.get("open_score") or 0.0),
        "components": components,
        "decision": decision,
        "live_decision": str(item.get("decision") or "watch").lower(),
        "eligible": decision in POSITIVE_ACTIONS and quality.get("status") == "passed",
        "quality_status": quality.get("status"),
        "evidence_asof": generated_at,
        "policy_reasons": list((item.get("policy_decision") or {}).get("reasons") or []),
        "research_evidence": dict(item.get("research_evidence") or {}),
    }


def record_open_confirmation(result: Mapping[str, Any]) -> dict[str, Any]:
    """Persist one live day's final candidate surface exactly once."""
    asof = str(result.get("asof") or "")
    generated_at = str(result.get("generated_at") or "")
    if not asof or not generated_at:
        raise ValueError("open confirmation requires asof and generated_at")
    try:
        generated_date = _parse_datetime(generated_at).date().isoformat()
    except ValueError as exc:
        raise ValueError("open confirmation generated_at is invalid") from exc
    shanghai_today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    if generated_date != asof or asof != shanghai_today:
        return {
            "status": "skipped_non_live_date",
            "reason": "historical reruns are not point-in-time evidence",
            "asof": asof,
        }

    input_ref = dict(result.get("input_snapshot") or {})
    source_versions = dict(input_ref.get("source_versions") or {})
    if not source_versions:
        raise ValueError("open confirmation research snapshot requires source_versions")
    body = {
        "schema": SNAPSHOT_SCHEMA,
        "date": asof,
        "generated_at": generated_at,
        "source_versions": source_versions,
        "source_snapshot": {
            key: input_ref.get(key)
            for key in ("snapshot_id", "payload_hash", "snapshot_path")
            if input_ref.get(key) is not None
        },
        "candidates": [
            _candidate(item, generated_at)
            for item in result.get("signals") or []
        ],
    }
    body["snapshot_sha256"] = json_sha256(body)
    path = snapshot_file(asof)
    existing = read_json(path, None)
    if isinstance(existing, dict):
        if existing.get("snapshot_sha256") == body["snapshot_sha256"]:
            return {"status": "reused", "path": path, "snapshot": existing}
        return {
            "status": "conflict_preserved",
            "reason": "daily research snapshot already recorded; first observation retained",
            "path": path,
            "snapshot": existing,
            "attempted_snapshot_sha256": body["snapshot_sha256"],
        }
    atomic_write_json(path, body)
    return {"status": "recorded", "path": path, "snapshot": body}


def load_snapshots(start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
    directory = _snapshot_dir()
    if not os.path.isdir(directory):
        return []
    output = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        item = read_json(os.path.join(directory, name), None)
        if not isinstance(item, dict) or item.get("schema") != SNAPSHOT_SCHEMA:
            continue
        asof = str(item.get("date") or "")
        if (start and asof < start) or (end and asof > end):
            continue
        digest = item.get("snapshot_sha256")
        body = {key: value for key, value in item.items() if key != "snapshot_sha256"}
        if digest != json_sha256(body):
            raise ValueError(f"portfolio research snapshot hash mismatch: {name}")
        output.append(item)
    return output


def build_portfolio_input(
    snapshots: Sequence[Mapping[str, Any]],
    market_data: Mapping[str, Any],
    *,
    rules_locked_at: str,
    strategy_id: str = "open_confirmation_combined_v1",
    policy: Mapping[str, Any] | None = None,
    weights: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Join point-in-time candidates with later-observed OHLCV outcome data."""
    rows = [dict(item) for item in snapshots]
    if not rows:
        raise ValueError("at least one portfolio research snapshot is required")
    for item in rows:
        if item.get("schema") != SNAPSHOT_SCHEMA:
            raise ValueError("unsupported portfolio research snapshot schema")
    bars_by_code = market_data.get("bars_by_code")
    benchmark_bars = market_data.get("benchmark_bars")
    if not isinstance(bars_by_code, dict) or not isinstance(benchmark_bars, list):
        raise ValueError("market data requires bars_by_code and benchmark_bars")
    return {
        "schema": INPUT_SCHEMA,
        "strategy_id": strategy_id,
        "rules_locked_at": rules_locked_at,
        "weights": dict(weights or {}),
        "policy": dict(policy or {}),
        "snapshots": [
            {
                "date": item["date"],
                "generated_at": item["generated_at"],
                "source_versions": dict(item.get("source_versions") or {}),
                "snapshot_sha256": item.get("snapshot_sha256"),
                "candidates": list(item.get("candidates") or []),
            }
            for item in rows
        ],
        "bars_by_code": dict(bars_by_code),
        "benchmark_bars": list(benchmark_bars),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="组装组合级回测输入")
    parser.add_argument("--market-data", required=True, help="后验 OHLCV JSON")
    parser.add_argument("--rules-locked-at", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--strategy-id", default="open_confirmation_combined_v1")
    args = parser.parse_args()
    with open(args.market_data, encoding="utf-8") as handle:
        market_data = json.load(handle)
    payload = build_portfolio_input(
        load_snapshots(args.start, args.end),
        market_data,
        rules_locked_at=args.rules_locked_at,
        strategy_id=args.strategy_id,
    )
    atomic_write_json(os.path.abspath(os.path.expanduser(args.output)), payload)
    print(json.dumps({
        "status": "ok",
        "output": os.path.abspath(os.path.expanduser(args.output)),
        "snapshot_count": len(payload["snapshots"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
