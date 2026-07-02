"""Deterministic blackboard synthesis for multi-expert research tasks.

Experts never talk to each other; each writes one schema-validated finding to
the task board. This reducer merges the board without any model turn: risk
veto and confidence thresholds decide the verdict, disagreement is surfaced
as an explicit ``disputed`` verdict instead of being averaged away, and the
human-readable report is generated exactly once per task. Directional
verdicts only ever produce proposals gated behind the existing decision
policy and strategy registry; nothing here writes fact-plane state.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from state_store import atomic_write_json, read_json

import research_bus


SYNTHESIS_SCHEMA = "research_synthesis_v1"
PROPOSAL_SCHEMA = "research_proposal_v1"
_LIST_CAP = 10


def _now_text(now: str | None = None) -> str:
    return now or datetime.now().isoformat(timespec="seconds")


def load_findings(task: dict[str, Any]) -> dict[str, dict[str, Any]]:
    board = research_bus.board_dir(str(task.get("id")))
    findings: dict[str, dict[str, Any]] = {}
    for role in task.get("expert_plan") or []:
        value = read_json(os.path.join(board, f"{role}.json"), None)
        if isinstance(value, dict):
            findings[role] = value
    return findings


def _confidence(finding: dict[str, Any]) -> float:
    try:
        return max(0.0, min(1.0, float(finding.get("confidence", 0.0))))
    except (TypeError, ValueError):
        return 0.0


def decide_verdict(
    findings: dict[str, dict[str, Any]],
    synthesis_cfg: dict[str, Any],
) -> dict[str, Any]:
    stances = {
        role: str(finding.get("stance") or "neutral")
        for role, finding in findings.items()
    }
    if stances and all(stance == "abstain" for stance in stances.values()):
        return {"verdict": "abstained", "basis": "all_roles_abstained"}
    veto_at = float(synthesis_cfg.get("veto_confidence") or 0.7)
    conflict_at = float(synthesis_cfg.get("conflict_confidence") or 0.6)
    advance_at = float(synthesis_cfg.get("advance_min_support_confidence") or 0.6)

    risk = findings.get("risk_redteam")
    if risk and risk.get("stance") == "oppose" and _confidence(risk) >= veto_at:
        return {"verdict": "rejected", "basis": "risk_redteam_veto"}

    support = max(
        (_confidence(f) for f in findings.values() if f.get("stance") == "support"),
        default=0.0,
    )
    oppose = max(
        (_confidence(f) for f in findings.values() if f.get("stance") == "oppose"),
        default=0.0,
    )
    if support >= conflict_at and oppose >= conflict_at:
        return {
            "verdict": "disputed",
            "basis": f"support_{support:.2f}_vs_oppose_{oppose:.2f}",
        }
    if support >= advance_at and oppose < conflict_at:
        return {"verdict": "advance", "basis": f"max_support_{support:.2f}"}
    return {"verdict": "watch", "basis": "no_confident_direction"}


def _merge_lists(
    findings: dict[str, dict[str, Any]],
    field: str,
) -> list[str]:
    merged: list[str] = []
    for role in sorted(findings):
        for item in findings[role].get(field) or []:
            text = str(item)
            if text not in merged:
                merged.append(text)
    return merged[:_LIST_CAP]


def _digest(findings: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "role": role,
            "stance": findings[role].get("stance"),
            "confidence": _confidence(findings[role]),
            "summary": findings[role].get("summary"),
            "abstain_reason": findings[role].get("abstain_reason"),
        }
        for role in sorted(findings)
    ]


def _render_report(
    task: dict[str, Any],
    synthesis: dict[str, Any],
) -> str:
    subject = task.get("subject") or {}
    lines = [
        f"# 研究合成报告 — {subject.get('name') or task.get('subject_key')}",
        "",
        f"- 任务: `{task.get('id')}` ({task.get('kind')})",
        f"- 交易日: {task.get('trading_date')}  触发: {task.get('reason')}",
        f"- 结论: **{synthesis['verdict']}** （依据: {synthesis['basis']}）",
        f"- 证据包: `{task.get('evidence_pack_ref')}`",
        "",
        "## 各专家结论",
        "",
    ]
    for entry in synthesis["findings"]:
        confidence = f"{entry['confidence']:.2f}"
        lines.append(
            f"- **{entry['role']}** [{entry['stance']} @ {confidence}] "
            f"{entry.get('summary') or entry.get('abstain_reason') or ''}"
        )
    for title, field in (
        ("## 反证", "counterevidence"),
        ("## 失效条件", "invalidation_conditions"),
        ("## 风险标记", "risk_flags"),
    ):
        items = synthesis.get(field) or []
        if items:
            lines.extend(["", title, ""])
            lines.extend(f"- {item}" for item in items)
    lines.extend([
        "",
        "> 本报告为研究平面产物。任何方向性动作必须先通过 decision policy、",
        "> strategy registry 与 OOS 门禁；未通过前保持 research-only。",
        "",
    ])
    return "\n".join(lines)


def _write_proposal(
    task: dict[str, Any],
    synthesis: dict[str, Any],
    now: str,
) -> str:
    path = os.path.join(
        research_bus.proposals_dir("pending"),
        f"{task.get('id')}.json",
    )
    atomic_write_json(path, {
        "schema": PROPOSAL_SCHEMA,
        "task_id": task.get("id"),
        "kind": task.get("kind"),
        "subject": task.get("subject"),
        "trading_date": task.get("trading_date"),
        "created_at": now,
        "verdict": synthesis["verdict"],
        "summary": [entry.get("summary") for entry in synthesis["findings"]],
        "counterevidence": synthesis.get("counterevidence") or [],
        "invalidation_conditions": synthesis.get("invalidation_conditions") or [],
        "policy_gate_required": True,
        "live_effect": "none_until_strategy_registry_and_decision_policy_pass",
    })
    return path


def _maybe_escalate(
    task: dict[str, Any],
    findings: dict[str, dict[str, Any]],
    synthesis_cfg: dict[str, Any],
    now: str,
) -> dict[str, Any] | None:
    escalation = synthesis_cfg.get("escalation") or {}
    if not escalation.get("enabled"):
        return None
    round_index = int(task.get("escalation_round") or 0)
    if round_index >= int(escalation.get("max_rounds") or 1):
        return None
    conflicted = [
        role for role, finding in findings.items()
        if finding.get("stance") in ("support", "oppose")
    ]
    board = research_bus.board_dir(str(task.get("id")))
    atomic_write_json(
        os.path.join(board, f"escalation-{round_index + 1}.json"),
        {
            "round": round_index + 1,
            "created_at": now,
            "conflicting_findings": {role: findings[role] for role in conflicted},
        },
    )
    roles = dict(task.get("roles") or {})
    for role in conflicted:
        roles[role] = {"status": "pending", "attempts": 0}
    research_bus.update_task(str(task.get("id")), {
        "status": "in_progress",
        "escalation_round": round_index + 1,
        "roles": roles,
    })
    return {"escalated": True, "round": round_index + 1, "roles": conflicted}


_TERMINAL_STATUS = {
    "advance": "done",
    "watch": "done",
    "disputed": "done",
    "rejected": "rejected",
    "abstained": "abstained",
}


def synthesize_task(
    task_id: str,
    *,
    config: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    config = config or research_bus.load_config()
    task = research_bus.find_task(task_id)
    if not task:
        return {"ok": False, "error": "task not found"}
    findings = load_findings(task)
    missing = [
        role for role in task.get("expert_plan") or []
        if role not in findings
    ]
    if missing:
        return {"ok": False, "error": f"findings missing for roles: {missing}"}

    timestamp = _now_text(now)
    synthesis_cfg = config.get("synthesis") or {}
    decision = decide_verdict(findings, synthesis_cfg)
    if decision["verdict"] == "disputed":
        escalated = _maybe_escalate(task, findings, synthesis_cfg, timestamp)
        if escalated:
            return {"ok": True, **escalated}

    synthesis: dict[str, Any] = {
        "schema": SYNTHESIS_SCHEMA,
        "task_id": task_id,
        "generated_at": timestamp,
        "verdict": decision["verdict"],
        "basis": decision["basis"],
        "findings": _digest(findings),
        "counterevidence": _merge_lists(findings, "counterevidence"),
        "invalidation_conditions": _merge_lists(
            findings, "invalidation_conditions",
        ),
        "risk_flags": _merge_lists(findings, "risk_flags"),
        "policy_gate_required": decision["verdict"] in ("advance", "watch"),
    }
    if decision["verdict"] == "advance":
        synthesis["proposal_path"] = _write_proposal(task, synthesis, timestamp)

    report_path = os.path.join(
        research_bus.reports_dir(),
        f"{task.get('trading_date')}-{task_id}.md",
    )
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write(_render_report(task, synthesis))
    synthesis["report_path"] = report_path

    board_path = os.path.join(
        research_bus.board_dir(task_id), "synthesis.json",
    )
    atomic_write_json(board_path, synthesis)
    research_bus.update_task(task_id, {
        "status": _TERMINAL_STATUS[decision["verdict"]],
        "verdict": decision["verdict"],
        "synthesis_path": board_path,
    })
    research_bus.append_ledger_event({
        "event_type": "research.synthesized",
        "task_id": task_id,
        "kind": task.get("kind"),
        "subject_key": task.get("subject_key"),
        "trading_date": task.get("trading_date"),
        "verdict": decision["verdict"],
        "at": timestamp,
    })
    return {"ok": True, "synthesis": synthesis}


def synthesize_ready_tasks(
    *,
    config: dict[str, Any] | None = None,
    now: str | None = None,
) -> list[dict[str, Any]]:
    config = config or research_bus.load_config()
    results = []
    for task in research_bus.load_tasks():
        if task.get("status") != "ready_to_synthesize":
            continue
        results.append({
            "task_id": task.get("id"),
            **synthesize_task(str(task.get("id")), config=config, now=now),
        })
    return results


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="研究黑板确定性合成")
    parser.add_argument("--task")
    parser.add_argument("--all-ready", action="store_true")
    args = parser.parse_args()
    if args.task:
        result: Any = synthesize_task(args.task)
    elif args.all_ready:
        result = synthesize_ready_tasks()
    else:
        parser.error("provide --task or --all-ready")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
