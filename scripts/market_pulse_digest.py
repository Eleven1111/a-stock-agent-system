#!/usr/bin/env python3
"""Short market-pulse digest for OpenClaw/Hermes cron.

This replaces prompt-heavy market-pulse cron jobs with one bounded Serper.dev
query and deterministic text compression. No model call, no multi-page fetch.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

from a_stock_http import load_hermes_env  # noqa: E402
from data_provider import fetch_serper_news  # noqa: E402
from data_provider import _next_serper_key as _serper_key  # noqa: E402
from http_client import DataSourceError  # noqa: E402


PROFILES = {
    "midday": {
        "label": "午后市场脉冲",
        "query": "A股 午后 盘中 板块 异动 风险 最新",
    },
    "close": {
        "label": "收盘市场脉冲",
        "query": "A股 收盘 板块 异动 资金 风险 最新",
    },
}


def _clip(text: str, max_chars: int) -> str:
    limit = max(20, int(max_chars))
    compact = " ".join(str(text or "").split())
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"


def summarize_events(events: list[dict[str, Any]], *, label: str, max_chars: int) -> str:
    if not events:
        return ""
    titles = []
    for item in events[:3]:
        title = str(item.get("title") or "").strip()
        if title:
            titles.append(title)
    if not titles:
        return ""
    return _clip(f"{label}：" + "；".join(titles), max_chars)


def run_pulse(
    *,
    profile: str,
    limit: int = 3,
    max_chars: int = 200,
    now: datetime | None = None,
) -> dict[str, Any]:
    load_hermes_env()
    current = now or datetime.now()
    selected = PROFILES.get(profile)
    if selected is None:
        return {
            "schema": "market_pulse_digest_v1",
            "status": "invalid_profile",
            "profile": profile,
            "generated_at": current.isoformat(timespec="seconds"),
            "signals": [],
        }

    api_key = _serper_key()
    if not api_key:
        return {
            "schema": "market_pulse_digest_v1",
            "status": "insufficient_data",
            "profile": profile,
            "generated_at": current.isoformat(timespec="seconds"),
            "message": "SERPER_API_KEY missing; no market pulse judgement",
            "events": [],
            "signals": [],
        }

    try:
        result = fetch_serper_news(selected["query"], api_key, max(1, int(limit)))
        events = result.data if hasattr(result, "data") else []
    except DataSourceError as exc:
        return {
            "schema": "market_pulse_digest_v1",
            "status": "insufficient_data",
            "profile": profile,
            "generated_at": current.isoformat(timespec="seconds"),
            "message": str(exc),
            "events": [],
            "signals": [],
        }

    summary = summarize_events(events, label=selected["label"], max_chars=max_chars)
    status = "ready" if summary else "no_signal"
    return {
        "schema": "market_pulse_digest_v1",
        "status": status,
        "profile": profile,
        "generated_at": current.isoformat(timespec="seconds"),
        "query": selected["query"],
        "summary": summary,
        "events": events,
        "events_count": len(events),
        "signals": events if status == "ready" else [],
        "signal_count": len(events) if status == "ready" else 0,
        "max_chars": max_chars,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded market pulse digest")
    parser.add_argument("--profile", choices=sorted(PROFILES), required=True)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--max-chars", type=int, default=200)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run_pulse(
        profile=args.profile,
        limit=args.limit,
        max_chars=args.max_chars,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result.get("summary"):
        print(result["summary"])


if __name__ == "__main__":
    main()
