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

# Compliance footer appended to every outbound message (AGENTS.md: never
# represent analysis as a substitute for the user's final decision). This is
# the single Feishu egress point, so the disclosure is enforced here instead
# of relying on each script to remember to add it.
DISCLOSURE = "—— 研究信息，非投资建议 ——"


def target_chat_id() -> str | None:
    value = os.environ.get(CHAT_ID_ENV, "").strip()
    return value or None


def _with_disclosure(text: str) -> str:
    """Append DISCLOSURE as its own trailing line, idempotently.

    Callers (e.g. run_agent_dag.target_output) truncate stdout to
    max_output_chars before calling push_text purely as a flood-guard, not a
    hard protocol limit, so appending the footer here can push the final
    message slightly past that budget. That trade-off is intentional: the
    disclosure must survive truncation, and max_output_chars is not a wire
    limit that would reject an oversized message.
    """
    if DISCLOSURE in text:
        return text
    separator = "" if text.endswith("\n") else "\n"
    return f"{text}{separator}{DISCLOSURE}"


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

    text = _with_disclosure(text)

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
