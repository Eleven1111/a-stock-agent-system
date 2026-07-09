"""Direct Feishu chat push for pure-notification cron jobs.

Bypasses the Hermes/OpenClaw agent turn entirely so routine notification
content (capital flow, event calendar, policy watch, news monitor) never
enters the LLM context window.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

CHAT_ID_ENV = "A_STOCK_FEISHU_CHAT_ID"
USER_ID_ENV = "A_STOCK_FEISHU_USER_ID"

# Default Feishu user for direct push (十一).
# Override via A_STOCK_FEISHU_USER_ID env var if needed.
DEFAULT_USER_ID = "ou_cc19c4f3a365cf9f7815b0a3208e7cfc"

LARK_CLI = "lark-cli"


def target_recipient() -> tuple[str, str] | None:
    chat_id = os.environ.get(CHAT_ID_ENV, "").strip()
    if chat_id:
        return "--chat-id", chat_id
    user_id = os.environ.get(USER_ID_ENV, "").strip()
    if user_id:
        return "--user-id", user_id
    # Fall back to hardcoded default (avoids cross-app open_id issues).
    if DEFAULT_USER_ID:
        return "--user-id", DEFAULT_USER_ID
    return None


def push_text(job_id: str, text: str, *, timeout_seconds: int = 15) -> dict[str, Any]:
    """Send text directly to the configured Feishu chat via lark-cli.

    Never raises: callers run inside cron jobs and a push failure must not
    block the underlying job's artifact/ledger writes.
    """
    recipient = target_recipient()
    if recipient is None:
        return {"status": "not_configured", "job_id": job_id}
    if not text.strip():
        return {"status": "empty", "job_id": job_id}

    try:
        completed = subprocess.run(
            [
                LARK_CLI, "im", "+messages-send",
                recipient[0], recipient[1],
                "--text", text,
                "--as", "bot",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "failed", "job_id": job_id, "error": str(exc)}

    if completed.returncode != 0:
        return {
            "status": "failed",
            "job_id": job_id,
            "error": completed.stderr.strip()[:500],
        }
    return {"status": "sent", "job_id": job_id}
