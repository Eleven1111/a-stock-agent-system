#!/usr/bin/env python3
"""Cron-safe, fail-closed promotion through the eligible state only."""
from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
try:
    from ._repo_bootstrap import ensure_repo_importable  # type: ignore[import-not-found]
except ImportError:
    from _repo_bootstrap import ensure_repo_importable  # type: ignore[no-redef]

ensure_repo_importable(str(ROOT))
import skills.common  # noqa: F401,E402 -- owns the canonical flat common-module path

import strategy_registry  # noqa: E402
from paths import data_file  # noqa: E402

AUTO_TARGETS = {"research_only": "shadow", "shadow": "eligible_for_manual_pilot"}
PAPER_AUTO_TARGETS = {**AUTO_TARGETS, "eligible_for_manual_pilot": "manual_pilot"}
MACHINE_ACTOR = "cron:strategy-promotion-auto"
PAPER_ENABLE_ENV = "A_STOCK_PAPER_PROMOTION"


def _paper_enabled(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "paper_only", False)) or os.environ.get(PAPER_ENABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _load(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        with open(os.path.abspath(os.path.expanduser(path)), encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _promotion_data_path(*parts: str) -> str:
    return os.path.join(data_file("stock-triage", "strategy_promotion"), *parts)


def _default_precommit() -> str:
    return _promotion_data_path("precommit.json")


def _default_empirical_dir() -> str:
    return _promotion_data_path("empirical_gates")


def _default_shadow_dir() -> str:
    return _promotion_data_path("shadow_records")


def _audit(strategy_id: str, run_id: str) -> dict[str, str]:
    return {
        "schema": "strategy_promotion_machine_audit_v1",
        "strategy_id": strategy_id,
        "actor": MACHINE_ACTOR,
        "reason": "cron_auto_promotion_evidence_gates_only",
        "signature": f"run_id:{run_id}",
        "run_id": run_id,
        "signed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _outcome(strategy_id: str, state: str, outcome: str, reason: str, audit: Mapping[str, Any], *, target: str | None = None) -> dict[str, Any]:
    return {"strategy_id": strategy_id, "from": state, "target": target if target is not None else AUTO_TARGETS.get(state),
            "outcome": outcome, "reason": reason, "audit": dict(audit)}


def _resolve(value: Any, fallback: str | None, strategy_id: str) -> str | None:
    path = str(value or fallback or "").strip()
    if not path:
        return None
    expanded = os.path.expanduser(path)
    if os.path.isdir(expanded):
        return os.path.join(expanded, f"{strategy_id}.json")
    return path


def run(args: argparse.Namespace) -> dict[str, Any]:
    run_id = os.environ.get("HERMES_RUN_ID") or os.environ.get("A_STOCK_RUN_ID") or f"local-{uuid.uuid4().hex}"
    registry_file = args.registry or strategy_registry._default_file()
    registry = strategy_registry.all_strategies(registry_file)
    ids = [args.strategy_id] if args.strategy_id else sorted(registry)
    paper_only = _paper_enabled(args)
    targets = PAPER_AUTO_TARGETS if paper_only else AUTO_TARGETS
    results: list[dict[str, Any]] = []
    for strategy_id in ids:
        rec = registry.get(strategy_id)
        audit = _audit(strategy_id, run_id)
        if not isinstance(rec, dict):
            result = _outcome(strategy_id, "research_only", "skipped", "strategy_not_registered", audit)
        else:
            promotion = rec.get("promotion") if isinstance(rec.get("promotion"), dict) else {}
            state = str(promotion.get("state") or "research_only")
            target = targets.get(state)
            if target is None:
                # This includes eligible_for_manual_pilot, manual_pilot and live.
                result = _outcome(strategy_id, state, "skipped", "automatic_target_not_allowed", audit)
            elif state == "research_only":
                precommit_path = _resolve(promotion.get("precommit_path"), args.precommit, strategy_id)
                precommit = _load(precommit_path)
                # start_shadow's real gate is _validated_precommit (OOS
                # precommit + thresholds).  In particular it does not require
                # allowed_in_live_agent: that bit is the later live-admission
                # gate, and requiring it here would make research_only records
                # unable to enter shadow for no API-backed reason.
                if precommit is None:
                    result = _outcome(strategy_id, state, "skipped", "precommit_evidence_missing", audit)
                elif args.dry_run:
                    result = _outcome(strategy_id, state, "would_promote", "promotion_gate_ready", audit, target=target)
                else:
                    try:
                        strategy_registry.start_shadow(strategy_id, precommit=precommit, thresholds_path=args.thresholds,
                                                        promotion_audit=audit, registry_file=registry_file)
                        result = _outcome(strategy_id, state, "promoted", "promotion_gate_passed", audit, target=target)
                    except (OSError, ValueError, KeyError) as exc:
                        result = _outcome(strategy_id, state, "rejected", str(exc), audit)
            else:
                empirical = _load(_resolve(promotion.get("empirical_gate_path"), args.empirical_gate, strategy_id))
                shadow = _load(_resolve(promotion.get("shadow_record_path"), args.shadow_record, strategy_id))
                if empirical is None or shadow is None:
                    result = _outcome(strategy_id, state, "skipped", "promotion_evidence_missing", audit)
                elif args.dry_run:
                    result = _outcome(strategy_id, state, "would_promote", "promotion_gate_ready", audit, target=target)
                else:
                    try:
                        strategy_registry.promote_strategy(
                            strategy_id, target, empirical_gate=empirical,
                            shadow_record=shadow, promotion_audit=audit,
                            requested_weight=(args.paper_weight if paper_only and target == "manual_pilot" else None),
                            paper_only=paper_only and target == "manual_pilot",
                            registry_file=registry_file,
                        )
                        result = _outcome(strategy_id, state, "promoted", "promotion_gate_passed", audit, target=target)
                    except (OSError, ValueError, KeyError) as exc:
                        result = _outcome(strategy_id, state, "rejected", str(exc), audit)
        if not args.dry_run and result["outcome"] in {"skipped", "rejected"} and isinstance(rec, dict):
            try:
                strategy_registry.record_promotion_outcome(strategy_id, outcome=result["outcome"], reason=result["reason"],
                                                           promotion_audit=audit, registry_file=registry_file)
            except (OSError, ValueError, KeyError) as exc:
                result["audit_error"] = str(exc)
        results.append(result)
    return {"schema": "strategy_promotion_run_v1", "run_id": run_id, "actor": MACHINE_ACTOR,
            "dry_run": bool(args.dry_run), "mode": "paper_only" if paper_only else "real_guarded",
            "allowed_targets": sorted(set(targets.values())), "results": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry")
    parser.add_argument("--strategy-id")
    parser.add_argument("--precommit", default=_default_precommit())
    parser.add_argument("--thresholds", default=str(ROOT / "config" / "validation_thresholds.json"))
    parser.add_argument("--empirical-gate", default=_default_empirical_dir())
    parser.add_argument("--shadow-record", default=_default_shadow_dir())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--paper-only", action="store_true", help="仅允许晋级到 paper manual_pilot，不开启真实 runtime")
    parser.add_argument("--paper-weight", type=float, default=0.1)
    parser.add_argument("--json", action="store_true", help="输出 JSON（默认格式）")
    args = parser.parse_args(argv)
    print(json.dumps(run(args), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
