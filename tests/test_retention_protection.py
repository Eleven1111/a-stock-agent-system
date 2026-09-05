"""GC must not collect evidence a running study still needs.

Recency-based protection only reads files modified inside
``reference_protection_days`` (30).  A pre-registered experiment freezes its
inputs on day zero and never touches the record again, while a 60-fitting plus
60-out-of-sample cycle runs for roughly 170 calendar days.  These tests advance
the clock past every ordinary TTL and check what survives.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from skills.common import retention_protection as holds
from skills.common import storage_retention

NOW = datetime(2026, 9, 5, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Every write in this module stays inside a temporary state root."""
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    return tmp_path / "state"


def _snapshot(home, dataset: str, captured_at: datetime, name: str):
    path = home / "market" / "snapshots" / dataset / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "schema": "market_snapshot_v1",
            "dataset": dataset,
            "captured_at": captured_at.isoformat(),
            "payload": {"rows": []},
        }),
        encoding="utf-8",
    )
    return path.resolve()


def _settings(**overrides):
    settings = {
        "snapshot_input_retention_days": 30,
        "snapshot_output_retention_days": 90,
        "cron_artifact_retention_days": 30,
        "reference_protection_days": 30,
        "gc_max_delete_files": 10000,
        "snapshot_min_keep_per_dataset": 1,
        "snapshot_max_total_mb": 1024,
        "snapshot_cold_archive_enabled": False,
    }
    settings.update(overrides)
    return settings


def test_a_hold_survives_a_clock_advanced_past_every_ordinary_ttl(_isolated_state):
    home = _isolated_state
    frozen = _snapshot(home, "oos-input", NOW - timedelta(days=170), "frozen")
    stale = _snapshot(home, "oos-input", NOW - timedelta(days=170), "stale")
    _snapshot(home, "oos-input", NOW, "recent")
    holds.place_hold(
        "experiment:rank_surprise_next_open_paper_v1:abc",
        [frozen],
        reason="active_pre_registered_experiment",
        state_home=home,
    )

    plan = storage_retention.cleanup_storage(
        state_home=home, settings=_settings(), now=NOW, apply=False, use_index=False
    )

    assert plan["mode"] == "dry_run"
    assert plan["protected"]["held_snapshots"] == 1
    assert plan["deleted"]["expired_snapshots"] == 1
    assert frozen.exists() and stale.exists()

    applied = storage_retention.cleanup_storage(
        state_home=home, settings=_settings(), now=NOW, apply=True, use_index=False
    )
    assert applied["deleted"]["expired_snapshots"] == 1
    assert frozen.exists()
    assert not stale.exists()


def test_without_a_hold_the_same_evidence_is_collected(_isolated_state):
    home = _isolated_state
    frozen = _snapshot(home, "oos-input", NOW - timedelta(days=170), "frozen")
    _snapshot(home, "oos-input", NOW, "recent")

    storage_retention.cleanup_storage(
        state_home=home, settings=_settings(), now=NOW, apply=True, use_index=False
    )

    assert not frozen.exists()


def test_a_released_hold_stops_protecting_and_leaves_its_history(_isolated_state):
    home = _isolated_state
    frozen = _snapshot(home, "oos-input", NOW - timedelta(days=170), "frozen")
    _snapshot(home, "oos-input", NOW, "recent")
    holds.place_hold("study-a", [frozen], reason="fitting_window", state_home=home)
    holds.release_hold("study-a", reason="study_concluded", state_home=home)

    assert holds.active_holds(now=NOW, state_home=home) == []
    storage_retention.cleanup_storage(
        state_home=home, settings=_settings(), now=NOW, apply=True, use_index=False
    )
    assert not frozen.exists()
    # The ledger is a history: both the hold and its release remain readable.
    kinds = [row["record_type"] for row in holds.read_ledger(home)]
    assert kinds == ["hold", "release"]


def test_an_expired_hold_stops_protecting_but_an_open_ended_one_does_not(_isolated_state):
    home = _isolated_state
    holds.place_hold(
        "study-expired", ["/tmp/a.json"], reason="r",
        expires_at=(NOW - timedelta(days=1)).isoformat(), state_home=home,
    )
    holds.place_hold("study-open", ["/tmp/b.json"], reason="r", state_home=home)

    scopes = [row["scope"] for row in holds.active_holds(now=NOW, state_home=home)]
    assert scopes == ["study-open"]


def test_a_re_placed_hold_supersedes_its_own_release(_isolated_state):
    home = _isolated_state
    holds.place_hold("study-a", ["/tmp/a.json"], reason="first", state_home=home)
    holds.release_hold("study-a", reason="done", state_home=home)
    holds.place_hold("study-a", ["/tmp/a.json"], reason="reopened", state_home=home)

    active = holds.active_holds(now=NOW, state_home=home)
    assert [row["reason"] for row in active] == ["reopened"]


def test_an_incomplete_hold_is_refused(_isolated_state):
    home = _isolated_state
    with pytest.raises(ValueError, match="retention_hold_incomplete"):
        holds.place_hold("", ["/tmp/a.json"], reason="r", state_home=home)
    with pytest.raises(ValueError, match="retention_hold_incomplete"):
        holds.place_hold("scope", [], reason="r", state_home=home)
    with pytest.raises(ValueError, match="retention_hold_incomplete"):
        holds.place_hold("scope", ["/tmp/a.json"], reason="", state_home=home)
    with pytest.raises(ValueError, match="retention_release_incomplete"):
        holds.release_hold("scope", reason="", state_home=home)


def test_the_gc_plan_names_which_studies_are_holding_what(_isolated_state):
    home = _isolated_state
    frozen = _snapshot(home, "oos-input", NOW - timedelta(days=170), "frozen")
    _snapshot(home, "oos-input", NOW, "recent")
    holds.place_hold("study-a", [frozen], reason="fitting_window", state_home=home)

    plan = storage_retention.cleanup_storage(
        state_home=home, settings=_settings(), now=NOW, apply=False, use_index=False
    )
    report = plan["protected"]["holds"]

    assert report["active_scopes"] == ["study-a"]
    assert report["reasons"] == {"study-a": "fitting_window"}
    assert report["held_reference_count"] == 1


def test_capacity_pressure_is_reported_rather_than_resolved_by_dropping_holds(
    _isolated_state,
):
    home = _isolated_state
    frozen = _snapshot(home, "oos-output", NOW - timedelta(days=5), "frozen")
    _snapshot(home, "oos-output", NOW, "recent")
    holds.place_hold("study-a", [frozen], reason="oos_window", state_home=home)

    plan = storage_retention.cleanup_storage(
        state_home=home,
        settings=_settings(snapshot_max_total_mb=0.0000001),
        now=NOW, apply=False, use_index=False,
    )

    assert plan["capacity_satisfied"] is False
    assert plan["capacity_blocked_by_holds"] is True
    assert plan["protected"]["held_snapshots"] == 1
    assert frozen.exists()


def test_a_hold_on_a_path_outside_the_snapshot_tree_changes_nothing(_isolated_state):
    home = _isolated_state
    frozen = _snapshot(home, "oos-input", NOW - timedelta(days=170), "frozen")
    _snapshot(home, "oos-input", NOW, "recent")
    holds.place_hold("study-a", [home / "elsewhere.json"], reason="r", state_home=home)

    storage_retention.cleanup_storage(
        state_home=home, settings=_settings(), now=NOW, apply=True, use_index=False
    )

    assert not frozen.exists()


def test_an_experiment_hold_is_scoped_to_its_frozen_identity(_isolated_state):
    home = _isolated_state
    row = holds.hold_experiment_evidence(
        {"experiment_id": "rank_surprise_next_open_paper_v1",
         "experiment_sha256": "0123456789abcdef" * 4},
        ["/tmp/a.json"],
        state_home=home,
    )

    assert row["scope"] == "experiment:rank_surprise_next_open_paper_v1:0123456789abcdef"
    assert row["reason"] == "active_pre_registered_experiment"
    assert row["record_sha256"]
