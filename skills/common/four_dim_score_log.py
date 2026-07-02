"""Append-only log of four-dimension sub-scores, keyed by (code, date).

Instrumentation for T5: the four_dim sub-scores (technical / sentiment /
catalyst / deep) that scoring.yaml's 30/15/30/25 weights actually control are
computed only on the daily top-N shortlist by batch_four_dim_scorer, and today
they are never persisted alongside a settled outcome. Without that pairing, the
original "calibrate the four_dim weights against T+3 results" study has no data.

This log captures each sub-score at scoring time. Because the scored codes are a
subset of the candidate_lifecycle universe (which already settles T+1/T+3/max_gain
for the whole universe), a later join on (code, date) yields sub-scores paired
with settled outcomes. This closes the data gap without touching the fragile
recommendation/settlement path or running four_dim on the full universe.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Mapping

from paths import data_file
from state_store import file_lock

SCHEMA = "four_dim_score_log_v1"
DIMENSIONS = ("technical", "sentiment", "catalyst", "deep")


def _code(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text[2:] if text.startswith(("sh", "sz")) else text.zfill(6)


def log_path() -> str:
    return data_file("stock-triage", "four_dim_score_log.jsonl")


def _sub_score(scores: Mapping[str, Any], dim: str) -> float | None:
    block = scores.get(dim) if isinstance(scores, Mapping) else None
    value = block.get("score") if isinstance(block, Mapping) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def record_scores(batch_result: Mapping[str, Any], *, asof: str, path: str | None = None) -> int:
    """Append one row per successfully scored stock in a batch result.

    Never raises: instrumentation must not break the scorer's own output. Rows
    with no usable sub-scores are skipped. Returns the number of rows written.
    """
    results = batch_result.get("results") if isinstance(batch_result, Mapping) else None
    if not isinstance(results, list):
        return 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for item in results:
        if not isinstance(item, Mapping) or item.get("status") == "failed":
            continue
        scores = item.get("scores")
        subs = {dim: _sub_score(scores or {}, dim) for dim in DIMENSIONS}
        if all(value is None for value in subs.values()):
            continue
        row = {
            "schema": SCHEMA,
            "code": _code(item.get("code")),
            "date": asof,
            "strategy_lane": item.get("strategy_lane"),
            "weighted": item.get("weighted"),
            "grade": item.get("grade"),
            "recorded_at": now,
            **subs,
        }
        rows.append(row)
    if not rows:
        return 0

    target = path or log_path()
    try:
        with file_lock(target):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except (OSError, TimeoutError):
        return 0
    return len(rows)


def load_scores(path: str | None = None) -> list[dict[str, Any]]:
    """Read all logged sub-score rows. Corrupt lines are skipped, not fatal."""
    target = path or log_path()
    if not os.path.exists(target):
        return []
    rows: list[dict[str, Any]] = []
    with open(target, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows
