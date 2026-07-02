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
LARK_CLI = "lark-cli"


def target_chat_id() -> str | None:
    value = os.environ.get(CHAT_ID_ENV, "").strip()
    return value or None


def push_text(job_id: str, text: str, *, timeout_seconds: int = 15) -> dict[str, Any]:
    """Send text directly to the configured Feishu chat via lark-cli.

    Never raises: callers run inside cron jobs and a push failure must not
    block the underlying job's artifact/ledger writes.
    """
    chat_id = target_chat_id()
    if not chat_id:
        return {"status": "not_configured", "job_id": job_id}
    if not text.strip():
        return {"status": "empty", "job_id": job_id}

    try:
        completed = subprocess.run(
            [
                LARK_CLI, "im", "+messages-send",
                "--chat-id", chat_id,
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
