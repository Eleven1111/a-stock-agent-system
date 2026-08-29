#!/usr/bin/env python3
"""T5 research report: discovery-score calibration + four_dim join readiness.

Two questions, both answered from settled candidate_lifecycle data:

1. Do the discovery ranking scores (daban_score / trend_score) and their raw
   input features actually predict settled outcomes? Reported as Spearman IC
   per outcome metric, plus quantile-bucket outcome profiles, plus each feature's
   information coefficient next to its current *hardcoded* weight in
   candidate_pipeline.py — so a human can see mismatches (high weight / low IC).

2. Is the original four_dim (30/15/30/25) calibration unblocked yet? Joins the
   four_dim_score_log sidecar against settled outcomes; reports insufficient_data
   until enough paired rows accumulate.

Research-only: this prints evidence. It does not change any weight. The current
weights below mirror candidate_pipeline.py and must be kept in sync by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path
sys.path.insert(0, ROOT)

import lifecycle_analytics as la  # noqa: E402
import four_dim_score_log as fdl  # noqa: E402
import signal_ledger  # noqa: E402
from scripts import recommendation_feedback as rf  # noqa: E402

# Persisted raw feature -> its current hardcoded weight in candidate_pipeline.py.
# Mirror only; keep in sync with the score formulas there.
DABAN_FEATURE_WEIGHTS = {
    "change_pct": 0.15,
    "amount": 0.15,
    "turnover": 0.12,
    "momentum_5d": 0.10,
    "volume_ratio_5d": 0.10,
    "breakout_20d": 0.10,
}
TREND_FEATURE_WEIGHTS = {
    "momentum_20d": 0.22,
    "momentum_60d": 0.18,
    "amount": 0.12,
    "above_ma20": 0.10,
    "above_ma60": 0.10,
    "breakout_20d": 0.10,
    "volume_ratio_5d": 0.08,
    "volatility_20d": 0.10,
}
COMPOSITE_SCORES = ("daban_score", "trend_score", "leader_score")
DEFAULT_OUTCOMES = ("t1_close_ret", "t3_close_ret", "max_gain")


def _ic_by_outcome(records, score_key, outcomes):
    table = {}
    for outcome_key in outcomes:
        table[outcome_key] = la.spearman_ic(
            (r.get(score_key), la.outcome_value(r, outcome_key)) for r in records
        )
    return table


def _feature_table(records, weights, outcomes):
    rows = []
    for feature, weight in weights.items():
        rows.append({
            "feature": feature,
            "current_weight": weight,
            "ic_by_outcome": _ic_by_outcome(records, feature, outcomes),
        })
    # Surface the biggest weight/signal mismatches first: rank by |IC| on the
    # primary outcome descending, so under-weighted-but-predictive features and
    # over-weighted-but-noisy ones are both easy to spot.
    primary = outcomes[0]
    rows.sort(
        key=lambda row: abs(row["ic_by_outcome"][primary]["ic"] or 0.0),
        reverse=True,
    )
    return rows


def _four_dim_join(records, outcomes, log_path=None):
    logged = fdl.load_scores(log_path)
    if not logged:
        return {
            "status": "insufficient_data",
            "note": (
                "No four_dim sub-scores logged yet. The instrumentation now records "
                "them at scoring time; this section fills in as scored days settle."
            ),
            "paired_rows": 0,
        }
    outcome_by_key = {(r.get("asof"), la._code(r.get("code"))): r for r in records}
    joined = []
    for row in logged:
        key = (row.get("trading_date") or row.get("date"), la._code(row.get("code")))
        settled = outcome_by_key.get(key)
        if settled is None:
            continue
        joined.append({**row, "_outcome_record": settled})
    if len(joined) < 10:
        return {
            "status": "insufficient_data",
            "note": (
                "Fewer than 10 four_dim rows are paired with a settled outcome. "
                "Accumulating; the four_dim (30/15/30/25) calibration runs once this grows."
            ),
            "paired_rows": len(joined),
        }
    ic = {}
    for dim in fdl.DIMENSIONS:
        # Attach the sub-score onto a copy of the settled lifecycle record so the
        # shared IC helper reads score (top level) and outcome (under "outcome").
        rows = [
            {
                **j["_outcome_record"],
                dim: (
                    ((j.get("dimensions") or {}).get(dim) or {}).get("score")
                    if j.get("schema") == fdl.SCHEMA
                    else j.get(dim)
                ),
            }
            for j in joined
        ]
        ic[dim] = _ic_by_outcome(rows, dim, outcomes)
    return {"status": "ok", "paired_rows": len(joined), "ic_by_dimension": ic}


def _useful_rate(rows: list[dict]) -> float:
    useful = sum(1 for row in rows if row.get("verdict") == "useful")
    return round(useful / len(rows), 4) if rows else 0.0


def _group_stats(rows: list[dict], key: str) -> dict[str, dict]:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        group_key = str(row.get(key) or "unknown")
        groups.setdefault(group_key, []).append(row)
    return {
        group_key: {
            "total": len(group_rows),
            "useful_rate": _useful_rate(group_rows),
        }
        for group_key, group_rows in groups.items()
    }


def build_feedback_stats(ledger_file: str | None = None) -> dict:
    """人工反馈统计：整体 useful 率 + 按策略/通道分组。

    每个 signal_id 只取最新一条反馈（允许人工更正认知）。没有反馈事件时返回
    insufficient_data，不影响报告其余部分（向后兼容旧调用不传 ledger_file）。
    """
    path = ledger_file or signal_ledger.LEDGER_FILE
    events = signal_ledger.read_events(path)
    latest_by_signal = rf.latest_feedback_by_signal(events)
    rows = [
        {
            "signal_id": signal_id,
            "verdict": payload.get("verdict"),
            "strategy_id": payload.get("strategy_id") or "default",
            "source": payload.get("source") or "unknown",
        }
        for signal_id, payload in latest_by_signal.items()
        if payload.get("verdict") in {"useful", "not_useful"}
    ]
    if not rows:
        return {
            "schema": "a_stock_recommendation_feedback_stats_v1",
            "status": "insufficient_data",
            "total_feedback": 0,
            "overall_useful_rate": None,
            "by_strategy": {},
            "by_source": {},
        }
    return {
        "schema": "a_stock_recommendation_feedback_stats_v1",
        "status": "ok",
        "total_feedback": len(rows),
        "overall_useful_rate": _useful_rate(rows),
        "by_strategy": _group_stats(rows, "strategy_id"),
        "by_source": _group_stats(rows, "source"),
    }


def build_report(
    *,
    days=None,
    outcomes=DEFAULT_OUTCOMES,
    four_dim_log_path=None,
    feedback_ledger_path=None,
) -> dict:
    records = la.load_settled_records(days)
    composite = {}
    for score_key in COMPOSITE_SCORES:
        composite[score_key] = {
            "ic_by_outcome": _ic_by_outcome(records, score_key, outcomes),
            "buckets": {
                outcome_key: la.quantile_buckets(records, score_key, outcome_key, n_buckets=5)
                for outcome_key in outcomes
            },
        }
    settled_day_count = len({r.get("asof") for r in records})
    return {
        "schema": "a_stock_score_calibration_report_v1",
        "status": "ok" if records else "insufficient_data",
        "research_only": True,
        "note": (
            "Directional evidence, not conclusive: "
            f"{settled_day_count} settled trading day(s), {len(records)} records. "
            "IC and bucket monotonicity firm up as settled days accumulate. This "
            "report never changes a weight; any retune is a separate human decision."
        ),
        "outcomes": list(outcomes),
        "settled_days": settled_day_count,
        "sample_size": len(records),
        "composite_scores": composite,
        "daban_features": _feature_table(records, DABAN_FEATURE_WEIGHTS, outcomes),
        "trend_features": _feature_table(records, TREND_FEATURE_WEIGHTS, outcomes),
        "four_dim_calibration": _four_dim_join(records, outcomes, four_dim_log_path),
        "feedback": build_feedback_stats(feedback_ledger_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", action="append", dest="days", help="Limit to specific asof day(s)")
    args = parser.parse_args()
    print(json.dumps(build_report(days=args.days), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
