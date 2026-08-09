#!/usr/bin/env python3
"""
推荐反馈回流 — 人工对已开仓推荐（signal）标注有效/无效
======================================================
反馈本身作为一条新的 signal_ledger 事件追加（recommendation.feedback），不改写
历史事件；同一 signal_id 可反复反馈（更正认知），每次都是独立事实，读取侧按
最新一条为准（见 latest_feedback_by_signal）。

Usage:
  python3 recommendation_feedback.py record --signal-id sig-xxx --verdict useful
  python3 recommendation_feedback.py record --signal-id sig-xxx --verdict not_useful --note "假突破"
  python3 recommendation_feedback.py list --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime
from typing import Any, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path
sys.path.insert(0, ROOT)

import signal_ledger  # noqa: E402


SCHEMA = "recommendation_feedback_v1"
VALID_VERDICTS = {"useful", "not_useful"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def record_feedback(
    *,
    signal_id: str,
    verdict: str,
    note: Optional[str] = None,
    ledger_file: Optional[str] = None,
) -> dict[str, Any]:
    verdict = str(verdict or "").strip().lower()
    if verdict not in VALID_VERDICTS:
        return {"error": f"非法反馈结论: {verdict}，允许值: {sorted(VALID_VERDICTS)}"}
    if not signal_id:
        return {"error": "signal_id 为必填字段"}

    path = ledger_file or signal_ledger.LEDGER_FILE
    signals = {
        str(record.get("signal_id")): record
        for record in signal_ledger.project_signals(ledger_file=path)
    }
    signal = signals.get(str(signal_id))
    if signal is None:
        return {"error": f"未找到 signal_id 对应的已开仓推荐: {signal_id}"}

    links = signal_ledger.make_links(
        signal.get("recommendation_id"),
        correlation_id=signal.get("correlation_id"),
        signal_id=signal_id,
        trade_id=signal.get("trade_id"),
        monitor_id=signal.get("monitor_id"),
    )
    payload = {
        "verdict": verdict,
        "note": note,
        "code": signal.get("code"),
        "strategy_id": signal.get("strategy_id"),
        "source": signal.get("source"),
        "recorded_at": _now(),
    }
    event = signal_ledger.append_event(
        "recommendation.feedback",
        links,
        payload,
        idempotency_key=f"recommendation.feedback:{signal_id}:{uuid.uuid4().hex}",
        ledger_file=path,
    )
    return {"ok": True, "event": event}


def list_feedback(ledger_file: Optional[str] = None) -> list[dict[str, Any]]:
    path = ledger_file or signal_ledger.LEDGER_FILE
    events = signal_ledger.read_events(path)
    rows = []
    for event in events:
        if event.get("event_type") != "recommendation.feedback":
            continue
        links = event.get("links") or {}
        payload = event.get("payload") or {}
        rows.append({
            "signal_id": links.get("signal_id"),
            "recommendation_id": links.get("recommendation_id"),
            "code": payload.get("code"),
            "strategy_id": payload.get("strategy_id"),
            "source": payload.get("source"),
            "verdict": payload.get("verdict"),
            "note": payload.get("note"),
            "recorded_at": payload.get("recorded_at"),
            "occurred_at": event.get("occurred_at"),
        })
    return rows


def latest_feedback_by_signal(
    events: Optional[list[dict[str, Any]]] = None,
    ledger_file: Optional[str] = None,
) -> dict[str, dict[str, Any]]:
    """每个 signal_id 只保留最新一次反馈（按事件在 ledger 中出现的顺序）。"""
    path = ledger_file or signal_ledger.LEDGER_FILE
    stream = events if events is not None else signal_ledger.read_events(path)
    latest: dict[str, dict[str, Any]] = {}
    for event in stream:
        if event.get("event_type") != "recommendation.feedback":
            continue
        links = event.get("links") or {}
        signal_id = links.get("signal_id")
        if not signal_id:
            continue
        latest[str(signal_id)] = dict(event.get("payload") or {})
    return latest


def format_feedback(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "📋 暂无推荐反馈记录"
    lines = [
        "📋 **推荐反馈记录**",
        f"共 {len(rows)} 条",
        "",
        "| signal_id | 标的 | 策略 | 结论 | 备注 |",
        "|-----------|------|------|------|------|",
    ]
    for row in rows[-20:]:
        lines.append(
            f"| {row.get('signal_id')} | {row.get('code')} | {row.get('strategy_id') or '-'} | "
            f"{row.get('verdict')} | {row.get('note') or '-'} |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="推荐反馈回流")
    sub = parser.add_subparsers(dest="command")

    record_parser = sub.add_parser("record", help="记录一条反馈")
    record_parser.add_argument("--signal-id", required=True)
    record_parser.add_argument("--verdict", required=True, choices=sorted(VALID_VERDICTS))
    record_parser.add_argument("--note")
    record_parser.add_argument("--json", action="store_true")

    list_parser = sub.add_parser("list", help="列出所有反馈")
    list_parser.add_argument("--json", action="store_true")

    args = parser.parse_args()

    if args.command == "record":
        output = record_feedback(signal_id=args.signal_id, verdict=args.verdict, note=args.note)
        if args.json:
            print(json.dumps(output, ensure_ascii=False, indent=2))
        else:
            print(output if "error" in output else "反馈已记录")
    elif args.command == "list":
        output = list_feedback()
        if args.json:
            print(json.dumps(output, ensure_ascii=False, indent=2))
        else:
            print(format_feedback(output))
    else:
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
