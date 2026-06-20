"""Runtime identity prevents OpenClaw from silently using the wrong state root."""

from __future__ import annotations

from state_integrity import ensure_state_identity


def test_openclaw_requires_explicit_shared_state_home(tmp_path):
    result = ensure_state_identity(
        "openclaw",
        env={"HOME": str(tmp_path)},
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "explicit_state_home_required"


def test_state_identity_is_stable_and_can_be_pinned(tmp_path):
    state = tmp_path / "state"
    env = {
        "A_STOCK_STATE_HOME": str(state),
        "A_STOCK_STATE_ID": "machine-cluster-1",
    }

    first = ensure_state_identity("openclaw", env=env)
    second = ensure_state_identity("openclaw", env=env)

    assert first["status"] == "ok"
    assert first["state_id"] == "machine-cluster-1"
    assert second["state_id"] == first["state_id"]


def test_state_identity_mismatch_fails_closed(tmp_path):
    state = tmp_path / "state"
    ensure_state_identity(
        "openclaw",
        env={"A_STOCK_STATE_HOME": str(state), "A_STOCK_STATE_ID": "expected"},
    )

    result = ensure_state_identity(
        "openclaw",
        env={"A_STOCK_STATE_HOME": str(state), "A_STOCK_STATE_ID": "wrong"},
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "state_identity_mismatch"
