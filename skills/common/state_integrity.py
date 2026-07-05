"""Runtime state-root identity checks shared by Hermes and OpenClaw."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Mapping

from state_store import mutate_json, read_json


SCHEMA = "a_stock_state_identity_v1"

# Repository working tree root (skills/common/state_integrity.py -> repo root).
# Derived from the module location like research_bus._REPO_ROOT; no git calls.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _state_root(values: Mapping[str, str]) -> str:
    configured = values.get("A_STOCK_STATE_HOME") or values.get("HERMES_HOME")
    if configured:
        return os.path.abspath(os.path.expanduser(str(configured)))
    home = values.get("HOME") or os.path.expanduser("~")
    return os.path.abspath(os.path.join(str(home), ".hermes"))


def _is_inside_repo(root: str) -> bool:
    repo = os.path.realpath(_REPO_ROOT)
    resolved = os.path.realpath(root)
    if resolved == repo:
        return True
    return resolved.startswith(repo + os.sep)


def ensure_state_identity(
    runtime: str,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    values = env if env is not None else os.environ
    root = _state_root(values)

    require_explicit = _truthy(values.get("A_STOCK_REQUIRE_EXPLICIT_STATE_HOME"))
    explicit_home = values.get("A_STOCK_STATE_HOME")
    if (runtime == "openclaw" or require_explicit) and not explicit_home:
        return {
            "status": "blocked",
            "reason": "explicit_state_home_required",
            "state_root": root,
        }

    if _is_inside_repo(root) and not _truthy(values.get("A_STOCK_ALLOW_REPO_STATE")):
        return {
            "status": "blocked",
            "reason": "state_root_inside_repo",
            "state_root": root,
        }

    identity_path = os.path.join(root, "state_identity.json")
    expected = str(values.get("A_STOCK_STATE_ID") or "").strip()

    # Fail-closed: when an expected id is configured we never mint a new
    # identity.  A missing or corrupt identity file means we are pointed at the
    # wrong home, so we block instead of silently self-healing.
    if expected:
        current = read_json(identity_path, None)
        if not isinstance(current, dict) or current.get("schema") != SCHEMA:
            return {
                "status": "blocked",
                "reason": "state_identity_missing",
                "state_root": root,
                "expected_state_id": expected,
            }
        actual = str(current.get("state_id") or "")
        if actual != expected:
            return {
                "status": "blocked",
                "reason": "state_identity_mismatch",
                "state_root": root,
                "state_id": actual,
                "expected_state_id": expected,
            }
        return {
            "status": "ok",
            "reason": "",
            "state_root": root,
            "state_id": actual,
        }

    # Bootstrap path (no expected id configured): mint an identity if absent,
    # preserving historical behaviour.
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def initialize(current):
        if isinstance(current, dict) and current.get("schema") == SCHEMA:
            return current
        return {
            "schema": SCHEMA,
            "state_id": str(uuid.uuid4()),
            "created_at": created_at,
            "initial_root": root,
        }

    identity = mutate_json(identity_path, initialize, default=None)
    actual = str(identity.get("state_id") or "")
    return {
        "status": "ok",
        "reason": "",
        "state_root": root,
        "state_id": actual,
    }
