"""state_doctor split-brain detection: report divergent minted identities."""

from __future__ import annotations

import json
import os

from scripts import state_doctor


def _write_identity(root, state_id, *, created_at="2026-07-01T00:00:00+00:00"):
    os.makedirs(root, exist_ok=True)
    payload = {
        "schema": "a_stock_state_identity_v1",
        "state_id": state_id,
        "created_at": created_at,
        "initial_root": str(root),
    }
    with open(os.path.join(root, "state_identity.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def test_split_brain_detected_across_two_homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    hermes = home / ".hermes"
    prod = home / ".a-stock-agent-cc"
    _write_identity(hermes, "hermes-id")
    _write_identity(prod, "prod-id")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(hermes))
    # Restrict the scan to the two seeded homes so the machine's real
    # repo/home identities do not perturb the assertion.
    monkeypatch.setattr(
        state_doctor,
        "_identity_candidate_roots",
        lambda: [os.path.abspath(str(hermes)), os.path.abspath(str(prod))],
    )

    report = state_doctor.detect_split_brain()

    assert report["detected"] is True
    assert set(report["distinct_state_ids"]) == {"hermes-id", "prod-id"}
    roots = {entry["state_id"]: entry["root"] for entry in report["identities"]}
    assert roots["hermes-id"] == os.path.abspath(str(hermes))
    assert roots["prod-id"] == os.path.abspath(str(prod))


def test_single_identity_is_not_split_brain(tmp_path, monkeypatch):
    home = tmp_path / "home"
    hermes = home / ".hermes"
    _write_identity(hermes, "only-id")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(hermes))
    monkeypatch.setattr(
        state_doctor,
        "_identity_candidate_roots",
        lambda: [os.path.abspath(str(hermes))],
    )

    report = state_doctor.detect_split_brain()

    assert report["detected"] is False
    assert report["distinct_state_ids"] == ["only-id"]


def test_inspect_state_marks_degraded_on_split_brain(tmp_path, monkeypatch):
    home = tmp_path / "home"
    hermes = home / ".hermes"
    prod = home / ".a-stock-agent-cc"
    _write_identity(hermes, "hermes-id")
    _write_identity(prod, "prod-id")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(hermes))
    monkeypatch.delenv("A_STOCK_STATE_ID", raising=False)
    monkeypatch.delenv("A_STOCK_REQUIRE_EXPLICIT_STATE_HOME", raising=False)
    monkeypatch.setattr(
        state_doctor,
        "_identity_candidate_roots",
        lambda: [os.path.abspath(str(hermes)), os.path.abspath(str(prod))],
    )

    report = state_doctor.inspect_state("hermes")

    assert report["split_brain"]["detected"] is True
    assert report["status"] == "degraded"
