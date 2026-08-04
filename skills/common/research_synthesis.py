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
import hashlib
from datetime import datetime
from typing import Any

from state_store import atomic_write_json, file_lock, read_json

import research_bus


SYNTHESIS_SCHEMA = "research_synthesis_v1"
PROPOSAL_SCHEMA = "research_proposal_v1"
_LIST_CAP = 10


def _now_text(now: str | None = None) -> str:
    return now or datetime.now().astimezone().isoformat(timespec="seconds")


def load_findings(task: dict[str, Any]) -> dict[str, dict[str, Any]]:
    board = research_bus.board_dir(str(task.get("id")))
    findings: dict[str, dict[str, Any]] = {}
    for role in task.get("expert_plan") or []:
        value = read_json(os.path.join(board, f"{role}.json"), None)
        if isinstance(value, dict):
            findings[role] = value
    return findings


def _revalidate_model_manifests(
    task: dict[str, Any],
    findings: dict[str, dict[str, Any]],
    config: dict[str, Any],
    now: str,
) -> dict[str, dict[str, Any]]:
    finding_cfg = config.get("finding") or {}
    if not finding_cfg.get("require_model_run_manifest"):
        return findings
    try:
        import agent_evidence
        import evidence_pack
    except ImportError:
        return {
            role: {**finding, "model_run_manifest": {"execution_eligible": False}}
            for role, finding in findings.items()
        }
    checked: dict[str, dict[str, Any]] = {}
    for role, finding in findings.items():
        item = dict(finding)
        if item.get("stance") == "abstain":
            checked[role] = item
            continue
        manifest = item.get("model_run_manifest") or {}
        pack = evidence_pack.load_pack(str(manifest.get("evidence_pack_ref") or ""))
        if not pack:
            checked[role] = {
                **item,
                "model_run_manifest": {
                    **dict(manifest),
                    "execution_eligible": False,
                },
                "model_integrity_errors": ["artifact_hash_mismatch"],
            }
            continue
        role_state = (task.get("roles") or {}).get(role) or {}
        errors = agent_evidence.validate_finding_manifest(
            manifest,
            evidence_pack=pack,
            evidence_refs=item.get("evidence_refs") or [],
            tool_inputs=item.get("tool_inputs") or {},
            finding=item,
            task_id=str(task.get("id") or ""),
            role=role,
            claim_id=str(role_state.get("submission_claim_id") or ""),
            submitter=str(role_state.get("submission_worker") or ""),
            require_execution_eligible=False,
            now=now,
            max_age_minutes=int(finding_cfg.get("manifest_max_age_minutes") or 10),
        )
        if errors:
            manifest = dict(item.get("model_run_manifest") or {})
            manifest["execution_eligible"] = False
            item["model_integrity_errors"] = errors
            item["model_run_manifest"] = manifest
        checked[role] = item
    return checked


_STRUCTURE_RISK_FLAG_LABELS = {
    "seg_end_divergence": "线段末端背驰",
    "third_sell_structure": "三卖后反弹未过中枢下沿",
}


def _structure_position_risk_flags(task: dict[str, Any]) -> list[str]:
    """从任务的 evidence_pack 读取 candidate_entry.research_evidence.structure_position
    .risk_flags（chanlun verdict B 遗留项 2：结构位置只作证据陈列，不预测方向）。

    只读已缓存的 evidence_pack（research_evidence.build_research_evidence 早在
    candidate_discovery 阶段算好、随 candidate_pool 落盘），不新增网络调用；
    pack 缺失/字段不存在时静默返回空列表（fail-open，这只是陈列性证据）。
    """
    ref = str(task.get("evidence_pack_ref") or "")
    if not ref:
        return []
    try:
        import evidence_pack
    except ImportError:
        return []
    pack = evidence_pack.load_pack(ref)
    if not pack:
        return []
    subject_data = (pack.get("payload") or {}).get("subject_data")
    if not isinstance(subject_data, dict):
        return []
    candidate = subject_data.get("candidate_entry")
    if not isinstance(candidate, dict):
        return []
    structure_position = (candidate.get("research_evidence") or {}).get("structure_position")
    if not isinstance(structure_position, dict):
        return []
    raw_flags = structure_position.get("risk_flags") or []
    return [
        f"[结构位置]{_STRUCTURE_RISK_FLAG_LABELS.get(flag, flag)}"
        for flag in raw_flags if isinstance(flag, str)
    ]


def _augment_risk_redteam_with_structure_position(
    task: dict[str, Any],
    findings: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """把 structure_position.risk_flags 并入 risk_redteam 的证据文本。

    只作证据陈列（追加到 risk_flags 供 `_merge_lists` 渲染进报告的"风险标记"），
    不改 stance/confidence——risk_redteam 的一票否决逻辑（decide_verdict 里的
    stance==oppose 判定）完全不受影响。
    """
    risk_finding = findings.get("risk_redteam")
    if not isinstance(risk_finding, dict):
        return findings
    extra_flags = _structure_position_risk_flags(task)
    if not extra_flags:
        return findings
    existing = list(risk_finding.get("risk_flags") or [])
    merged_flags = existing + [flag for flag in extra_flags if flag not in existing]
    if merged_flags == existing:
        return findings
    return {**findings, "risk_redteam": {**risk_finding, "risk_flags": merged_flags}}


def _confidence(finding: dict[str, Any]) -> float:
    try:
        return max(0.0, min(1.0, float(finding.get("confidence", 0.0))))
    except (TypeError, ValueError):
        return 0.0


def _review_gate_decision(
    findings: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    review_only_roles = [
        role
        for role, finding in findings.items()
        if finding.get("stance") != "abstain"
        and isinstance(finding.get("model_run_manifest"), dict)
        and (finding.get("model_run_manifest") or {}).get("execution_eligible")
        is not True
    ]
    if review_only_roles:
        return {
            "verdict": "review_only",
            "basis": "human_review_required:" + ",".join(sorted(review_only_roles)),
        }
    stances = [
        str(finding.get("stance") or "neutral")
        for finding in findings.values()
    ]
    if stances and all(stance == "abstain" for stance in stances):
        return {"verdict": "abstained", "basis": "all_roles_abstained"}
    return None


def _adjudicator_decision(
    findings: dict[str, dict[str, Any]],
    synthesis_cfg: dict[str, Any],
    task: dict[str, Any] | None,
    conflict_at: float,
) -> dict[str, Any] | None:
    adjudicator = findings.get("adjudicator")
    if not isinstance(adjudicator, dict):
        return None
    escalation = synthesis_cfg.get("escalation") or {}
    adjudicator_cfg = escalation.get("adjudicator") or {}
    round_index = int((task or {}).get("escalation_round") or 0)
    max_rounds = int(escalation.get("max_rounds") or 1)
    plan = (task or {}).get("expert_plan") or []
    problems: list[str] = []
    if not (
        escalation.get("enabled")
        and adjudicator_cfg.get("enabled")
        and round_index == max_rounds
        and "adjudicator" in plan
    ):
        problems.append("illegal_round")
    stance = str(adjudicator.get("stance") or "")
    if stance not in {"support", "oppose"}:
        problems.append("stance")
    minimum = float(adjudicator_cfg.get("min_confidence") or conflict_at)
    if _confidence(adjudicator) < minimum:
        problems.append("confidence")
    peer_roles = {
        str(ref).removeprefix("peer_findings.findings.").split(".", 1)[0]
        for ref in adjudicator.get("evidence_refs") or []
        if str(ref).startswith("peer_findings.findings.")
    }
    eligible_peers = {
        role
        for role, finding in findings.items()
        if role != "adjudicator"
        and finding.get("stance") in {"support", "oppose"}
    }
    if not peer_roles.intersection(eligible_peers):
        problems.append("peer_evidence")
    if not adjudicator.get("adjudication_points"):
        problems.append("adjudication_points")
    if problems:
        return {
            "verdict": "disputed",
            "basis": "adjudicator_fail_closed:" + ",".join(problems),
            "final_stance": "oppose",
        }
    return {
        "verdict": "adjudicated",
        "basis": f"adjudicator_{stance}",
        "final_stance": stance,
    }


def _risk_consensus_decision(
    findings: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    risk_roles = {"risk_redteam", "risk_aggressive", "risk_neutral"}
    risk_findings = [
        finding
        for role, finding in findings.items()
        if role in risk_roles and finding.get("stance") in {"support", "oppose"}
    ]
    if len(risk_findings) >= 2 and all(
        finding.get("stance") == "oppose" for finding in risk_findings
    ):
        return {
            "verdict": "disputed",
            "basis": "risk_triad_unanimous_oppose",
        }
    return None


def _directional_decision(
    findings: dict[str, dict[str, Any]],
    *,
    conflict_at: float,
    advance_at: float,
) -> dict[str, Any]:
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


def decide_verdict(
    findings: dict[str, dict[str, Any]],
    synthesis_cfg: dict[str, Any],
    *,
    task: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gated = _review_gate_decision(findings)
    if gated:
        return gated
    veto_at = float(synthesis_cfg.get("veto_confidence") or 0.7)
    conflict_at = float(synthesis_cfg.get("conflict_confidence") or 0.6)
    advance_at = float(synthesis_cfg.get("advance_min_support_confidence") or 0.6)
    risk = findings.get("risk_redteam")
    if risk and risk.get("stance") == "oppose" and _confidence(risk) >= veto_at:
        return {"verdict": "rejected", "basis": "risk_redteam_veto"}
    adjudicated = _adjudicator_decision(
        findings, synthesis_cfg, task, conflict_at,
    )
    if adjudicated:
        return adjudicated
    consensus = _risk_consensus_decision(findings)
    if consensus:
        return consensus
    return _directional_decision(
        findings,
        conflict_at=conflict_at,
        advance_at=advance_at,
    )


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
        "synthesis_ref": os.path.join(
            research_bus.board_dir(str(task.get("id"))),
            "synthesis.json",
        ),
        "synthesis_sha256": synthesis["synthesis_sha256"],
        "summary": [entry.get("summary") for entry in synthesis["findings"]],
        "counterevidence": synthesis.get("counterevidence") or [],
        "invalidation_conditions": synthesis.get("invalidation_conditions") or [],
        "policy_gate_required": True,
        "live_effect": "none_until_strategy_registry_and_decision_policy_pass",
    })
    return path


def _synthesis_sha256(synthesis: dict[str, Any]) -> str:
    """Hash the semantic synthesis, excluding self-hash and storage locators."""
    payload = {
        key: value
        for key, value in synthesis.items()
        if key not in {
            "synthesis_sha256", "report_path", "proposal_path",
        }
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _maybe_escalate(
    task: dict[str, Any],
    findings: dict[str, dict[str, Any]],
    synthesis_cfg: dict[str, Any],
    now: str,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    escalation = synthesis_cfg.get("escalation") or {}
    if not escalation.get("enabled"):
        return None
    round_index = int(task.get("escalation_round") or 0)
    max_rounds = int(escalation.get("max_rounds") or 1)
    adjudicator_cfg = escalation.get("adjudicator") or {}
    if round_index >= max_rounds:
        if adjudicator_cfg.get("enabled") and "adjudicator" not in (
            task.get("expert_plan") or []
        ):
            roles = dict(task.get("roles") or {})
            roles["adjudicator"] = {"status": "pending", "attempts": 0}
            plan = list(task.get("expert_plan") or []) + ["adjudicator"]
            research_bus.update_task(str(task.get("id")), {
                "status": "in_progress",
                "roles": roles,
                "expert_plan": plan,
            })
            return {"needs_adjudicator": True}
        return None
    conflicted = [
        role for role, finding in findings.items()
        if finding.get("stance") in ("support", "oppose")
    ]
    board = research_bus.board_dir(str(task.get("id")))
    next_round = round_index + 1
    conflicting_findings = {role: findings[role] for role in conflicted}
    import evidence_pack

    context_hash = evidence_pack.peer_context_sha256(conflicting_findings)
    atomic_write_json(
        os.path.join(board, f"escalation-{next_round}.json"),
        {
            "round": next_round,
            "created_at": now,
            "peer_context_sha256": context_hash,
            "conflicting_findings": conflicting_findings,
        },
    )
    roles = dict(task.get("roles") or {})
    for role in conflicted:
        roles[role] = {"status": "pending", "attempts": 0}
    plan = list(task.get("expert_plan") or [])
    refreshed_task = {
        **task,
        "status": "in_progress",
        "escalation_round": next_round,
        "roles": roles,
        "expert_plan": plan,
    }
    previous_ref = task.get("evidence_pack_ref")
    refreshed_pack = evidence_pack.build_pack(refreshed_task, config=config)
    refreshed_task["evidence_pack_ref"] = refreshed_pack["ref"]
    research_bus.update_task(str(task.get("id")), {
        key: refreshed_task[key]
        for key in (
            "status", "escalation_round", "roles", "expert_plan",
            "evidence_pack_ref",
        )
    })
    return {
        "escalated": True,
        "round": next_round,
        "roles": conflicted,
        "previous_evidence_pack_ref": previous_ref,
        "evidence_pack_ref": refreshed_pack["ref"],
        "peer_context_sha256": context_hash,
    }


_TERMINAL_STATUS = {
    "advance": "done",
    "watch": "done",
    "disputed": "done",
    "rejected": "rejected",
    "abstained": "abstained",
    "review_only": "done",
    "adjudicated": "done",
}


def _persist_synthesis(
    task: dict[str, Any],
    task_id: str,
    synthesis: dict[str, Any],
) -> str:
    """Write the human report and the board copy; return the board path."""
    report_path = os.path.join(
        research_bus.reports_dir(),
        f"{task.get('trading_date')}-{task_id}.md",
    )
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write(_render_report(task, synthesis))
    synthesis["report_path"] = report_path

    board_path = os.path.join(research_bus.board_dir(task_id), "synthesis.json")
    atomic_write_json(board_path, synthesis)
    return board_path


def _existing_synthesis(task: dict[str, Any]) -> dict[str, Any] | None:
    if task.get("status") not in {"done", "rejected", "abstained"}:
        return None
    path = str(task.get("synthesis_path") or "")
    value = read_json(path, None) if path else None
    return value if isinstance(value, dict) else None


def _finding_readiness_error(
    task: dict[str, Any],
    findings: dict[str, dict[str, Any]],
) -> str | None:
    missing = [
        role for role in task.get("expert_plan") or []
        if role not in findings
    ]
    if missing:
        return f"findings missing for roles: {missing}"
    failed = sorted(
        role
        for role, state in (task.get("roles") or {}).items()
        if isinstance(state, dict) and str(state.get("status")) == "failed"
    )
    if failed:
        return f"agent failure is not neutral evidence; failed roles: {failed}"
    return None


def _effective_synthesis_config(
    task: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    synthesis_cfg = dict(config.get("synthesis") or {})
    kind_cfg = (config.get("task_kinds") or {}).get(str(task.get("kind"))) or {}
    if isinstance(kind_cfg.get("escalation"), dict):
        synthesis_cfg["escalation"] = {
            **dict(synthesis_cfg.get("escalation") or {}),
            **dict(kind_cfg["escalation"]),
        }
    return synthesis_cfg


def _build_synthesis_record(
    task: dict[str, Any],
    findings: dict[str, dict[str, Any]],
    decision: dict[str, Any],
    timestamp: str,
) -> dict[str, Any]:
    verdict = decision["verdict"]
    record: dict[str, Any] = {
        "schema": SYNTHESIS_SCHEMA,
        "task_id": task.get("id"),
        "generated_at": timestamp,
        "verdict": verdict,
        "basis": decision["basis"],
        "findings": _digest(findings),
        "counterevidence": _merge_lists(findings, "counterevidence"),
        "invalidation_conditions": _merge_lists(
            findings, "invalidation_conditions",
        ),
        "risk_flags": _merge_lists(findings, "risk_flags"),
        "policy_gate_required": (
            verdict in ("advance", "watch")
            or (
                verdict == "adjudicated"
                and decision.get("final_stance") == "support"
            )
        ),
        "expert_stances": [
            {
                "role": role,
                "stance": finding.get("stance"),
                "confidence": finding.get("confidence"),
                "round": int(task.get("escalation_round") or 0),
            }
            for role, finding in sorted(findings.items())
        ],
    }
    if "final_stance" in decision:
        record["final_stance"] = decision["final_stance"]
    record["synthesis_sha256"] = _synthesis_sha256(record)
    return record


def _proposal_required(decision: dict[str, Any]) -> bool:
    return decision["verdict"] == "advance" or (
        decision["verdict"] == "adjudicated"
        and decision.get("final_stance") == "support"
    )


def _commit_synthesis(
    task: dict[str, Any],
    synthesis: dict[str, Any],
    decision: dict[str, Any],
    timestamp: str,
) -> None:
    task_id = str(task.get("id"))
    board_path = _persist_synthesis(task, task_id, synthesis)
    update = {
        "status": _TERMINAL_STATUS[decision["verdict"]],
        "verdict": decision["verdict"],
        "synthesis_path": board_path,
    }
    if "final_stance" in decision:
        update["final_stance"] = decision["final_stance"]
    research_bus.update_task(task_id, update)
    research_bus.append_ledger_event({
        "event_type": "research.synthesized",
        "task_id": task_id,
        "kind": task.get("kind"),
        "subject_key": task.get("subject_key"),
        "trading_date": task.get("trading_date"),
        "verdict": decision["verdict"],
        "at": timestamp,
    })


def _synthesize_task_locked(
    task_id: str,
    *,
    config: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    config = config or research_bus.load_config()
    task = research_bus.find_task(task_id)
    if not task:
        return {"ok": False, "error": "task not found"}
    existing = _existing_synthesis(task)
    if existing:
        return {"ok": True, "idempotent": True, "synthesis": existing}
    findings = load_findings(task)
    readiness_error = _finding_readiness_error(task, findings)
    if readiness_error:
        return {"ok": False, "error": readiness_error}
    timestamp = _now_text(now)
    findings = _revalidate_model_manifests(task, findings, config, timestamp)
    findings = _augment_risk_redteam_with_structure_position(task, findings)
    synthesis_cfg = _effective_synthesis_config(task, config)
    decision = decide_verdict(findings, synthesis_cfg, task=task)
    if decision["verdict"] == "disputed":
        escalated = _maybe_escalate(
            task, findings, synthesis_cfg, timestamp, config,
        )
        if escalated:
            return {"ok": True, **escalated}
    synthesis = _build_synthesis_record(task, findings, decision, timestamp)
    if _proposal_required(decision):
        synthesis["proposal_path"] = _write_proposal(task, synthesis, timestamp)
    _commit_synthesis(task, synthesis, decision, timestamp)
    return {"ok": True, "synthesis": synthesis}


def synthesize_task(
    task_id: str,
    *,
    config: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Serialize the terminal compare-and-commit boundary per research task."""
    guard = os.path.join(research_bus.board_dir(task_id), "synthesis.commit")
    with file_lock(guard, timeout=30.0):
        return _synthesize_task_locked(task_id, config=config, now=now)


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
