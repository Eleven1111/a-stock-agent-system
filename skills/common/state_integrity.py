"""Runtime state-root identity checks shared by Hermes and OpenClaw."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Mapping

from state_store import mutate_json


SCHEMA = "a_stock_state_identity_v1"


def _state_root(values: Mapping[str, str]) -> str:
    configured = values.get("A_STOCK_STATE_HOME") or values.get("HERMES_HOME")
    if configured:
        return os.path.abspath(os.path.expanduser(str(configured)))
    home = values.get("HOME") or os.path.expanduser("~")
    return os.path.abspath(os.path.join(str(home), ".hermes"))


def ensure_state_identity(
    runtime: str,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    values = env if env is not None else os.environ
    root = _state_root(values)
    if runtime == "openclaw" and not values.get("A_STOCK_STATE_HOME"):
        return {
            "status": "blocked",
            "reason": "explicit_state_home_required",
            "state_root": root,
        }

    identity_path = os.path.join(root, "state_identity.json")
    expected = str(values.get("A_STOCK_STATE_ID") or "").strip()
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def initialize(current):
        if isinstance(current, dict) and current.get("schema") == SCHEMA:
            return current
        return {
            "schema": SCHEMA,
            "state_id": expected or str(uuid.uuid4()),
            "created_at": created_at,
            "initial_root": root,
        }

    identity = mutate_json(identity_path, initialize, default=None)
    actual = str(identity.get("state_id") or "")
    if expected and actual != expected:
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
