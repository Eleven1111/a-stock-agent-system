#!/usr/bin/env python3
"""Build the four-dimension Bayesian shadow report from local artifacts only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import date

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
import skills.common  # noqa: F401,E402 -- installs skills/common on sys.path

import four_dim_score_log as observations  # noqa: E402
import four_dim_weight_research as research  # noqa: E402
from config_registry import config_path  # noqa: E402
from paths import data_file  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402


def _current_weights() -> dict[str, dict[str, float]]:
    with open(config_path("scoring"), encoding="utf-8") as handle:
        configured = (yaml.safe_load(handle) or {}).get("scoring", {}).get("weights", {})
    return {lane: dict(configured.get(lane) or {}) for lane in ("trend", "daban")}


def _shadow_settings() -> dict[str, float | int]:
    with open(config_path("scoring"), encoding="utf-8") as handle:
        configured = (yaml.safe_load(handle) or {}).get("scoring", {}).get("weight_shadow", {})
    method = configured.get("method", "dirichlet_simplex_student_t_importance_shadow_v1")
    live_effect = configured.get("live_effect", "none")
    automatic = configured.get("automatic_promotion", False)
    if method != "dirichlet_simplex_student_t_importance_shadow_v1" or live_effect != "none" or automatic is not False:
        raise ValueError("unsafe or unsupported four-dimension shadow configuration")
    settings = {
        "minimum_fit_trading_days": int(configured.get("minimum_fit_trading_days", research.MIN_FIT_DAYS)),
        "minimum_unseen_oos_trading_days": int(
            configured.get("minimum_unseen_oos_trading_days", research.MIN_OOS_DAYS)
        ),
        "assumed_notional": float(configured.get("assumed_notional", research.ASSUMED_NOTIONAL)),
    }
    if (
        settings["minimum_fit_trading_days"] < research.MIN_FIT_DAYS
        or settings["minimum_unseen_oos_trading_days"] < research.MIN_OOS_DAYS
        or settings["assumed_notional"] <= 0
    ):
        raise ValueError("four-dimension shadow gates may not be weaker than the 60/60 research contract")
    return settings


def _config_sha256() -> str:
    with open(config_path("scoring"), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _frozen_lanes(asof: str) -> tuple[str, dict, dict]:
    freeze_path = data_file("stock-triage", "four_dim_weight_shadow_freeze.json")
    frozen = read_json(freeze_path, {})
    existing_lanes = (frozen.get("lanes") or {}) if isinstance(frozen, dict) else {}
    eligible_frozen_lanes = {
        lane: payload for lane, payload in existing_lanes.items()
        if str((payload or {}).get("fit_cutoff") or "9999-12-31") <= asof
    }
    return freeze_path, dict(existing_lanes), eligible_frozen_lanes


def _persist(
    *, asof: str, rows: list[dict], labels: dict, report: dict,
    freeze_path: str, existing_lanes: dict,
) -> dict:
    artifact_key = report["version_hashes"]["observation_set_sha256"][:16]
    artifact_dir = data_file("stock-triage", "four_dim_weight_shadow")
    report_path = os.path.join(artifact_dir, f"{asof}-{artifact_key}.json")
    labels_path = os.path.join(artifact_dir, f"{asof}-{artifact_key}-labels.json")
    latest_path = data_file("stock-triage", "four_dim_weight_shadow_latest.json")
    rollback_path = data_file("stock-triage", "four_dim_weight_shadow_rollback.json")
    merged_frozen_lanes = dict(existing_lanes)
    for lane, payload in report["frozen_lanes"].items():
        merged_frozen_lanes.setdefault(lane, payload)
    atomic_write_json(freeze_path, {
        "schema": "four_dim_weight_fit_freeze_set_v1",
        "asof": asof,
        "research_only": True,
        "live_effect": "none",
        "lanes": merged_frozen_lanes,
    })
    atomic_write_json(labels_path, {
        "schema": "four_dim_forward_label_set_v1",
        "asof": asof,
        "research_only": True,
        "live_effect": "none",
        "labels": labels,
        "label_set_sha256": report["version_hashes"]["label_set_sha256"],
    })
    atomic_write_json(report_path, report)
    atomic_write_json(latest_path, {**report, "artifact_path": report_path, "labels_path": labels_path})
    atomic_write_json(rollback_path, {
        "schema": "four_dim_weight_shadow_rollback_v1",
        "asof": asof,
        "action": "no_live_change",
        "live_effect": "none",
        "authoritative_config": "config/scoring.yaml",
        "scoring_config_sha256": report["version_hashes"]["scoring_config_sha256"],
        "lanes": {lane: payload.get("rollback") for lane, payload in report["lanes"].items()},
    })
    return {
        "schema": "four_dim_weight_shadow_run_v1",
        "asof": asof,
        "status": report["status"],
        "research_only": True,
        "live_effect": "none",
        "observation_count": len(rows),
        "complete_label_count": sum(1 for value in labels.values() if value.get("status") == "complete"),
        "lane_status": {lane: payload["status"] for lane, payload in report["lanes"].items()},
        "report_path": report_path,
        "labels_path": labels_path,
        "rollback_path": rollback_path,
        "freeze_path": freeze_path,
        "config_unchanged": True,
        "promotion_allowed": False,
    }


def build(*, asof: str, posterior_draws: int = 4096) -> dict:
    config_before = _config_sha256()
    settings = _shadow_settings()
    rows = [
        row for row in observations.load_scores(schemas=[observations.SCHEMA])
        if str(row.get("trading_date") or "") <= asof
    ]
    labels = research.build_labels(rows, asof=asof, assumed_notional=settings["assumed_notional"])
    freeze_path, existing_lanes, eligible_frozen_lanes = _frozen_lanes(asof)
    report = research.build_shadow_report(
        rows, labels, current_weights=_current_weights(), posterior_draws=posterior_draws,
        min_fit_days=settings["minimum_fit_trading_days"],
        min_oos_days=settings["minimum_unseen_oos_trading_days"], asof=asof,
        frozen_lanes=eligible_frozen_lanes,
    )
    config_after = _config_sha256()
    report.update({
        "config_unchanged": config_before == config_after,
        "config_sha256_before": config_before,
        "config_sha256_after": config_after,
    })
    if not report["config_unchanged"]:
        raise RuntimeError("shadow evaluation mutated scoring config")
    return _persist(
        asof=asof, rows=rows, labels=labels, report=report,
        freeze_path=freeze_path, existing_lanes=existing_lanes,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--posterior-draws", type=int, default=4096)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = build(asof=args.asof, posterior_draws=args.posterior_draws)
    print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
