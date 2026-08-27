"""Quantify what 5-minute close-state reconstruction cannot observe.

This module deliberately does not create canonical limit-up events. It emits
an explicitly approximate representation for bias measurement only. A 5-minute
bar cannot reveal an open-and-reseal sequence that happened inside the same
bar, so its output is never eligible for ``divergence_reseal`` research.
"""

from __future__ import annotations

from statistics import mean
from typing import Any, Mapping, Sequence


SOURCE_RECONSTRUCTED_5M = "reconstructed_5m_close_state_v1"
APPROXIMATE_CLOSE_STATE = "approximate:5m_close_state_intrabar_unobservable"
INELIGIBILITY_REASON = "5m_close_state_cannot_observe_intrabar_open_and_reseal"
FAST_BOARD_MAX_MINUTE = 9 * 60 + 31


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _minutes(value: Any) -> int | None:
    text = str(value or "").strip()
    if " " in text:
        text = text.rsplit(" ", 1)[-1]
    text = text.replace(":", "")
    if len(text) < 4 or not text[:4].isdigit():
        return None
    hour, minute = int(text[:2]), int(text[2:4])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _compact_time(value: Any) -> str | None:
    minute = _minutes(value)
    if minute is None:
        return None
    return f"{minute // 60:02d}{minute % 60:02d}00"


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "event_source": SOURCE_RECONSTRUCTED_5M,
        "first_seal_time": None,
        "last_seal_time": None,
        "reseal_time": None,
        "open_board_count": None,
        "field_availability": {},
        "eligible_for_divergence_reseal": False,
        "ineligibility_reason": reason,
    }


def infer_5m_close_state(
    rows: Sequence[Mapping[str, Any]],
    *,
    limit_price: Any,
    tolerance: float = 0.005,
) -> dict[str, Any]:
    """Infer visible sealed segments from 5-minute closing prices.

    Segment transitions are bar-close observations, not exchange events. The
    result is suitable only for comparison with a real event feed.
    """
    cap = _number(limit_price)
    if cap is None or cap <= 0 or not rows:
        return _unavailable("limit_price_or_minute_rows_missing")

    ordered = sorted(rows, key=lambda row: _minutes(row.get("time")) or -1)
    states: list[bool] = []
    times: list[str] = []
    for row in ordered:
        close = _number(row.get("close"))
        compact = _compact_time(row.get("time"))
        if close is None or compact is None:
            return _unavailable("minute_close_or_time_missing")
        states.append(close >= cap - abs(float(tolerance)))
        times.append(compact)

    segment_starts = [
        index for index, sealed in enumerate(states)
        if sealed and (index == 0 or not states[index - 1])
    ]
    if not segment_starts:
        return _unavailable("no_5m_bar_closed_at_limit")

    open_count = len(segment_starts) - 1
    availability = {
        field: APPROXIMATE_CLOSE_STATE
        for field in (
            "first_seal_time", "last_seal_time", "reseal_time", "open_board_count"
        )
    }
    return {
        "status": "ok",
        "event_source": SOURCE_RECONSTRUCTED_5M,
        "first_seal_time": times[segment_starts[0]],
        "last_seal_time": times[segment_starts[-1]],
        "reseal_time": times[segment_starts[-1]] if open_count > 0 else None,
        "open_board_count": open_count,
        "field_availability": availability,
        "eligible_for_divergence_reseal": False,
        "ineligibility_reason": INELIGIBILITY_REASON,
    }


def _numeric_metric(pairs: Sequence[tuple[float, float]]) -> dict[str, Any]:
    errors = [abs(left - right) for left, right in pairs]
    exact = sum(1 for error in errors if error == 0)
    return {
        "comparable": len(errors),
        "exact_matches": exact,
        "exact_match_rate": round(exact / len(errors), 4) if errors else None,
        "mean_absolute_error": round(mean(errors), 4) if errors else None,
    }


def _time_metric(pairs: Sequence[tuple[Any, Any]]) -> dict[str, Any]:
    errors = [
        abs(left - right)
        for truth, inferred in pairs
        if (left := _minutes(truth)) is not None
        and (right := _minutes(inferred)) is not None
    ]
    within_five = sum(1 for error in errors if error <= 5)
    return {
        "comparable": len(errors),
        "within_5m": within_five,
        "within_5m_rate": round(within_five / len(errors), 4) if errors else None,
        "mean_absolute_error_minutes": round(mean(errors), 4) if errors else None,
    }


def _fast_board_metric(pairs: Sequence[tuple[Any, Any]]) -> dict[str, Any]:
    truth_positive = 0
    true_positive = 0
    for truth, inferred in pairs:
        truth_minute = _minutes(truth)
        inferred_minute = _minutes(inferred)
        if truth_minute is None or truth_minute > FAST_BOARD_MAX_MINUTE:
            continue
        truth_positive += 1
        if inferred_minute is not None and inferred_minute <= FAST_BOARD_MAX_MINUTE:
            true_positive += 1
    return {
        "truth_positive": truth_positive,
        "true_positive": true_positive,
        "recall": round(true_positive / truth_positive, 4) if truth_positive else None,
    }


def build_bias_report(
    truth_events: Sequence[Mapping[str, Any]],
    reconstructed: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[str, Any]:
    """Compare real event metadata with approximate 5-minute observations."""
    covered: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for truth in truth_events:
        key = (str(truth.get("date") or ""), str(truth.get("code") or "").zfill(6))
        inferred = reconstructed.get(key)
        if inferred and inferred.get("status") == "ok":
            covered.append((truth, inferred))

    board_pairs = [
        (_number(truth.get("open_board_count")), _number(inferred.get("open_board_count")))
        for truth, inferred in covered
    ]
    numeric_pairs = [
        (left, right) for left, right in board_pairs if left is not None and right is not None
    ]
    first_pairs = [(truth.get("first_seal_time"), item.get("first_seal_time"))
                   for truth, item in covered]
    last_pairs = [(truth.get("last_seal_time"), item.get("last_seal_time"))
                  for truth, item in covered]
    reseal_pairs = [(truth.get("reseal_time"), item.get("reseal_time"))
                    for truth, item in covered]
    total = len(truth_events)
    coverage = len(covered) / total if total else 0.0
    return {
        "schema": "limitup_reconstruction_bias_v1",
        "status": "ok" if total and covered else "blocked",
        "truth_events": total,
        "covered_events": len(covered),
        "coverage_ratio": round(coverage, 4),
        "open_board_count": _numeric_metric(numeric_pairs),
        "first_seal_time": _time_metric(first_pairs),
        "last_seal_time": _time_metric(last_pairs),
        "reseal_time": _time_metric(reseal_pairs),
        "fast_board_recall": _fast_board_metric(first_pairs),
        "event_source": SOURCE_RECONSTRUCTED_5M,
        "eligible_for_divergence_reseal": False,
        "ineligibility_reason": INELIGIBILITY_REASON,
    }
