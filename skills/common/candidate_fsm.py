"""Single authority for candidate state transitions.

The discovery -> observation -> candidate -> confirmation pipeline previously
let candidate_discovery / closing-triage / open_confirmation each decide when
a stock advances or is dropped, with no shared guard and no recorded reason.
This module is the only place allowed to move a stock between the four FSM
states below; every attempted move (accepted or rejected) is appended to a
durable transition log next to the existing candidate_lifecycle records so a
single ticker's full history can be replayed on demand.

States: screened -> watching -> candidate -> confirmed, any state -> dropped.
Guards are pure functions of the current record + caller-supplied evidence;
they never perform I/O beyond the read-only research_bus lookup used by the
candidate -> confirmed review gate. No model name or vendor is referenced
anywhere in this module; review behavior is controlled purely by the
`review_gate.mode` config value (off / advisory / enforce).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from paths import data_file
from state_store import mutate_json, read_json

SCHEMA = "candidate_fsm_v1"

STATES = ("screened", "watching", "candidate", "confirmed", "dropped")
TERMINAL_STATE = "dropped"

# Legal (from_state -> to_state) edges. Any state may drop; drops are handled
# separately from this table because "any live state" is not enumerable here.
ALLOWED_EDGES: dict[str, set[str]] = {
    "screened": {"watching"},
    "watching": {"candidate"},
    "candidate": {"confirmed", "watching"},
    "confirmed": {"candidate"},
}

REASON_CODES = {
    # forward progress
    "score_above_threshold",
    "research_bus_advance",
    "manual_promote",
    # candidate -> confirmed guard outcomes
    "research_verdict_clear",
    "review_gate_off",
    "review_gate_advisory_pending",
    "review_gate_advisory_no_task",
    # confirmed -> candidate (demotion, not a drop)
    "research_rejected",
    "confirm_stall",
    # dropped reasons
    "theme_fading",
    "research_disputed",
    "stale_watch",
    "manual_drop",
    "screened_rejected",
}

# Reason codes that legally accompany a downgrade to `watching` from `candidate`
# or `confirmed` (never a hard drop).
DOWNGRADE_REASON_CODES = {"research_rejected", "confirm_stall"}

DEFAULT_REVIEW_GATE = {
    "mode": "advisory",
    "task_kind": "candidate_deep_dive",
}
DEFAULT_TIMEOUTS = {
    "watching_stale_days": 5,
    "confirmed_stall_days": 3,
}


def _code(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text[2:] if text.startswith(("sh", "sz")) else text.zfill(6)


def _now_text(now: str | None = None) -> str:
    return now or datetime.now().isoformat(timespec="seconds")


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def config_path() -> str:
    return os.environ.get("A_STOCK_CANDIDATE_SELECTION_CONFIG") or os.path.join(
        _repo_root(), "config", "candidate_selection.json"
    )


def load_fsm_config(path: str | None = None) -> dict[str, Any]:
    """Read the `candidate_fsm` section; missing/invalid config fails safe to
    defaults (review gate advisory, non-zero timeouts) so a broken config
    never blocks the confirmation pipeline."""
    try:
        with open(path or config_path(), encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        payload = {}
    section = payload.get("candidate_fsm") if isinstance(payload, dict) else None
    section = dict(section) if isinstance(section, dict) else {}
    review_gate = {**DEFAULT_REVIEW_GATE, **(section.get("review_gate") or {})}
    if review_gate.get("mode") not in {"off", "advisory", "enforce"}:
        review_gate["mode"] = DEFAULT_REVIEW_GATE["mode"]
    timeouts = {**DEFAULT_TIMEOUTS, **(section.get("timeouts") or {})}
    return {"review_gate": review_gate, "timeouts": timeouts}


def transitions_file(asof: str) -> str:
    return data_file("stock-triage", os.path.join("candidate_fsm", f"{asof}.json"))


def load_day(asof: str) -> dict[str, Any]:
    return read_json(
        transitions_file(asof),
        {"schema": SCHEMA, "asof": asof, "transitions": []},
    )


def _append_event(asof: str, event: dict[str, Any]) -> dict[str, Any]:
    def _mutate(state: Any) -> dict[str, Any]:
        if not isinstance(state, dict):
            state = {"schema": SCHEMA, "asof": asof, "transitions": []}
        state.setdefault("transitions", []).append(event)
        state["updated_at"] = event["at"]
        return state

    return mutate_json(transitions_file(asof), _mutate, load_day(asof))


def current_state(code: str, *, days: Sequence[str] | None = None) -> dict[str, Any] | None:
    """Return the most recent accepted transition event for `code`, or None
    if it has never entered the FSM. Scans transition-log day files, newest
    first, among the given (or discovered) days."""
    import glob

    code = _code(code)
    candidate_days = list(days) if days is not None else sorted(
        (
            os.path.splitext(os.path.basename(path))[0]
            for path in glob.glob(data_file("stock-triage", os.path.join("candidate_fsm", "*.json")))
        ),
        reverse=True,
    )
    for day in candidate_days:
        events = [
            event for event in load_day(day).get("transitions", [])
            if _code(event.get("code")) == code and event.get("accepted")
        ]
        if events:
            return events[-1]
    return None


def codes_in_state(state: str, *, days: Sequence[str] | None = None) -> dict[str, str]:
    """Return {code: last_seen_asof} for every code whose current FSM state
    equals `state`. Used by the timeout sweeps to discover their own input
    set instead of requiring callers to pass a codes list by hand."""
    import glob

    candidate_days = list(days) if days is not None else sorted(
        os.path.splitext(os.path.basename(path))[0]
        for path in glob.glob(data_file("stock-triage", os.path.join("candidate_fsm", "*.json")))
    )
    last_by_code: dict[str, dict[str, Any]] = {}
    for day in candidate_days:
        for event in load_day(day).get("transitions", []):
            if not event.get("accepted"):
                continue
            code = _code(event.get("code"))
            last_by_code[code] = event
    return {
        code: str(event.get("asof"))
        for code, event in last_by_code.items()
        if event.get("to_state") == state
    }


def _find_deep_dive_task(
    code: str,
    trading_date: str,
    task_kind: str,
) -> dict[str, Any] | None:
    """Read-only research_bus lookup, isolated so a bus outage degrades to
    "no task found" instead of raising through the FSM guard."""
    try:
        import research_bus

        key = research_bus.subject_key({"code": code})
        task_id = research_bus.make_task_id(task_kind, key, trading_date)
        return research_bus.find_task(task_id)
    except Exception:  # noqa: BLE001 - read-only lookup must never block FSM
        return None


def _review_gate_check(
    code: str,
    trading_date: str,
    review_gate: Mapping[str, Any],
) -> dict[str, Any]:
    """Read-only lookup of today's candidate_deep_dive verdict for `code`.

    Returns {"outcome": "clear"|"blocked"|"downgrade", "reason_code": str,
    "task_id": str|None, "verdict": str|None}. Never writes to research_bus.
    """
    mode = str(review_gate.get("mode") or "advisory")
    if mode == "off":
        return {"outcome": "clear", "reason_code": "review_gate_off", "task_id": None, "verdict": None}

    task_kind = str(review_gate.get("task_kind") or "candidate_deep_dive")
    task = _find_deep_dive_task(code, trading_date, task_kind)
    task_id = task.get("id") if isinstance(task, dict) else None
    verdict = task.get("verdict") if isinstance(task, dict) else None

    if verdict == "rejected":
        return {"outcome": "downgrade", "reason_code": "research_rejected", "task_id": task_id, "verdict": verdict}
    if verdict == "disputed":
        outcome = "blocked" if mode == "enforce" else "clear"
        return {"outcome": outcome, "reason_code": "research_disputed", "task_id": task_id, "verdict": verdict}
    if verdict in {"advance", "watch", "abstained"}:
        return {"outcome": "clear", "reason_code": "research_verdict_clear", "task_id": task_id, "verdict": verdict}

    # no task, or task not yet terminal
    pending_reason = "review_gate_advisory_pending" if task else "review_gate_advisory_no_task"
    outcome = "blocked" if mode == "enforce" else "clear"
    return {"outcome": outcome, "reason_code": pending_reason, "task_id": task_id, "verdict": verdict}


def _guard(
    code: str,
    from_state: str | None,
    to_state: str,
    reason_code: str,
    *,
    asof: str,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Pure guard evaluation. Returns {"ok": bool, "reason": str, "override": dict|None}.
    `override` lets the review gate redirect a requested `confirmed` move into
    a `watching` downgrade instead of a hard rejection."""
    if to_state == "dropped":
        if from_state == "dropped":
            return {"ok": False, "reason": "already dropped", "override": None}
        return {"ok": True, "reason": "drop_from_any_state", "override": None}

    if from_state is None:
        if to_state != "screened":
            return {"ok": False, "reason": "first transition must be screened", "override": None}
        return {"ok": True, "reason": "initial_screen", "override": None}

    if from_state == "dropped":
        return {"ok": False, "reason": "dropped is terminal", "override": None}

    if to_state not in ALLOWED_EDGES.get(from_state, set()):
        return {
            "ok": False,
            "reason": f"illegal edge {from_state} -> {to_state}",
            "override": None,
        }

    if from_state == "candidate" and to_state == "confirmed":
        review_gate = config.get("review_gate") or DEFAULT_REVIEW_GATE
        check = _review_gate_check(code, asof, review_gate)
        if check["outcome"] == "downgrade":
            return {
                "ok": False,
                "reason": "research verdict rejected, downgraded to watching",
                "override": {"to_state": "watching", "reason_code": check["reason_code"]},
            }
        if check["outcome"] == "blocked":
            return {
                "ok": False,
                "reason": f"review gate blocked ({check['reason_code']})",
                "override": None,
            }
        # clear: fall through to acceptance below; caller-provided reason_code
        # is preserved unless it doesn't reflect the gate outcome.
    return {"ok": True, "reason": "guard_passed", "override": None}


def _effective_move(guard: Mapping[str, Any], to_state: str, reason_code: str) -> tuple[str | None, str | None]:
    """Resolve what actually gets written given a guard verdict: the
    requested move, an overridden (downgraded) move, or nothing."""
    if guard["ok"]:
        return to_state, reason_code
    if guard["override"]:
        return guard["override"]["to_state"], guard["override"]["reason_code"]
    return None, None


def transition(
    code: str,
    to_state: str,
    reason_code: str,
    evidence_ref: Mapping[str, Any] | None = None,
    *,
    asof: str,
    from_state: str | None = None,
    config: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """The single entry point for advancing, downgrading, or dropping a
    candidate. Always writes an event (accepted or rejected) to the daily
    transition log. Returns the recorded event.
    """
    code = _code(code)
    if to_state not in STATES:
        raise ValueError(f"unknown state: {to_state}")
    if reason_code not in REASON_CODES:
        raise ValueError(f"unknown reason_code: {reason_code}")

    config = config or load_fsm_config()
    if from_state is None:
        prior = current_state(code)
        from_state = prior.get("to_state") if prior else None

    guard = _guard(code, from_state, to_state, reason_code, asof=asof, config=config)
    effective_to, effective_reason = _effective_move(guard, to_state, reason_code)
    accepted = effective_to is not None

    event = {
        "code": code,
        "asof": asof,
        "at": _now_text(now),
        "requested_to_state": to_state,
        "requested_reason_code": reason_code,
        "from_state": from_state,
        "to_state": effective_to if accepted else from_state,
        "reason_code": effective_reason if accepted else reason_code,
        "accepted": accepted,
        "guard_message": guard["reason"],
        "overridden": bool(guard["override"]) if not guard["ok"] else False,
        "evidence_ref": dict(evidence_ref or {}),
    }
    _append_event(asof, event)
    return event


def history(code: str, *, days: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Replay every recorded transition attempt (accepted and rejected) for
    one ticker across all known transition-log days, oldest first."""
    import glob

    code = _code(code)
    candidate_days = list(days) if days is not None else sorted(
        os.path.splitext(os.path.basename(path))[0]
        for path in glob.glob(data_file("stock-triage", os.path.join("candidate_fsm", "*.json")))
    )
    events: list[dict[str, Any]] = []
    for day in candidate_days:
        events.extend(
            event for event in load_day(day).get("transitions", [])
            if _code(event.get("code")) == code
        )
    events.sort(key=lambda item: (item.get("asof", ""), item.get("at", "")))
    return events


def sweep_stale_watch(
    asof: str,
    watching_codes: Mapping[str, str],
    *,
    config: dict[str, Any] | None = None,
    now: str | None = None,
) -> list[dict[str, Any]]:
    """Drop any `watching` candidate whose last accepted event is older than
    `timeouts.watching_stale_days` trading days. `watching_codes` maps
    code -> the trading date it was first seen in watching state (used only
    as a fallback when no accepted history exists yet); the authoritative
    "last seen" comes from the FSM transition log itself.
    """
    config = config or load_fsm_config()
    threshold_days = int((config.get("timeouts") or {}).get("watching_stale_days") or DEFAULT_TIMEOUTS["watching_stale_days"])
    events: list[dict[str, Any]] = []
    for code in watching_codes:
        last = current_state(code)
        if not last or last.get("to_state") != "watching":
            continue
        age_days = _trading_days_between(last.get("asof"), asof)
        if age_days is None or age_days < threshold_days:
            continue
        events.append(
            transition(
                code,
                "dropped",
                "stale_watch",
                {"last_seen_asof": last.get("asof"), "age_days": age_days},
                asof=asof,
                from_state="watching",
                config=config,
                now=now,
            )
        )
    return events


def sweep_confirm_stall(
    asof: str,
    confirmed_codes: Mapping[str, str],
    *,
    config: dict[str, Any] | None = None,
    now: str | None = None,
) -> list[dict[str, Any]]:
    """Downgrade any `confirmed` candidate that has not been filled within
    `timeouts.confirmed_stall_days` trading days back to `candidate`.
    """
    config = config or load_fsm_config()
    threshold_days = int((config.get("timeouts") or {}).get("confirmed_stall_days") or DEFAULT_TIMEOUTS["confirmed_stall_days"])
    events: list[dict[str, Any]] = []
    for code in confirmed_codes:
        last = current_state(code)
        if not last or last.get("to_state") != "confirmed":
            continue
        age_days = _trading_days_between(last.get("asof"), asof)
        if age_days is None or age_days < threshold_days:
            continue
        events.append(
            transition(
                code,
                "candidate",
                "confirm_stall",
                {"last_seen_asof": last.get("asof"), "age_days": age_days},
                asof=asof,
                from_state="confirmed",
                config=config,
                now=now,
            )
        )
    return events


def _trading_days_between(start: str | None, end: str | None) -> int | None:
    """Count trading days strictly between `start` and `end` (exclusive of
    start, inclusive of end) by walking the calendar forward. Falls back to
    raw calendar-day distance if the trading calendar does not cover the
    range, consistent with the repo's fail-closed philosophy (age is never
    silently treated as zero)."""
    if not start or not end:
        return None
    try:
        from a_share_rules import next_trading_day

        start_date = date.fromisoformat(str(start)[:10])
        end_date = date.fromisoformat(str(end)[:10])
        if end_date <= start_date:
            return 0
        count = 0
        current = start_date
        for _ in range(60):
            current = next_trading_day(current)
            count += 1
            if current >= end_date:
                break
        return count
    except Exception:  # noqa: BLE001
        try:
            return (date.fromisoformat(str(end)[:10]) - date.fromisoformat(str(start)[:10])).days
        except ValueError:
            return None


def _cli_history(args: argparse.Namespace) -> None:
    events = history(args.code)
    print(json.dumps(events, ensure_ascii=False, indent=2, default=str))


def _cli_transition(args: argparse.Namespace) -> None:
    event = transition(
        args.code,
        args.to_state,
        args.reason_code,
        json.loads(args.evidence_ref) if args.evidence_ref else None,
        asof=args.asof,
    )
    print(json.dumps(event, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description="Candidate FSM CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    history_parser = sub.add_parser("history", help="Replay a ticker's full transition history")
    history_parser.add_argument("code")
    history_parser.set_defaults(func=_cli_history)

    transition_parser = sub.add_parser("transition", help="Attempt a state transition")
    transition_parser.add_argument("code")
    transition_parser.add_argument("to_state", choices=STATES)
    transition_parser.add_argument("reason_code", choices=sorted(REASON_CODES))
    transition_parser.add_argument("--asof", default=date.today().isoformat())
    transition_parser.add_argument("--evidence-ref", default=None)
    transition_parser.set_defaults(func=_cli_transition)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(__file__))
    main()
