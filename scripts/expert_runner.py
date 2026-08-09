#!/usr/bin/env python3
"""Generic expert runner for the research plane.

One script parameterizes every expert role. A Hermes/OpenClaw model turn calls
``next`` to claim a (task, role) work item and receives a bounded work order
(role instructions + shared evidence pack + output contract), reasons over it,
then calls ``submit`` with a schema-validated finding. Insufficient evidence
packs are auto-abstained without spending any model tokens. When the last
planned role reports, the deterministic synthesis reduces the blackboard.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

import evidence_pack  # noqa: E402
import agent_evidence  # noqa: E402
import agent_run_contract  # noqa: E402
import research_bus  # noqa: E402
import research_synthesis  # noqa: E402
import agent_runtime_adapter  # noqa: E402
from runtime_context import resolve_runtime_name  # noqa: E402
from state_store import read_json  # noqa: E402


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _profile_text(role: str, config: dict[str, Any]) -> str:
    role_cfg = (config.get("experts") or {}).get(role) or {}
    relative = str(role_cfg.get("profile") or "")
    path = os.path.join(ROOT, relative) if relative else ""
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read()
        except OSError as error:
            return f"(profile unreadable: {error}) 按输出契约保守作答，证据不足必须 abstain。"
    return "(profile missing) 按输出契约保守作答，证据不足必须 abstain。"


def _escalation_context(task: dict[str, Any]) -> dict[str, Any] | None:
    round_index = int(task.get("escalation_round") or 0)
    if round_index <= 0:
        return None
    path = os.path.join(
        research_bus.board_dir(str(task.get("id"))),
        f"escalation-{round_index}.json",
    )
    value = read_json(path, None)
    return value if isinstance(value, dict) else None


def _output_contract(
    task: dict[str, Any],
    role: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    limits = config.get("finding") or {}
    role_cfg = (config.get("experts") or {}).get(role) or {}
    return {
        "schema": research_bus.FINDING_SCHEMA,
        "required": ["schema", "task_id", "role", "stance", "confidence", "summary"],
        "stance_values": sorted(research_bus.STANCES),
        "rules": [
            "stance!=abstain 时 evidence_refs 必须引用证据包中的具体条目",
            "stance=support 时 counterevidence 与 invalidation_conditions 必填且非空",
            "stance=abstain 时 abstain_reason 必填",
            "confidence ∈ [0,1]；不确定就调低，禁止编造证据包之外的事实",
        ],
        "max_summary_chars": int(limits.get("max_summary_chars") or 600),
        "max_finding_chars": min(
            int(limits.get("max_finding_chars") or 10000),
            int(role_cfg.get("max_output_chars") or 10000) * 2,
        ),
        "task_id": task.get("id"),
        "role": role,
    }


def _ensure_pack(
    task: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    ref = task.get("evidence_pack_ref")
    if ref:
        stored = evidence_pack.load_pack(str(ref))
        if stored:
            return {
                "ref": ref,
                "payload": stored.get("payload") or {},
                "quality": (stored.get("payload") or {}).get("quality") or {},
            }
    built = evidence_pack.build_pack(task, config=config)
    research_bus.update_task(
        str(task.get("id")), {"evidence_pack_ref": built["ref"]},
    )
    return built


def _auto_abstain(
    task: dict[str, Any],
    role: str,
    quality: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    reason = f"evidence_pack_insufficient: missing={quality.get('missing')}"
    finding = {
        "schema": research_bus.FINDING_SCHEMA,
        "task_id": task.get("id"),
        "role": role,
        "stance": "abstain",
        "confidence": 1.0,
        "summary": "证据包未达最低要求，按 fail-closed 弃权。",
        "abstain_reason": reason,
    }
    result = research_bus.submit_finding(
        str(task.get("id")), role, finding,
        claim_id=str(((task.get("roles") or {}).get(role) or {}).get("claim_id") or ""),
        config=config,
    )
    outcome: dict[str, Any] = {
        "status": "abstained_insufficient_evidence",
        "task_id": task.get("id"),
        "role": role,
        "reason": reason,
        "submit": result,
    }
    if result.get("ok") and result.get("all_roles_done"):
        outcome["synthesis"] = research_synthesis.synthesize_task(
            str(task.get("id")), config=config,
        )
    return outcome


def cmd_next(args: argparse.Namespace, config: dict[str, Any]) -> int:
    worker = args.worker or resolve_runtime_name(None, os.environ)
    roles = [args.role] if args.role else None
    work = research_bus.claim_next_work(worker, roles=roles, config=config)
    if not work:
        _print({"status": "idle", "worker": worker,
                "queue": research_bus.queue_summary()["by_status"]})
        return 0
    task, role = work["task"], work["role"]
    pack = _ensure_pack(task, config)
    quality = pack.get("quality") or {}
    if quality.get("status") == "insufficient":
        _print(_auto_abstain(task, role, quality, config))
        return 0
    _print({
        "schema": "research_work_order_v1",
        "status": "work",
        "worker": worker,
        "claim_id": work.get("claim_id"),
        "task_id": task.get("id"),
        "kind": task.get("kind"),
        "role": role,
        "subject": task.get("subject"),
        "instructions": _profile_text(role, config),
        "escalation_context": _escalation_context(task),
        "evidence_pack_ref": pack.get("ref"),
        "evidence_pack": pack.get("payload"),
        "output_contract": _output_contract(task, role, config),
        "submit_command": (
            f"python scripts/expert_runner.py submit "
            f"--task {task.get('id')} --role {role} --model <model-version> "
            f"--claim-id {work.get('claim_id')} "
            f"--file <finding.json>"
        ),
        "abstain_command": (
            f"python scripts/expert_runner.py abstain "
            f"--task {task.get('id')} --role {role} "
            f"--claim-id {work.get('claim_id')} --reason <为何弃权>"
        ),
    })
    return 0


def _read_finding(args: argparse.Namespace) -> Any:
    if args.file:
        with open(args.file, encoding="utf-8") as handle:
            return json.load(handle)
    return json.load(sys.stdin)


def _read_approval(path: str | None) -> Any:
    if not path:
        return None
    return agent_evidence.load_trusted_finding_approval(path)


def _attach_finding_manifest(
    finding: Any,
    *,
    args: argparse.Namespace,
    task: dict[str, Any],
    pack: dict[str, Any] | None,
    config: dict[str, Any],
    submitter: str,
    approval: Any,
) -> None:
    if (
        not isinstance(finding, dict)
        or not pack
        or finding.get("stance") == "abstain"
    ):
        return
    prompt = _profile_text(args.role, config) + json.dumps(
        _output_contract(task, args.role, config),
        ensure_ascii=False,
        sort_keys=True,
    )
    finding["model_run_manifest"] = agent_evidence.build_finding_manifest(
        model=args.model or os.environ.get("A_STOCK_MODEL_VERSION", ""),
        prompt=prompt,
        evidence_pack=pack,
        evidence_refs=finding.get("evidence_refs") or [],
        tool_inputs=finding.get("tool_inputs") or {},
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        finding=finding,
        approval=approval,
        approval_ref=args.approval_file or "",
        task_id=args.task,
        role=args.role,
        claim_id=args.claim_id,
        submitter=submitter,
    )


def _submit_finding_result(
    finding: Any,
    *,
    args: argparse.Namespace,
    task: dict[str, Any],
    pack: dict[str, Any] | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    request = agent_runtime_adapter.build_request(
        task,
        args.role,
        runtime=args.runtime or os.environ.get("A_STOCK_RUNTIME", "hermes"),
        output_schema=research_bus.FINDING_SCHEMA,
        max_output_chars=int(
            _output_contract(task, args.role, config).get("max_finding_chars")
            or 10000
        ),
        claim_id=args.claim_id,
        model=args.model,
    )
    status = (
        "abstained"
        if isinstance(finding, dict) and finding.get("stance") == "abstain"
        else "completed"
    )
    parsed = agent_run_contract.parse_result(
        {"status": status, "finding": finding},
        request=request,
        evidence_pack=pack,
    )
    return agent_runtime_adapter.submit_result(
        parsed,
        worker=args.worker,
        config=config,
    )


def cmd_submit(args: argparse.Namespace, config: dict[str, Any]) -> int:
    try:
        finding = _read_finding(args)
    except (OSError, json.JSONDecodeError) as error:
        _print({"ok": False, "errors": [f"cannot read finding JSON: {error}"]})
        return 2
    task = research_bus.find_task(args.task)
    role_state = ((task or {}).get("roles") or {}).get(args.role) or {}
    submitter = str(args.worker or role_state.get("claimed_by") or "")
    ref = str((task or {}).get("evidence_pack_ref") or "")
    pack = evidence_pack.load_pack(ref) if ref else None
    try:
        approval = _read_approval(args.approval_file)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        _print({"ok": False, "errors": [f"cannot read approval JSON: {error}"]})
        return 2
    if approval is not None:
        approval_errors = agent_evidence.validate_finding_approval(
            approval,
            task_id=args.task,
            role=args.role,
            claim_id=args.claim_id,
            finding=finding if isinstance(finding, dict) else {},
            submitter=submitter,
        )
        if approval_errors:
            _print({"ok": False, "errors": approval_errors})
            return 2
    _attach_finding_manifest(
        finding,
        args=args,
        task=task or {},
        pack=pack,
        config=config,
        submitter=submitter,
        approval=approval,
    )
    result = _submit_finding_result(
        finding,
        args=args,
        task=task or {},
        pack=pack,
        config=config,
    )
    if not result.get("ok"):
        _print(result)
        return 2
    if result.get("all_roles_done"):
        result["synthesis"] = research_synthesis.synthesize_task(
            args.task, config=config,
        )
    _print(result)
    return 0


def cmd_abstain(args: argparse.Namespace, config: dict[str, Any]) -> int:
    finding = {
        "schema": research_bus.FINDING_SCHEMA,
        "task_id": args.task,
        "role": args.role,
        "stance": "abstain",
        "confidence": 1.0,
        "summary": "专家主动弃权。",
        "abstain_reason": args.reason,
    }
    result = research_bus.submit_finding(
        args.task, args.role, finding, worker=args.worker,
        claim_id=args.claim_id, config=config,
    )
    if result.get("ok") and result.get("all_roles_done"):
        result["synthesis"] = research_synthesis.synthesize_task(
            args.task, config=config,
        )
    _print(result)
    return 0 if result.get("ok") else 2


def cmd_fail(args: argparse.Namespace, config: dict[str, Any]) -> int:
    result = research_bus.fail_role(
        args.task, args.role, args.error,
        retry=not args.no_retry, worker=args.worker,
        claim_id=args.claim_id, config=config,
    )
    _print(result)
    return 0 if result.get("ok") else 1


def cmd_status(args: argparse.Namespace, config: dict[str, Any]) -> int:
    summary = research_bus.queue_summary()
    if args.task:
        summary = {"task": research_bus.find_task(args.task)}
    _print(summary)
    return 0


def cmd_synthesize(args: argparse.Namespace, config: dict[str, Any]) -> int:
    if args.task:
        _print(research_synthesis.synthesize_task(args.task, config=config))
    else:
        _print(research_synthesis.synthesize_ready_tasks(config=config))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="研究平面通用专家 runner")
    sub = parser.add_subparsers(dest="command", required=True)

    nxt = sub.add_parser("next", help="claim 下一个 (task, role) 工单")
    nxt.add_argument("--worker")
    nxt.add_argument("--role")

    submit = sub.add_parser("submit", help="提交 schema 校验的 finding")
    submit.add_argument("--task", required=True)
    submit.add_argument("--role", required=True)
    submit.add_argument("--file")
    submit.add_argument("--worker")
    submit.add_argument("--claim-id", required=True)
    submit.add_argument("--model")
    submit.add_argument("--runtime")
    submit.add_argument(
        "--approval-file",
        help=(
            "独立 research_finding_approval_v1 JSON；必须绑定 "
            "task/role/claim/finding hash"
        ),
    )
    submit.add_argument(
        "--reviewed-by",
        help="已弃用，仅作兼容记录；不能授予 execution eligibility",
    )

    abstain = sub.add_parser("abstain", help="主动弃权")
    abstain.add_argument("--task", required=True)
    abstain.add_argument("--role", required=True)
    abstain.add_argument("--reason", required=True)
    abstain.add_argument("--worker")
    abstain.add_argument("--claim-id", required=True)

    fail = sub.add_parser("fail", help="上报角色执行失败")
    fail.add_argument("--task", required=True)
    fail.add_argument("--role", required=True)
    fail.add_argument("--error", required=True)
    fail.add_argument("--no-retry", action="store_true")
    fail.add_argument("--worker")
    fail.add_argument("--claim-id", required=True)

    status = sub.add_parser("status", help="查看队列/任务状态")
    status.add_argument("--task")

    synthesize = sub.add_parser("synthesize", help="手动触发确定性合成")
    synthesize.add_argument("--task")

    args = parser.parse_args()
    config = research_bus.load_config()
    handlers = {
        "next": cmd_next,
        "submit": cmd_submit,
        "abstain": cmd_abstain,
        "fail": cmd_fail,
        "status": cmd_status,
        "synthesize": cmd_synthesize,
    }
    return handlers[args.command](args, config)


if __name__ == "__main__":
    raise SystemExit(main())
