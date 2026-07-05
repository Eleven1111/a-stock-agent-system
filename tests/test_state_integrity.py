"""Runtime identity prevents OpenClaw from silently using the wrong state root."""

from __future__ import annotations

import json
import os
import shutil

from state_integrity import ensure_state_identity


def test_openclaw_requires_explicit_shared_state_home(tmp_path):
    result = ensure_state_identity(
        "openclaw",
        env={"HOME": str(tmp_path)},
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "explicit_state_home_required"


def _bootstrap_identity(state, state_id):
    """Mint an identity the way a first bootstrap run (no expected id) would."""
    bootstrap = ensure_state_identity(
        "openclaw",
        env={"A_STOCK_STATE_HOME": str(state)},
    )
    assert bootstrap["status"] == "ok"
    identity_path = state / "state_identity.json"
    payload = json.loads(identity_path.read_text(encoding="utf-8"))
    payload["state_id"] = state_id
    identity_path.write_text(json.dumps(payload), encoding="utf-8")
    return state_id


def test_state_identity_is_stable_and_can_be_pinned(tmp_path):
    state = tmp_path / "state"
    _bootstrap_identity(state, "machine-cluster-1")
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
    _bootstrap_identity(state, "expected")

    result = ensure_state_identity(
        "openclaw",
        env={"A_STOCK_STATE_HOME": str(state), "A_STOCK_STATE_ID": "wrong"},
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "state_identity_mismatch"


def test_expected_id_with_missing_identity_blocks_without_minting(tmp_path):
    state = tmp_path / "state"
    env = {"A_STOCK_STATE_HOME": str(state), "A_STOCK_STATE_ID": "prod-home"}

    result = ensure_state_identity("hermes", env=env)

    assert result["status"] == "blocked"
    assert result["reason"] == "state_identity_missing"
    # Fail-closed: never self-heal by writing a fresh identity into the wrong home.
    assert not (state / "state_identity.json").exists()


def test_expected_id_with_corrupt_identity_blocks_without_minting(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "state_identity.json").write_text("{ not json", encoding="utf-8")
    env = {"A_STOCK_STATE_HOME": str(state), "A_STOCK_STATE_ID": "prod-home"}

    result = ensure_state_identity("hermes", env=env)

    assert result["status"] == "blocked"
    assert result["reason"] == "state_identity_missing"


def test_expected_id_matching_identity_is_ok(tmp_path):
    state = tmp_path / "state"
    _bootstrap_identity(state, "prod-home")
    env = {"A_STOCK_STATE_HOME": str(state), "A_STOCK_STATE_ID": "prod-home"}

    result = ensure_state_identity("hermes", env=env)

    assert result["status"] == "ok"
    assert result["state_id"] == "prod-home"


def test_require_explicit_home_blocks_any_runtime(tmp_path):
    for runtime in ("hermes", "local"):
        result = ensure_state_identity(
            runtime,
            env={
                "HOME": str(tmp_path),
                "A_STOCK_REQUIRE_EXPLICIT_STATE_HOME": "1",
            },
        )
        assert result["status"] == "blocked", runtime
        assert result["reason"] == "explicit_state_home_required", runtime


def test_require_explicit_home_allows_when_home_set(tmp_path):
    state = tmp_path / "state"
    result = ensure_state_identity(
        "hermes",
        env={
            "A_STOCK_STATE_HOME": str(state),
            "A_STOCK_REQUIRE_EXPLICIT_STATE_HOME": "true",
        },
    )
    assert result["status"] == "ok"


def test_state_root_inside_repo_is_blocked():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    result = ensure_state_identity(
        "hermes",
        env={"A_STOCK_STATE_HOME": repo_root},
    )
    assert result["status"] == "blocked"
    assert result["reason"] == "state_root_inside_repo"


def test_state_root_inside_repo_escape_hatch():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    inside = os.path.join(repo_root, ".pytest-repo-state")
    try:
        result = ensure_state_identity(
            "hermes",
            env={
                "A_STOCK_STATE_HOME": inside,
                "A_STOCK_ALLOW_REPO_STATE": "1",
            },
        )
        # Escape hatch lets the check pass; identity resolution proceeds normally.
        assert result["status"] == "ok"
    finally:
        shutil.rmtree(inside, ignore_errors=True)
