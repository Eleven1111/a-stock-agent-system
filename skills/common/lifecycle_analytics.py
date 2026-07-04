"""Read-only analytics over candidate_lifecycle: funnel recall/regret + score calibration.

Research-only by construction: this module reads lifecycle day files and computes
evidence. It never mutates lifecycle state and never changes any scoring weight or
gate. Per the plan's discipline, weight/gate changes only ever produce reports for
human review, they do not take effect automatically.

Why this data source: candidate_lifecycle records the full ~3000-stock discovery
universe per trading day with component scores AND settled T+1/T+3/max_gain
outcomes. The signal_ledger, by contrast, holds only a handful of recommendations
and none are settled, so it is unusable for statistics. Lifecycle is ~200x richer.
"""

from __future__ import annotations

import glob
import os
from statistics import mean, median
from typing import Any, Iterable, Mapping, Sequence

from paths import data_file
from state_store import read_json


# Discovery funnel gates in execution order. Each stage name matches the string
# passed to candidate_lifecycle.transition() by the daban lane scripts.
FUNNEL_STAGES: tuple[str, ...] = (
    "discovery",
    "auction_shortlist",
    "open_confirmed",
    "hot_money_morning",
    "hot_money_afternoon",
    "afternoon_reflow",
)

# Settled outcome metrics available on each resolved record's `outcome` block.
OUTCOME_KEYS: tuple[str, ...] = (
    "t1_open_ret",
    "t1_close_ret",
    "t3_close_ret",
    "max_gain",
    "max_drawdown",
)


def _code(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text[2:] if text.startswith(("sh", "sz")) else text.zfill(6)


def lifecycle_glob() -> str:
    return data_file("stock-triage", os.path.join("candidate_lifecycle", "*.json"))


def available_days() -> list[str]:
    days = []
    for path in glob.glob(lifecycle_glob()):
        name = os.path.splitext(os.path.basename(path))[0]
        if name and not name.endswith((".bak", ".lock")):
            days.append(name)
    return sorted(set(days))


def load_day(asof: str) -> dict[str, Any]:
    path = data_file("stock-triage", os.path.join("candidate_lifecycle", f"{asof}.json"))
    return read_json(path, {"schema": "candidate_lifecycle_v1", "asof": asof, "records": []})


def _is_settled(record: Mapping[str, Any]) -> bool:
    return bool((record.get("outcome") or {}).get("resolved"))


def load_settled_records(days: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Return every settled record across the given (or all) days, each tagged
    with its source `asof` so per-day breakdowns remain possible after pooling."""
    target_days = list(days) if days is not None else available_days()
    pooled: list[dict[str, Any]] = []
    for asof in target_days:
        day = load_day(asof)
        for record in day.get("records") or []:
            if _is_settled(record):
                tagged = dict(record)
                tagged["asof"] = asof
                pooled.append(tagged)
    return pooled


def outcome_value(record: Mapping[str, Any], key: str) -> float | None:
    value = (record.get("outcome") or {}).get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def passed_stages(record: Mapping[str, Any]) -> set[str]:
    """Stages this candidate was *selected at* (i.e. passed and advanced), read
    from its stage_history. A stage where selected is False is a rejection, not a
    pass. Discovery is not implicit: a candidate passes discovery only if its
    discovery event marks it selected into the watch pool."""
    passed = set()
    for event in record.get("stage_history") or []:
        if event.get("selected") and event.get("stage"):
            passed.add(str(event["stage"]))
    return passed


def rejected_at_stage(record: Mapping[str, Any]) -> str | None:
    """The gate this candidate was rejected at, or None if it was never rejected
    (i.e. it advanced as far as the pipeline took it)."""
    current = str(record.get("current_stage") or "")
    if current == "discovery_rejected":
        return "discovery"
    if current.startswith("rejected:"):
        return current.split(":", 1)[1]
    return None


def _rank(values: Sequence[float]) -> list[float]:
    """Average-rank transform (ties share the mean rank), for Spearman IC."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def spearman_ic(pairs: Iterable[tuple[float | None, float | None]]) -> dict[str, Any]:
    """Rank correlation between a score and an outcome. Returns coefficient plus
    sample size so a caller can judge whether it is statistically meaningful."""
    clean = [(float(s), float(o)) for s, o in pairs if s is not None and o is not None]
    n = len(clean)
    if n < 3:
        return {"ic": None, "n": n, "note": "insufficient_sample"}
    scores = [s for s, _ in clean]
    outcomes = [o for _, o in clean]
    rs = _rank(scores)
    ro = _rank(outcomes)
    mean_rs = mean(rs)
    mean_ro = mean(ro)
    cov = sum((a - mean_rs) * (b - mean_ro) for a, b in zip(rs, ro))
    var_s = sum((a - mean_rs) ** 2 for a in rs)
    var_o = sum((b - mean_ro) ** 2 for b in ro)
    if var_s <= 0 or var_o <= 0:
        return {"ic": None, "n": n, "note": "degenerate_variance"}
    return {"ic": round(cov / (var_s ** 0.5 * var_o ** 0.5), 4), "n": n}


def ordered_stages(records: Sequence[Mapping[str, Any]]) -> list[str]:
    """Funnel gates actually present in the data, in execution order. Known
    stages sort by FUNNEL_STAGES; any unrecognised stage sorts to the end so the
    report degrades gracefully if the daban lane adds a new stage."""
    seen: set[str] = set()
    for record in records:
        seen |= passed_stages(record)
        rej = rejected_at_stage(record)
        if rej:
            seen.add(rej)
    known = [s for s in FUNNEL_STAGES if s in seen]
    extra = sorted(s for s in seen if s not in FUNNEL_STAGES)
    return known + extra


def funnel_analysis(
    records: Sequence[Mapping[str, Any]],
    *,
    outcome_key: str = "t3_close_ret",
    big_mover_threshold: float = 9.9,
) -> dict[str, Any]:
    """Per-gate recall of big movers and regret of rejected candidates.

    recall  = big movers that passed the gate / big movers that entered it.
    regret  = outcome distribution of candidates the gate rejected (what the
              gate left on the table). A high count of big movers among the
              rejected is the actionable signal.
    """
    stages = ordered_stages(records)
    reached_by = [(r, passed_stages(r), rejected_at_stage(r)) for r in records]
    gates = []
    for idx, stage in enumerate(stages):
        prev = stages[idx - 1] if idx > 0 else None
        entrants = [
            (r, reached, rej)
            for (r, reached, rej) in reached_by
            if prev is None or prev in reached
        ]
        passed = [t for t in entrants if stage in t[1]]
        rejected = [t for t in reached_by if t[2] == stage]

        def _big(bundle):
            out = []
            for r, _reached, _rej in bundle:
                v = outcome_value(r, outcome_key)
                if v is not None and v >= big_mover_threshold:
                    out.append(r)
            return out

        entered_big = _big(entrants)
        passed_big = _big(passed)
        rejected_outcomes = [
            v for (r, _s, _j) in rejected if (v := outcome_value(r, outcome_key)) is not None
        ]
        rejected_big = [
            r for (r, _s, _j) in rejected
            if (v := outcome_value(r, outcome_key)) is not None and v >= big_mover_threshold
        ]
        gates.append({
            "stage": stage,
            "entered": len(entrants),
            "passed": len(passed),
            "rejected": len(rejected),
            "big_movers_entered": len(entered_big),
            "big_movers_passed": len(passed_big),
            "recall": (
                round(len(passed_big) / len(entered_big), 4) if entered_big else None
            ),
            "rejected_mean_outcome": (
                round(mean(rejected_outcomes), 3) if rejected_outcomes else None
            ),
            "rejected_median_outcome": (
                round(median(rejected_outcomes), 3) if rejected_outcomes else None
            ),
            "big_movers_wrongly_rejected": len(rejected_big),
        })
    return {
        "outcome_key": outcome_key,
        "big_mover_threshold": big_mover_threshold,
        "sample_size": len(records),
        "gates": gates,
    }


DEFAULT_RECALL_SOURCE = "full_market_enumeration"


def recall_source(record: Mapping[str, Any]) -> str:
    """The candidate-discovery channel that surfaced this record.

    Records written before the recall_source field existed (or any record
    from a channel that never tags itself) default to the historical
    full-market enumeration channel rather than an unlabelled bucket, so
    older lifecycle days remain attributable without a schema migration.
    """
    value = record.get("recall_source")
    return str(value) if isinstance(value, str) and value else DEFAULT_RECALL_SOURCE


def recall_source_breakdown(
    records: Sequence[Mapping[str, Any]],
    *,
    outcome_key: str = "t3_close_ret",
    big_mover_threshold: float = 9.9,
) -> dict[str, Any]:
    """Per-recall_source recall/regret funnel, so a second recall channel's
    contribution (e.g. nl_screening_eastmoney) can be compared against the
    primary full-market enumeration channel without changing any gate or
    weight. Research-only: read-only over settled lifecycle records.
    """
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        grouped.setdefault(recall_source(record), []).append(record)

    per_source: dict[str, Any] = {}
    for source, source_records in grouped.items():
        funnel = funnel_analysis(
            source_records,
            outcome_key=outcome_key,
            big_mover_threshold=big_mover_threshold,
        )
        settled_outcomes = [
            v for r in source_records if (v := outcome_value(r, outcome_key)) is not None
        ]
        per_source[source] = {
            "sample_size": len(source_records),
            "settled_count": len(settled_outcomes),
            "mean_outcome": round(mean(settled_outcomes), 3) if settled_outcomes else None,
            "big_movers": sum(1 for v in settled_outcomes if v >= big_mover_threshold),
            "funnel": funnel,
        }
    return {
        "outcome_key": outcome_key,
        "big_mover_threshold": big_mover_threshold,
        "sources": per_source,
    }


def quantile_buckets(
    records: Sequence[Mapping[str, Any]],
    score_key: str,
    outcome_key: str,
    n_buckets: int = 5,
) -> list[dict[str, Any]]:
    """Sort by score, split into equal-count buckets, report each bucket's mean
    outcome. A monotonically increasing profile means the score is predictive."""
    pairs = [
        (float(r[score_key]), o)
        for r in records
        if isinstance(r.get(score_key), (int, float))
        and not isinstance(r.get(score_key), bool)
        and (o := outcome_value(r, outcome_key)) is not None
    ]
    if len(pairs) < n_buckets:
        return []
    pairs.sort(key=lambda p: p[0])
    buckets = []
    size = len(pairs) / n_buckets
    for b in range(n_buckets):
        lo = int(round(b * size))
        hi = int(round((b + 1) * size)) if b < n_buckets - 1 else len(pairs)
        chunk = pairs[lo:hi]
        if not chunk:
            continue
        outs = [o for _, o in chunk]
        buckets.append({
            "bucket": b + 1,
            "score_min": round(chunk[0][0], 2),
            "score_max": round(chunk[-1][0], 2),
            "mean_outcome": round(mean(outs), 3),
            "median_outcome": round(median(outs), 3),
            "n": len(chunk),
        })
    return buckets
