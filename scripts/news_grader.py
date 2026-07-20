#!/usr/bin/env python3
"""L2 model-grading contract for the news pipeline.

Mirrors ``scripts/expert_runner.py``'s next/submit shape so a model turn can
claim a batch of L1-passed news items, grade them against a hard schema, and
submit. materiality=3 grades trigger the breaking-event bypass: an immediate
``research_bus.enqueue_task`` call plus a bounded Feishu push, so a genuinely
major policy/news event does not wait for the next scheduled research window.

This script is runtime-model-neutral: nothing here names or prefers any
specific model. Whichever Hermes/OpenClaw session turn calls ``next`` does
the grading; the contract only cares about the JSON shape it gets back.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
if COMMON not in sys.path:
    sys.path.insert(0, COMMON)

import feishu_push  # noqa: E402
import news_pipeline  # noqa: E402
import research_bus  # noqa: E402
from runtime_context import resolve_runtime_name  # noqa: E402
from state_store import read_json  # noqa: E402


DEFAULT_CONFIG_PATH = os.path.join(ROOT, "config", "news_pipeline.json")
GRADE_SCHEMA = "news_grade_batch_v1"
MATERIALITY_RANGE = (0, 3)
VALID_TIME_WINDOWS = {"intraday", "1-3d", "1-2w", "structural", "unknown"}


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def load_pipeline_config(path: str | None = None) -> dict[str, Any]:
    try:
        with open(path or DEFAULT_CONFIG_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def _l2_settings(config: dict[str, Any]) -> dict[str, Any]:
    defaults = {
        "claim_ttl_minutes": 30,
        "max_attempts_per_batch": 2,
        "batch_size": 20,
        "materiality_range": list(MATERIALITY_RANGE),
        "breaking_materiality": 3,
        "profile": "skills/research-committee/experts/news_grader.md",
    }
    defaults.update(config.get("l2") or {})
    return defaults


def _profile_text(relative_path: str) -> str:
    path = os.path.join(ROOT, relative_path) if relative_path else ""
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    return "(profile missing) 按输出契约保守作答，不确定就打低分并说明理由。"


def cmd_next(args: argparse.Namespace, config: dict[str, Any]) -> int:
    worker = args.worker or resolve_runtime_name(None, os.environ)
    l2_cfg = _l2_settings(config)
    batch = news_pipeline.claim_l1_batch(
        worker,
        batch_size=int(args.batch_size or l2_cfg["batch_size"]),
        ttl_minutes=int(l2_cfg["claim_ttl_minutes"]),
        max_attempts=int(l2_cfg["max_attempts_per_batch"]),
    )
    if not batch:
        _print({"status": "idle", "worker": worker, "queue": news_pipeline.queue_summary()})
        return 0
    items = [
        {
            "fingerprint": entry.get("fingerprint"),
            "title": entry.get("title"),
            "summary": entry.get("summary") or "",
            "detail_status": entry.get("detail_status") or "title_only",
            "url": entry.get("url"),
            "source_name": entry.get("source_name"),
            "source_rank": entry.get("source_rank"),
            "source_type": entry.get("source_type"),
            "authority_scope": entry.get("authority_scope"),
            "matched_keywords": entry.get("matched_keywords"),
            "excerpt": entry.get("excerpt"),
            "published_hint": entry.get("published_hint"),
        }
        for entry in batch
    ]
    _print({
        "schema": "news_grade_work_order_v1",
        "status": "work",
        "worker": worker,
        "batch_size": len(items),
        "items": items,
        "instructions": _profile_text(l2_cfg["profile"]),
        "output_contract": {
            "schema": GRADE_SCHEMA,
            "required": ["schema", "grades"],
            "grade_required": [
                "fingerprint", "materiality", "affected_sectors",
                "time_window", "needs_deep_review",
            ],
            "grade_optional": ["affected_codes"],
            "materiality_range": l2_cfg["materiality_range"],
            "time_window_values": sorted(VALID_TIME_WINDOWS),
            "rules": [
                "grades 必须覆盖工单里的每一个 fingerprint，且不得新增未知 fingerprint",
                "materiality 必须是整数且落在 materiality_range 内，越界拒收整批",
                "affected_sectors 证据不足留空数组，禁止编造",
                "affected_codes 可选：条目明确点名个股代码时填写，证据不足留空数组，禁止编造",
            ],
        },
        "submit_command": "python scripts/news_grader.py submit --file <grades.json>",
    })
    return 0


def _validate_grades(payload: Any, expected_fingerprints: set[str]) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload must be a JSON object"]
    if payload.get("schema") != GRADE_SCHEMA:
        errors.append(f"schema must be {GRADE_SCHEMA!r}")
    grades = payload.get("grades")
    if not isinstance(grades, list) or not grades:
        errors.append("grades must be a non-empty list")
        return errors

    seen: set[str] = set()
    lo, hi = MATERIALITY_RANGE
    for idx, grade in enumerate(grades):
        prefix = f"grades[{idx}]"
        if not isinstance(grade, dict):
            errors.append(f"{prefix} must be an object")
            continue
        fp = grade.get("fingerprint")
        if not fp or not isinstance(fp, str):
            errors.append(f"{prefix}.fingerprint missing")
            continue
        if fp not in expected_fingerprints:
            errors.append(f"{prefix}.fingerprint {fp!r} not in claimed batch")
            continue
        seen.add(fp)
        materiality = grade.get("materiality")
        if not isinstance(materiality, int) or isinstance(materiality, bool) or not (lo <= materiality <= hi):
            errors.append(f"{prefix}.materiality must be an integer in [{lo},{hi}]")
        sectors = grade.get("affected_sectors")
        if sectors is not None and not isinstance(sectors, list):
            errors.append(f"{prefix}.affected_sectors must be a list")
        codes = grade.get("affected_codes")
        if codes is not None and not isinstance(codes, list):
            errors.append(f"{prefix}.affected_codes must be a list")
        time_window = grade.get("time_window")
        if time_window is not None and time_window not in VALID_TIME_WINDOWS:
            errors.append(f"{prefix}.time_window must be one of {sorted(VALID_TIME_WINDOWS)}")
        if "needs_deep_review" in grade and not isinstance(grade.get("needs_deep_review"), bool):
            errors.append(f"{prefix}.needs_deep_review must be a boolean")

    missing = expected_fingerprints - seen
    if missing:
        errors.append(f"grades missing fingerprints: {sorted(missing)}")
    return errors


def _breaking_bypass(
    entry: dict[str, Any],
    grade: dict[str, Any],
    *,
    research_config: dict[str, Any],
    l2_cfg: dict[str, Any],
    pipeline_config: dict[str, Any],
) -> dict[str, Any]:
    """materiality=3 → enqueue research task + bounded Feishu push.

    ``kind`` is fixed to the existing ``anomaly_review`` task kind (single
    ``risk_redteam`` role) because this script must not add a new kind to
    ``config/research_committee.json`` — that file is owned by the parallel
    fact-plane/candidate-state-machine work in this branch set. A suggested
    ``event_review`` kind JSON fragment is reported to the caller instead of
    being applied here.
    """
    kind = str(l2_cfg.get("research_task_kind") or "anomaly_review")
    subject = {
        "theme": entry.get("title"),
        "name": entry.get("title"),
        "source": entry.get("source_name"),
    }
    outcome = research_bus.enqueue_task(
        kind,
        subject,
        reason="news_l2_materiality_3",
        trigger={
            "source": "news_grader.submit",
            "fingerprint": entry.get("fingerprint"),
            "source_id": entry.get("source_id"),
            "source_url": entry.get("url"),
            "affected_sectors": grade.get("affected_sectors") or [],
            "time_window": grade.get("time_window"),
        },
        config=research_config,
    )
    if outcome.get("enqueued"):
        research_bus.append_ledger_event({
            "event_type": "research.enqueued",
            "task_id": outcome["task"]["id"],
            "kind": kind,
            "reason": "news_l2_materiality_3",
            "trading_date": outcome["task"].get("trading_date"),
        })

    max_chars = int((pipeline_config.get("breaking") or {}).get("delivery_max_chars") or 800)
    push_text = (
        f"[重大] {entry.get('title', '')}\n"
        f"来源: {entry.get('source_name', '')} ({entry.get('source_rank', '')})\n"
        f"板块: {', '.join(grade.get('affected_sectors') or []) or '未标注'}\n"
        f"窗口: {grade.get('time_window') or 'unknown'}\n"
        f"研究任务: {outcome.get('task', {}).get('id') or outcome.get('task_id', '(未入队/已存在)')}"
    )[:max_chars]
    push_result = feishu_push.push_text("news-l2-breaking", push_text)
    return {"research_enqueue": outcome, "delivery": push_result}


def cmd_submit(args: argparse.Namespace, config: dict[str, Any]) -> int:
    try:
        with open(args.file, encoding="utf-8") if args.file else sys.stdin as handle:
            payload = json.load(handle) if args.file else json.loads(handle.read())
    except (OSError, json.JSONDecodeError) as error:
        _print({"ok": False, "errors": [f"cannot read grades JSON: {error}"]})
        return 2

    queue = read_json(news_pipeline.l1_queue_path(), [])
    claimed_fp = {
        entry.get("fingerprint")
        for entry in queue
        if isinstance(entry, dict)
        and entry.get("status") == "claimed"
        and entry.get("fingerprint")
    }
    if not claimed_fp:
        _print({"ok": False, "errors": ["no claimed batch to submit against"]})
        return 2
    errors = _validate_grades(payload, claimed_fp)
    if errors:
        _print({"ok": False, "errors": errors})
        return 2

    grades = payload["grades"]
    l2_cfg = _l2_settings(config)
    breaking_threshold = int(l2_cfg.get("breaking_materiality") or 3)

    submit_result = news_pipeline.submit_l2_grades(grades)
    queue = read_json(news_pipeline.l1_queue_path(), [])
    by_fp = {e.get("fingerprint"): e for e in queue if isinstance(e, dict)}

    research_config = research_bus.load_config()
    bypassed: list[dict[str, Any]] = []
    for grade in grades:
        if int(grade.get("materiality") or 0) < breaking_threshold:
            continue
        entry = by_fp.get(grade.get("fingerprint")) or {}
        outcome = _breaking_bypass(
            entry, grade,
            research_config=research_config, l2_cfg=l2_cfg, pipeline_config=config,
        )
        bypassed.append({"fingerprint": grade.get("fingerprint"), **outcome})

    result = {"ok": True, **submit_result, "breaking_bypass": bypassed}
    _print(result)
    return 0


def cmd_status(args: argparse.Namespace, config: dict[str, Any]) -> int:
    _print(news_pipeline.queue_summary())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="新闻管道 L2 模型分级 runner")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    nxt = sub.add_parser("next", help="claim 下一批 L1 通过条目")
    nxt.add_argument("--worker")
    nxt.add_argument("--batch-size", type=int)

    submit = sub.add_parser("submit", help="提交 schema 校验的批次分级结果")
    submit.add_argument("--file")

    sub.add_parser("status", help="查看 L1/L2 队列状态")

    args = parser.parse_args()
    config = load_pipeline_config(args.config)
    handlers = {"next": cmd_next, "submit": cmd_submit, "status": cmd_status}
    return handlers[args.command](args, config)


if __name__ == "__main__":
    raise SystemExit(main())
