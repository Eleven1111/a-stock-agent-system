"""Direct Feishu chat push for pure-notification cron jobs.

Bypasses the Hermes/OpenClaw agent turn entirely so routine notification
content (capital flow, event calendar, policy watch, news monitor) never
enters the LLM context window.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Mapping

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


def render_delivery_text(job_id: str, stdout: str, max_chars: int) -> str:
    """Render job stdout as human-readable Feishu text.

    Jobs that emit `--json` (e.g. official-policy-watch, event-calendar) put a
    full JSON dump on stdout; pushing that raw would leak internal structure.
    When stdout parses as a JSON object we render a compact digest (top-level
    message string, signal/event lines, summary counters). Non-JSON output
    (e.g. capital-flow's markdown report) passes through unchanged.
    """
    raw = str(stdout or "")
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return raw[:max_chars]
    if not isinstance(parsed, Mapping):
        return raw[:max_chars]

    if parsed.get("schema") == "capital_flow_v2":
        northbound = parsed.get("northbound") or {}
        net_flow = northbound.get("net_flow_yi")
        net_text = "NA" if net_flow is None else f"{float(net_flow):+.1f}亿"
        lines = [
            f"资金流向：北向{net_text}；"
            f"跟踪股{len(parsed.get('stocks') or [])}；"
            f"板块{len(parsed.get('sectors') or [])}"
        ]
        alerts = parsed.get("alerts") or []
        if alerts:
            lines.append("资金异动：")
            for alert in alerts[:10]:
                if isinstance(alert, Mapping):
                    lines.append(f"- {alert.get('level') or '⚠️'} {alert.get('msg') or alert.get('message') or ''}".rstrip())
                else:
                    lines.append(f"- {alert}")
            if len(alerts) > 10:
                lines.append(f"... 共 {len(alerts)} 条")
        return "\n".join(lines)[:max_chars]

    lines: list[str] = []
    message = parsed.get("message")
    if isinstance(message, str) and message.strip():
        lines.append(message.strip())
    # Most notification jobs use a plain string summary.  The old renderer
    # only handled summary mappings, so an otherwise useful payload such as
    # {"summary":"资金流正常", "alerts":[]} fell through to raw JSON.
    summary = parsed.get("summary")
    if isinstance(summary, str) and summary.strip():
        lines.append(summary.strip())

    for key in ("signals", "alerts", "events", "confirmations", "items", "articles", "news"):
        items = parsed.get(key)
        if not isinstance(items, list) or not items:
            continue
        for item in items[:10]:
            if isinstance(item, Mapping):
                rank = str(item.get("source_rank") or item.get("rank") or "")
                title = str(item.get("title") or item.get("name") or "")
                url = str(item.get("url") or "")
                line = f"[{rank}] {title} {url}".strip()
                lines.append(line if line else str(item)[:200])
            else:
                lines.append(str(item)[:200])
        if len(items) > 10:
            lines.append(f"... 共 {len(items)} 条")
        break

    if isinstance(summary, Mapping):
        counts = {
            str(k).replace("_count", ""): v
            for k, v in summary.items()
            if str(k).endswith("_count") and v is not None
        }
        if counts:
            lines.append(" | ".join(f"{k}={v}" for k, v in counts.items()))

    # Last-resort human rendering.  Never send a machine-readable JSON blob
    # to Feishu when the producer adds a new schema we have not seen yet.
    if not lines:
        status = parsed.get("status")
        counts = [
            f"{str(key).replace('_count', '')}={value}"
            for key, value in parsed.items()
            if str(key).endswith("_count") and value is not None
        ]
        prefix = job_id or "任务"
        if status is not None:
            prefix = f"{prefix}：{status}"
        lines.append(prefix + (" | " + " | ".join(counts) if counts else ""))
    text = "\n".join(line for line in lines if line).strip()
    return text[:max_chars]


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

    # Keep the egress boundary safe even for callers that bypass
    # render_delivery_text.  Feishu should never receive a raw artifact JSON
    # merely because a new caller forgot to use the renderer.
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, Mapping):
        text = render_delivery_text(job_id, text, 12000)

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
