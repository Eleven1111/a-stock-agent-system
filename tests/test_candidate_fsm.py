"""Candidate FSM: transition guards, review gate, timeout sweeps, replay."""

from __future__ import annotations

import json

import pytest

import candidate_fsm as fsm
import research_bus


def _config(mode: str = "advisory", watching_stale_days: int = 5, confirmed_stall_days: int = 3):
    return {
        "review_gate": {"mode": mode, "task_kind": "candidate_deep_dive"},
        "timeouts": {
            "watching_stale_days": watching_stale_days,
            "confirmed_stall_days": confirmed_stall_days,
        },
    }


def _seed_research_task(code: str, trading_date: str, verdict: str | None, *, status: str | None = None):
    """Fabricate a research_bus task record with a given verdict, bypassing
    the full claim/submit/synthesize flow since only find_task is read."""
    task_id = research_bus.make_task_id("candidate_deep_dive", research_bus.subject_key({"code": code}), trading_date)

    def _mutate(value):
        tasks = list(value) if isinstance(value, list) else []
        tasks.append({
            "schema": research_bus.TASK_SCHEMA,
            "id": task_id,
            "kind": "candidate_deep_dive",
            "subject": {"code": code},
            "subject_key": research_bus.subject_key({"code": code}),
            "trading_date": trading_date,
            "status": status or ("done" if verdict in {"advance", "watch", "abstained"} else "rejected"),
            "verdict": verdict,
            "roles": {},
            "expert_plan": [],
        })
        return tasks

    from state_store import mutate_json
    mutate_json(research_bus.queue_file(), _mutate, [])
    return task_id


# ---- basic legal transitions ----------------------------------------------


def test_initial_transition_must_be_screened(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    event = fsm.transition(
        "600001", "watching", "score_above_threshold", asof="2026-07-01",
        config=_config(),
    )
    assert event["accepted"] is False
    assert event["from_state"] is None
    assert event["requested_to_state"] == "watching"


def test_full_forward_path_screened_to_confirmed(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    code = "600001"
    e1 = fsm.transition(code, "screened", "score_above_threshold", asof="2026-07-01", config=_config())
    assert e1["accepted"] is True and e1["to_state"] == "screened"

    e2 = fsm.transition(code, "watching", "score_above_threshold", asof="2026-07-01", config=_config())
    assert e2["accepted"] is True and e2["to_state"] == "watching"

    e3 = fsm.transition(code, "candidate", "score_above_threshold", asof="2026-07-02", config=_config())
    assert e3["accepted"] is True and e3["to_state"] == "candidate"

    # advisory mode + no research task -> still clears (default posture)
    e4 = fsm.transition(code, "confirmed", "manual_promote", asof="2026-07-02", config=_config("advisory"))
    assert e4["accepted"] is True and e4["to_state"] == "confirmed"

    assert fsm.current_state(code)["to_state"] == "confirmed"


def test_illegal_edge_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    code = "600001"
    fsm.transition(code, "screened", "score_above_threshold", asof="2026-07-01", config=_config())
    # screened -> candidate skips watching: illegal
    event = fsm.transition(code, "candidate", "score_above_threshold", asof="2026-07-01", config=_config())
    assert event["accepted"] is False
    assert "illegal edge" in event["guard_message"]
    assert fsm.current_state(code)["to_state"] == "screened"


def test_dropped_is_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    code = "600001"
    fsm.transition(code, "screened", "score_above_threshold", asof="2026-07-01", config=_config())
    fsm.transition(code, "dropped", "manual_drop", asof="2026-07-01", config=_config())
    again = fsm.transition(code, "watching", "score_above_threshold", asof="2026-07-02", config=_config())
    assert again["accepted"] is False
    assert "terminal" in again["guard_message"]


def test_drop_allowed_from_any_live_state(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    code = "600001"
    fsm.transition(code, "screened", "score_above_threshold", asof="2026-07-01", config=_config())
    fsm.transition(code, "watching", "score_above_threshold", asof="2026-07-01", config=_config())
    event = fsm.transition(code, "dropped", "theme_fading", asof="2026-07-02", config=_config())
    assert event["accepted"] is True
    assert event["to_state"] == "dropped"


def test_unknown_reason_code_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    with pytest.raises(ValueError):
        fsm.transition("600001", "screened", "not_a_real_reason", asof="2026-07-01", config=_config())


def test_unknown_state_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    with pytest.raises(ValueError):
        fsm.transition("600001", "not_a_state", "manual_drop", asof="2026-07-01", config=_config())


# ---- review gate: off / advisory / enforce ---------------------------------


def test_review_gate_off_ignores_verdict(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    code = "600001"
    _seed_research_task(code, "2026-07-02", verdict="rejected")
    fsm.transition(code, "screened", "score_above_threshold", asof="2026-07-01", config=_config("off"))
    fsm.transition(code, "watching", "score_above_threshold", asof="2026-07-01", config=_config("off"))
    fsm.transition(code, "candidate", "score_above_threshold", asof="2026-07-02", config=_config("off"))
    event = fsm.transition(code, "confirmed", "manual_promote", asof="2026-07-02", config=_config("off"))
    assert event["accepted"] is True
    assert event["reason_code"] == "manual_promote"


def test_review_gate_advisory_rejected_verdict_downgrades_to_watching(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    code = "600001"
    _seed_research_task(code, "2026-07-02", verdict="rejected", status="rejected")
    fsm.transition(code, "screened", "score_above_threshold", asof="2026-07-01", config=_config("advisory"))
    fsm.transition(code, "watching", "score_above_threshold", asof="2026-07-01", config=_config("advisory"))
    fsm.transition(code, "candidate", "score_above_threshold", asof="2026-07-02", config=_config("advisory"))
    event = fsm.transition(code, "confirmed", "manual_promote", asof="2026-07-02", config=_config("advisory"))
    assert event["accepted"] is True
    assert event["to_state"] == "watching"
    assert event["reason_code"] == "research_rejected"
    assert event["overridden"] is True
    assert event["requested_to_state"] == "confirmed"
    assert fsm.current_state(code)["to_state"] == "watching"


def test_review_gate_advisory_disputed_verdict_does_not_block(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    code = "600001"
    _seed_research_task(code, "2026-07-02", verdict="disputed", status="done")
    fsm.transition(code, "screened", "score_above_threshold", asof="2026-07-01", config=_config("advisory"))
    fsm.transition(code, "watching", "score_above_threshold", asof="2026-07-01", config=_config("advisory"))
    fsm.transition(code, "candidate", "score_above_threshold", asof="2026-07-02", config=_config("advisory"))
    event = fsm.transition(code, "confirmed", "manual_promote", asof="2026-07-02", config=_config("advisory"))
    assert event["accepted"] is True
    assert event["to_state"] == "confirmed"


def test_review_gate_advisory_no_task_does_not_block(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    code = "600001"
    fsm.transition(code, "screened", "score_above_threshold", asof="2026-07-01", config=_config("advisory"))
    fsm.transition(code, "watching", "score_above_threshold", asof="2026-07-01", config=_config("advisory"))
    fsm.transition(code, "candidate", "score_above_threshold", asof="2026-07-02", config=_config("advisory"))
    event = fsm.transition(code, "confirmed", "manual_promote", asof="2026-07-02", config=_config("advisory"))
    assert event["accepted"] is True


def test_review_gate_enforce_rejected_verdict_downgrades(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    code = "600001"
    _seed_research_task(code, "2026-07-02", verdict="rejected", status="rejected")
    fsm.transition(code, "screened", "score_above_threshold", asof="2026-07-01", config=_config("enforce"))
    fsm.transition(code, "watching", "score_above_threshold", asof="2026-07-01", config=_config("enforce"))
    fsm.transition(code, "candidate", "score_above_threshold", asof="2026-07-02", config=_config("enforce"))
    event = fsm.transition(code, "confirmed", "manual_promote", asof="2026-07-02", config=_config("enforce"))
    assert event["accepted"] is True
    assert event["to_state"] == "watching"
    assert event["reason_code"] == "research_rejected"


def test_review_gate_enforce_disputed_verdict_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    code = "600001"
    _seed_research_task(code, "2026-07-02", verdict="disputed", status="done")
    fsm.transition(code, "screened", "score_above_threshold", asof="2026-07-01", config=_config("enforce"))
    fsm.transition(code, "watching", "score_above_threshold", asof="2026-07-01", config=_config("enforce"))
    fsm.transition(code, "candidate", "score_above_threshold", asof="2026-07-02", config=_config("enforce"))
    event = fsm.transition(code, "confirmed", "manual_promote", asof="2026-07-02", config=_config("enforce"))
    assert event["accepted"] is False
    assert event["to_state"] == "candidate"
    assert event["reason_code"] == "manual_promote"
    assert "research_disputed" in event["guard_message"]


def test_review_gate_enforce_no_task_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    code = "600001"
    fsm.transition(code, "screened", "score_above_threshold", asof="2026-07-01", config=_config("enforce"))
    fsm.transition(code, "watching", "score_above_threshold", asof="2026-07-01", config=_config("enforce"))
    fsm.transition(code, "candidate", "score_above_threshold", asof="2026-07-02", config=_config("enforce"))
    event = fsm.transition(code, "confirmed", "manual_promote", asof="2026-07-02", config=_config("enforce"))
    assert event["accepted"] is False
    assert event["to_state"] == "candidate"
    assert event["reason_code"] == "manual_promote"
    assert "review_gate_advisory_no_task" in event["guard_message"]


def test_review_gate_advance_verdict_clears(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    code = "600001"
    _seed_research_task(code, "2026-07-02", verdict="advance", status="done")
    fsm.transition(code, "screened", "score_above_threshold", asof="2026-07-01", config=_config("enforce"))
    fsm.transition(code, "watching", "score_above_threshold", asof="2026-07-01", config=_config("enforce"))
    fsm.transition(code, "candidate", "score_above_threshold", asof="2026-07-02", config=_config("enforce"))
    event = fsm.transition(code, "confirmed", "manual_promote", asof="2026-07-02", config=_config("enforce"))
    assert event["accepted"] is True
    assert event["to_state"] == "confirmed"


# ---- timeout sweeps ---------------------------------------------------------


def test_sweep_stale_watch_drops_after_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    code = "600001"
    fsm.transition(code, "screened", "score_above_threshold", asof="2026-06-01", config=_config())
    fsm.transition(code, "watching", "score_above_threshold", asof="2026-06-01", config=_config())

    events = fsm.sweep_stale_watch(
        "2026-06-15", {code: "2026-06-01"}, config=_config(watching_stale_days=5),
    )
    assert len(events) == 1
    assert events[0]["accepted"] is True
    assert events[0]["to_state"] == "dropped"
    assert events[0]["reason_code"] == "stale_watch"
    assert fsm.current_state(code)["to_state"] == "dropped"


def test_sweep_stale_watch_skips_recent(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    code = "600001"
    fsm.transition(code, "screened", "score_above_threshold", asof="2026-06-10", config=_config())
    fsm.transition(code, "watching", "score_above_threshold", asof="2026-06-10", config=_config())

    events = fsm.sweep_stale_watch(
        "2026-06-11", {code: "2026-06-10"}, config=_config(watching_stale_days=5),
    )
    assert events == []
    assert fsm.current_state(code)["to_state"] == "watching"


def test_sweep_stale_watch_ignores_non_watching_codes(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    code = "600001"
    fsm.transition(code, "screened", "score_above_threshold", asof="2026-06-01", config=_config())
    fsm.transition(code, "watching", "score_above_threshold", asof="2026-06-01", config=_config())
    fsm.transition(code, "candidate", "score_above_threshold", asof="2026-06-02", config=_config())

    events = fsm.sweep_stale_watch(
        "2026-06-20", {code: "2026-06-01"}, config=_config(watching_stale_days=5),
    )
    assert events == []
    assert fsm.current_state(code)["to_state"] == "candidate"


def test_sweep_confirm_stall_downgrades_after_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    code = "600001"
    fsm.transition(code, "screened", "score_above_threshold", asof="2026-06-01", config=_config())
    fsm.transition(code, "watching", "score_above_threshold", asof="2026-06-01", config=_config())
    fsm.transition(code, "candidate", "score_above_threshold", asof="2026-06-02", config=_config())
    fsm.transition(code, "confirmed", "manual_promote", asof="2026-06-02", config=_config())

    events = fsm.sweep_confirm_stall(
        "2026-06-10", {code: "2026-06-02"}, config=_config(confirmed_stall_days=3),
    )
    assert len(events) == 1
    assert events[0]["accepted"] is True
    assert events[0]["to_state"] == "candidate"
    assert events[0]["reason_code"] == "confirm_stall"
    assert fsm.current_state(code)["to_state"] == "candidate"


def test_sweep_confirm_stall_skips_recent(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    code = "600001"
    fsm.transition(code, "screened", "score_above_threshold", asof="2026-06-01", config=_config())
    fsm.transition(code, "watching", "score_above_threshold", asof="2026-06-01", config=_config())
    fsm.transition(code, "candidate", "score_above_threshold", asof="2026-06-02", config=_config())
    fsm.transition(code, "confirmed", "manual_promote", asof="2026-06-08", config=_config())

    events = fsm.sweep_confirm_stall(
        "2026-06-09", {code: "2026-06-08"}, config=_config(confirmed_stall_days=3),
    )
    assert events == []


# ---- replay -----------------------------------------------------------------


def test_history_replays_full_path_including_rejections(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    code = "600001"
    fsm.transition(code, "screened", "score_above_threshold", asof="2026-07-01", config=_config())
    fsm.transition(code, "watching", "score_above_threshold", asof="2026-07-01", config=_config())
    # illegal, will be rejected but logged
    fsm.transition(code, "confirmed", "manual_promote", asof="2026-07-01", config=_config())
    fsm.transition(code, "candidate", "score_above_threshold", asof="2026-07-02", config=_config())

    events = fsm.history(code)
    assert [e["requested_to_state"] for e in events] == [
        "screened", "watching", "confirmed", "candidate",
    ]
    assert [e["accepted"] for e in events] == [True, True, False, True]
    # rejected attempt did not move state
    assert events[2]["from_state"] == "watching"
    assert events[2]["to_state"] == "watching"


def test_history_empty_for_unknown_code(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    assert fsm.history("999999") == []


# ---- config loading -----------------------------------------------------------


def test_load_fsm_config_defaults_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    missing_path = str(tmp_path / "nonexistent.json")
    config = fsm.load_fsm_config(missing_path)
    assert config["review_gate"]["mode"] == "advisory"
    assert config["timeouts"]["watching_stale_days"] == 5
    assert config["timeouts"]["confirmed_stall_days"] == 3


def test_load_fsm_config_reads_real_config_file():
    config = fsm.load_fsm_config()
    assert config["review_gate"]["mode"] in {"off", "advisory", "enforce"}
    assert isinstance(config["timeouts"]["watching_stale_days"], int)


def test_load_fsm_config_rejects_invalid_mode(tmp_path):
    bad = tmp_path / "candidate_selection.json"
    bad.write_text(
        json.dumps({"candidate_fsm": {"review_gate": {"mode": "not_a_mode"}}}),
        encoding="utf-8",
    )
    config = fsm.load_fsm_config(str(bad))
    assert config["review_gate"]["mode"] == "advisory"
