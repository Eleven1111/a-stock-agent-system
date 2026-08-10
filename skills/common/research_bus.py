"""Research task bus for the multi-expert research plane.

The fact plane (deterministic DAG) enqueues bounded research tasks; Hermes and
OpenClaw model turns claim one (task, role) work item at a time, submit a
schema-validated finding to the per-task blackboard, and a deterministic
synthesis reduces the board once every planned role has reported. Research
outputs are proposals and reports only; this module never writes fact-plane
state (portfolio, signal ledger, cron manifest).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any

from paths import data_file, skill_data_dir
from state_store import atomic_write_json, mutate_json, read_json


SKILL = "research-committee"
TASK_SCHEMA = "research_task_v1"
FINDING_SCHEMA = "research_finding_v1"
ACTIVE_STATUSES = {"pending", "in_progress", "ready_to_synthesize"}
TERMINAL_STATUSES = {"done", "failed", "abstained", "rejected"}
STANCES = {"support", "oppose", "neutral", "abstain"}

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

DEFAULT_CONFIG: dict[str, Any] = {
    "claim_ttl_minutes": 120,
    "max_attempts_per_role": 2,
    "budget": {
        "daily_char_budget": 400000,
        "instructions_chars_estimate": 3000,
    },
    "task_kinds": {},
    "experts": {},
    "finding": {
        "max_summary_chars": 600,
        "max_finding_chars": 10000,
    },
    "synthesis": {
        "veto_confidence": 0.7,
        "conflict_confidence": 0.6,
        "advance_min_support_confidence": 0.6,
        "escalation": {"enabled": False, "max_rounds": 1},
    },
    "triggers": {},
}


def config_path() -> str:
    return os.environ.get("A_STOCK_RESEARCH_CONFIG") or os.path.join(
        _REPO_ROOT, "config", "research_committee.json"
    )


def load_config(path: str | None = None) -> dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(path or config_path(), encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError):
        loaded = {}
    if not isinstance(loaded, dict):
        return merged
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def queue_file() -> str:
    return data_file(SKILL, "research_tasks.json")


def board_dir(task_id: str) -> str:
    return os.path.join(skill_data_dir(SKILL), "board", _safe(task_id))


def turns_file(task_id: str) -> str:
    """Append-only audit stream for every claim/submit/fail turn."""
    return os.path.join(board_dir(task_id), "turns.jsonl")


def packs_dir() -> str:
    return os.path.join(skill_data_dir(SKILL), "packs")


def reports_dir() -> str:
    return os.path.join(skill_data_dir(SKILL), "reports")


def proposals_dir(bucket: str = "pending") -> str:
    return os.path.join(skill_data_dir(SKILL), "proposals", _safe(bucket))


def ledger_file() -> str:
    return data_file(SKILL, "research_ledger.jsonl")


def budget_file(trading_date: str) -> str:
    return data_file(SKILL, os.path.join("budget", f"{_safe(trading_date)}.json"))


def _safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(value))


def _now_text(now: str | None = None) -> str:
    if now:
        return now
    return datetime.now().astimezone().isoformat(timespec="seconds")


def subject_key(subject: dict[str, Any] | None) -> str:
    subject = subject or {}
    code = str(subject.get("code") or "").strip()
    if code:
        return code.zfill(6) if code.isdigit() else code
    theme = str(subject.get("theme") or subject.get("name") or "").strip()
    return _safe(theme.lower()) or "unknown"


def make_task_id(kind: str, key: str, trading_date: str) -> str:
    return f"rt-{_safe(trading_date)}-{_safe(kind)}-{_safe(key)}"


def load_tasks(path: str | None = None) -> list[dict[str, Any]]:
    value = read_json(path or queue_file(), [])
    return value if isinstance(value, list) else []


def find_task(task_id: str, path: str | None = None) -> dict[str, Any] | None:
    for task in load_tasks(path):
        if task.get("id") == task_id:
            return task
    return None


def _kind_config(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    kinds = config.get("task_kinds") or {}
    value = kinds.get(kind)
    if not isinstance(value, dict) or not value.get("experts"):
        raise ValueError(f"unknown research task kind: {kind}")
    return value


def _in_cooldown(
    tasks: list[dict[str, Any]],
    kind: str,
    key: str,
    trading_date: str,
    cooldown_days: int,
) -> bool:
    if cooldown_days <= 0:
        return False
    try:
        floor = (
            date.fromisoformat(trading_date) - timedelta(days=cooldown_days)
        ).isoformat()
    except ValueError:
        return False
    for task in tasks:
        if task.get("kind") != kind or task.get("subject_key") != key:
            continue
        if task.get("status") not in TERMINAL_STATUSES:
            continue
        if str(task.get("trading_date") or "") >= floor:
            return True
    return False


def estimate_task_chars(kind: str, config: dict[str, Any]) -> int:
    kind_cfg = _kind_config(kind, config)
    budget_cfg = config.get("budget") or {}
    instructions = int(budget_cfg.get("instructions_chars_estimate") or 3000)
    pack = int(kind_cfg.get("pack_budget_chars") or 20000)
    experts_cfg = config.get("experts") or {}
    total = pack
    for role in kind_cfg.get("experts") or []:
        role_cfg = experts_cfg.get(role) or {}
        total += instructions + int(role_cfg.get("max_output_chars") or 4000)
    return total


def enqueue_task(
    kind: str,
    subject: dict[str, Any] | None,
    *,
    reason: str,
    trigger: dict[str, Any] | None = None,
    trading_date: str | None = None,
    priority: int | None = None,
    force: bool = False,
    config: dict[str, Any] | None = None,
    path: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    config = config or load_config()
    kind_cfg = _kind_config(kind, config)
    day = str(trading_date or date.today().isoformat())[:10]
    key = subject_key(subject)
    task_id = make_task_id(kind, key, day)
    created: dict[str, Any] = {}
    skip: dict[str, Any] = {}

    def _mutate(value: Any) -> list[dict[str, Any]]:
        tasks = list(value) if isinstance(value, list) else []
        for task in tasks:
            same_subject = (
                task.get("kind") == kind and task.get("subject_key") == key
            )
            if same_subject and task.get("status") in ACTIVE_STATUSES:
                skip.update({"reason": "already_active", "task_id": task.get("id")})
                return tasks
            if task.get("id") == task_id:
                skip.update({"reason": "duplicate_id", "task_id": task_id})
                return tasks
        cooldown = int(kind_cfg.get("cooldown_days") or 0)
        if not force and _in_cooldown(tasks, kind, key, day, cooldown):
            skip.update({"reason": "cooldown", "task_id": task_id})
            return tasks
        roles = {
            str(role): {"status": "pending", "attempts": 0}
            for role in kind_cfg.get("experts") or []
        }
        task = {
            "schema": TASK_SCHEMA,
            "id": task_id,
            "kind": kind,
            "subject": dict(subject or {}),
            "subject_key": key,
            "reason": reason,
            "trigger": dict(trigger or {}),
            "priority": int(
                priority if priority is not None else kind_cfg.get("priority", 50)
            ),
            "trading_date": day,
            "status": "pending",
            "created_at": _now_text(now),
            "expert_plan": list(kind_cfg.get("experts") or []),
            "roles": roles,
            "evidence_pack_ref": None,
            "budget": {
                "estimated_chars": estimate_task_chars(kind, config),
                "reserved": False,
            },
            "verdict": None,
        }
        tasks.append(task)
        created.update(task)
        return tasks

    mutate_json(path or queue_file(), _mutate, [])
    if created:
        return {"enqueued": True, "task": created}
    return {"enqueued": False, **skip}


def _expire_stale_claims(
    task: dict[str, Any],
    current: datetime,
    ttl_minutes: int,
    max_attempts: int,
) -> None:
    expiry = current - timedelta(minutes=max(1, ttl_minutes))
    for state in (task.get("roles") or {}).values():
        if state.get("status") != "claimed":
            continue
        try:
            claimed_at = datetime.fromisoformat(str(state.get("claimed_at")))
        except (TypeError, ValueError):
            claimed_at = datetime.min.replace(tzinfo=current.tzinfo)
        if claimed_at.tzinfo is None and current.tzinfo is not None:
            # Claims written before timezone-aware timestamps were introduced
            # used local wall-clock time. Preserve that interpretation during
            # lease migration so old work can still expire deterministically.
            claimed_at = claimed_at.replace(tzinfo=current.tzinfo)
        elif claimed_at.tzinfo is not None and current.tzinfo is None:
            current = current.replace(tzinfo=claimed_at.tzinfo)
            expiry = current - timedelta(minutes=max(1, ttl_minutes))
        if claimed_at <= expiry:
            attempts = int(state.get("attempts") or 0)
            exhausted = attempts >= max_attempts
            state.update({
                "status": "failed" if exhausted else "pending",
                "last_error": "claim lease expired",
            })
            state.pop("claimed_by", None)
            state.pop("claimed_at", None)
            state.pop("claim_id", None)
            if exhausted:
                state["failed_at"] = current.isoformat(timespec="seconds")


def _task_has_failed_role(task: dict[str, Any]) -> bool:
    return any(
        state.get("status") == "failed"
        for state in (task.get("roles") or {}).values()
        if isinstance(state, dict)
    )


def _finding_hash(finding: dict[str, Any]) -> str:
    import hashlib

    payload = json.dumps(
        finding, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _finding_submission_path(
    task_id: str,
    role: str,
    claim_id: str | None,
    finding_hash: str,
) -> str:
    claim_key = _safe(claim_id or "unfenced")
    filename = f"{_safe(role)}-{claim_key}-{finding_hash}.json"
    return os.path.join(board_dir(task_id), "submissions", filename)


def _budget_remaining(trading_date: str, config: dict[str, Any]) -> int:
    budget_cfg = config.get("budget") or {}
    cap = int(budget_cfg.get("daily_char_budget") or 0)
    if cap <= 0:
        return 1 << 60
    usage = read_json(budget_file(trading_date), {})
    reserved = int((usage or {}).get("reserved_chars") or 0)
    return cap - reserved


def reserve_budget(
    task_id: str,
    estimated_chars: int,
    trading_date: str,
    *,
    config: dict[str, Any] | None = None,
    now: str | None = None,
) -> bool:
    config = config or load_config()
    cap = int((config.get("budget") or {}).get("daily_char_budget") or 0)
    granted: list[bool] = []

    def _mutate(value: Any) -> dict[str, Any]:
        usage = value if isinstance(value, dict) else {}
        entries = list(usage.get("entries") or [])
        if any(entry.get("task_id") == task_id for entry in entries):
            granted.append(True)
            return usage
        reserved = int(usage.get("reserved_chars") or 0)
        if cap > 0 and reserved + estimated_chars > cap:
            granted.append(False)
            return usage
        entries.append({
            "task_id": task_id,
            "estimated_chars": estimated_chars,
            "at": _now_text(now),
        })
        granted.append(True)
        return {
            "schema": "research_budget_v1",
            "date": trading_date,
            "char_budget": cap,
            "reserved_chars": reserved + estimated_chars,
            "entries": entries,
        }

    mutate_json(budget_file(trading_date), _mutate, {})
    return bool(granted and granted[0])


def record_usage(
    task_id: str,
    actual_chars: int,
    trading_date: str,
    *,
    now: str | None = None,
) -> None:
    def _mutate(value: Any) -> dict[str, Any]:
        usage = value if isinstance(value, dict) else {}
        actuals = list(usage.get("actuals") or [])
        actuals.append({
            "task_id": task_id,
            "actual_chars": actual_chars,
            "at": _now_text(now),
        })
        usage["actuals"] = actuals
        return usage

    mutate_json(budget_file(trading_date), _mutate, {})


def _claimable_role(
    task: dict[str, Any],
    roles: list[str] | None,
    max_attempts: int,
) -> str | None:
    for role in task.get("expert_plan") or []:
        if roles and role not in roles:
            continue
        state = (task.get("roles") or {}).get(role) or {}
        if (
            state.get("status") == "pending"
            and int(state.get("attempts") or 0) < max_attempts
        ):
            return role
    return None


def _reserve_claim_budget(
    task: dict[str, Any],
    config: dict[str, Any],
    now: str | None,
) -> bool:
    budget = task.get("budget") or {}
    if budget.get("reserved"):
        return True
    granted = reserve_budget(
        str(task.get("id")),
        int(budget.get("estimated_chars") or 0),
        str(task.get("trading_date") or date.today().isoformat()),
        config=config,
        now=_now_text(now),
    )
    if not granted:
        task["deferred_reason"] = "daily_budget_exhausted"
        return False
    budget["reserved"] = True
    task["budget"] = budget
    task.pop("deferred_reason", None)
    return True


def _claim_work_mutation(
    value: Any,
    *,
    worker: str,
    roles: list[str] | None,
    config: dict[str, Any],
    current: datetime,
    ttl_minutes: int,
    max_attempts: int,
    now: str | None,
    claimed: dict[str, Any],
) -> list[dict[str, Any]]:
    tasks = list(value) if isinstance(value, list) else []
    for task in tasks:
        if task.get("status") in ("pending", "in_progress"):
            _expire_stale_claims(task, current, ttl_minutes, max_attempts)
            if _task_has_failed_role(task):
                task["status"] = "failed"
    ordered = sorted(
        (task for task in tasks if task.get("status") in ("pending", "in_progress")),
        key=lambda item: (-int(item.get("priority") or 0), item.get("created_at") or ""),
    )
    for task in ordered:
        role = _claimable_role(task, roles, max_attempts)
        if role is None or not _reserve_claim_budget(task, config, now):
            continue
        state = task["roles"].setdefault(role, {"status": "pending", "attempts": 0})
        claim_id = uuid.uuid4().hex
        state.update({
            "status": "claimed",
            "claimed_by": worker,
            "claimed_at": _now_text(now),
            "claim_id": claim_id,
            "attempts": int(state.get("attempts") or 0) + 1,
        })
        task["status"] = "in_progress"
        claimed.update({
            "task": json.loads(json.dumps(task)),
            "role": role,
            "claim_id": claim_id,
        })
        break
    return tasks


def _record_claim_turn(
    claimed: dict[str, Any],
    worker: str,
    now: str | None,
) -> None:
    task = claimed["task"]
    role = str(claimed.get("role"))
    append_turn_event(str(task.get("id")), {
        "event_type": "research.turn.claimed",
        "task_id": task.get("id"),
        "role": claimed.get("role"),
        "worker": worker,
        "claim_id": claimed.get("claim_id"),
        "attempt": (task.get("roles") or {}).get(role, {}).get("attempts"),
        "at": _now_text(now),
    })


def claim_next_work(
    worker: str,
    *,
    roles: list[str] | None = None,
    config: dict[str, Any] | None = None,
    path: str | None = None,
    now: str | None = None,
) -> dict[str, Any] | None:
    config = config or load_config()
    claimed: dict[str, Any] = {}
    mutate_json(
        path or queue_file(),
        partial(
            _claim_work_mutation,
            worker=worker,
            roles=roles,
            config=config,
            current=datetime.fromisoformat(_now_text(now)),
            ttl_minutes=int(config.get("claim_ttl_minutes") or 120),
            max_attempts=int(config.get("max_attempts_per_role") or 2),
            now=now,
            claimed=claimed,
        ),
        [],
    )
    if claimed:
        _record_claim_turn(claimed, worker, now)
    return claimed or None


def validate_finding(
    finding: Any,
    *,
    task: dict[str, Any],
    role: str,
    config: dict[str, Any] | None = None,
    claim_id: str | None = None,
    worker: str | None = None,
    now: str | None = None,
) -> list[str]:
    config = config or load_config()
    limits = config.get("finding") or {}
    errors: list[str] = []
    if not isinstance(finding, dict):
        return ["finding must be a JSON object"]
    try:
        from agent_run_contract import research_only_breach
    except ImportError:
        research_only_breach = None
    if research_only_breach:
        breach = research_only_breach(finding)
        if breach:
            errors.append(breach)
    if finding.get("schema") != FINDING_SCHEMA:
        errors.append(f"schema must be {FINDING_SCHEMA}")
    if finding.get("task_id") != task.get("id"):
        errors.append("task_id does not match the claimed task")
    if finding.get("role") != role:
        errors.append("role does not match the claimed role")
    stance = finding.get("stance")
    if stance not in STANCES:
        errors.append(f"stance must be one of {sorted(STANCES)}")
    confidence = finding.get("confidence", 1.0 if stance == "abstain" else None)
    if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
        errors.append("confidence must be a number in [0, 1]")
    summary = finding.get("summary")
    max_summary = int(limits.get("max_summary_chars") or 600)
    if not isinstance(summary, str) or not summary.strip():
        errors.append("summary is required")
    elif len(summary) > max_summary:
        errors.append(f"summary exceeds {max_summary} chars")
    errors.extend(_validate_stance_fields(finding, stance, role=role))
    if not errors and stance != "abstain":
        errors.extend(
            _validate_bound_evidence(
                finding,
                task=task,
                role=role,
                claim_id=claim_id,
                worker=worker,
                config=config,
                now=now,
            )
        )
    max_chars = int(limits.get("max_finding_chars") or 10000)
    if len(json.dumps(finding, ensure_ascii=False)) > max_chars:
        errors.append(f"finding exceeds {max_chars} chars")
    if not errors and stance != "abstain":
        errors.extend(_validate_kind_specific(finding, task=task))
    return errors


def _validate_bound_evidence(
    finding: dict[str, Any],
    *,
    task: dict[str, Any],
    role: str,
    claim_id: str | None,
    worker: str | None,
    config: dict[str, Any],
    now: str | None,
) -> list[str]:
    if not bool((config.get("finding") or {}).get("require_bound_evidence")):
        return []
    ref = str(task.get("evidence_pack_ref") or "")
    if not ref:
        return ["evidence_pack_ref is required"]
    try:
        import agent_evidence
        import evidence_pack
    except ImportError:
        return ["evidence integrity validator unavailable"]
    pack = evidence_pack.load_pack(ref)
    if not pack:
        return ["artifact_hash_mismatch"]
    errors = agent_evidence.validate_reference_paths(
        pack,
        finding.get("evidence_refs") or [],
    )
    if bool((config.get("finding") or {}).get("require_model_run_manifest")):
        errors.extend(
            agent_evidence.validate_finding_manifest(
                finding.get("model_run_manifest"),
                evidence_pack=pack,
                evidence_refs=finding.get("evidence_refs") or [],
                tool_inputs=finding.get("tool_inputs") or {},
                finding=finding,
                task_id=str(task.get("id") or ""),
                role=role,
                claim_id=str(claim_id or ""),
                submitter=str(worker or ""),
                require_execution_eligible=False,
                now=now or datetime.now().astimezone().isoformat(timespec="seconds"),
                max_age_minutes=int(
                    (config.get("finding") or {}).get("manifest_max_age_minutes")
                    or 10
                ),
            )
        )
    return list(dict.fromkeys(errors))


def _validate_kind_specific(
    finding: dict[str, Any],
    *,
    task: dict[str, Any],
) -> list[str]:
    """Per-kind fail-closed submit checks beyond the generic schema.

    ``serenity_refresh`` findings preserve the fail-closed contract that used
    to live in ``serenity_refresh_queue.complete_request``: a finding is only
    accepted once a fresh ``deep_research_cache`` entry (asof not older than
    the task's trading_date) exists for the subject code. This stops a
    deep_researcher turn from marking the task done without actually writing
    real research back to the cache.
    """
    if task.get("kind") != "serenity_refresh":
        return []
    code = str((task.get("subject") or {}).get("code") or "")
    if not code:
        return []
    try:
        from deep_research_cache import read_deep_research
    except ImportError:
        return []
    trading_date = str(task.get("trading_date") or "")
    cache = read_deep_research(code, today=trading_date)
    if not cache:
        return ["fresh deep-research cache not found for serenity_refresh subject"]
    if str(cache.get("asof") or "") < trading_date:
        return ["deep-research cache predates the serenity_refresh task"]
    return []


def _validate_stance_fields(
    finding: dict[str, Any], stance: Any, *, role: str | None = None,
) -> list[str]:
    errors: list[str] = []

    def _non_empty_list(field: str) -> bool:
        value = finding.get(field)
        return isinstance(value, list) and len(value) > 0

    if stance == "abstain":
        reason = finding.get("abstain_reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append("abstain_reason is required when stance=abstain")
        return errors
    if role == "adjudicator" and stance not in {"support", "oppose"}:
        errors.append("adjudicator stance must be support or oppose")
    if role == "adjudicator" and not _non_empty_list("adjudication_points"):
        errors.append("adjudication_points is required for adjudicator")
    if role == "risk_neutral":
        suggestion = str(finding.get("strategy_suggestion") or "")
        if suggestion not in {"normal", "scaled", "hedged", "watch"}:
            errors.append(
                "strategy_suggestion must be normal, scaled, hedged or watch"
            )
    if not _non_empty_list("evidence_refs"):
        errors.append("evidence_refs must be a non-empty list")
    if stance == "support":
        if not _non_empty_list("counterevidence"):
            errors.append("counterevidence is required when stance=support")
        if not _non_empty_list("invalidation_conditions"):
            errors.append("invalidation_conditions is required when stance=support")
    return errors


def _claim_fence_error(
    state: dict[str, Any],
    role: str,
    *,
    require_fencing: bool,
    claim_id: str | None,
    worker: str | None,
    allow_unowned_worker: bool = False,
) -> str | None:
    if state.get("status") != "claimed":
        return f"role {role} is not claimed"
    if require_fencing and not claim_id:
        return "claim_id is required"
    if require_fencing and state.get("claim_id") != claim_id:
        return "claim_id does not match the active lease"
    valid_workers = (None, worker) if allow_unowned_worker else (worker,)
    if worker is not None and state.get("claimed_by") not in valid_workers:
        return f"role {role} is claimed by another worker"
    return None


def _completed_submission_result(
    item: dict[str, Any],
    state: dict[str, Any],
    role: str,
    *,
    require_fencing: bool,
    claim_id: str | None,
    worker: str | None,
    finding_hash: str,
) -> dict[str, Any]:
    same_submission = (
        require_fencing
        and claim_id
        and state.get("submission_claim_id") == claim_id
        and state.get("submission_worker") == worker
        and state.get("finding_hash") == finding_hash
    )
    if not same_submission:
        return {"ok": False, "errors": [f"role {role} is already completed"]}
    return {
        "ok": True,
        "idempotent": True,
        "task_status": item.get("status"),
        "all_roles_done": item.get("status") == "ready_to_synthesize",
        "board_path": state.get("finding_path"),
    }


def _publish_finding(
    item: dict[str, Any],
    state: dict[str, Any],
    role: str,
    finding: dict[str, Any],
    *,
    board_path: str,
    submission_path: str,
    claim_id: str | None,
    worker: str | None,
    finding_hash: str,
    now: str | None,
) -> dict[str, Any]:
    atomic_write_json(board_path, finding)
    state.update({
        "status": "done",
        "finding_path": board_path,
        "finding_content_path": submission_path,
        "completed_at": _now_text(now),
        "submission_claim_id": claim_id,
        "submission_worker": worker,
        "finding_hash": finding_hash,
    })
    for field in ("claim_id", "claimed_by", "claimed_at"):
        state.pop(field, None)
    item["roles"][role] = state
    statuses = {
        str(entry.get("status"))
        for entry in (item.get("roles") or {}).values()
    }
    if statuses <= {"done"}:
        item["status"] = "ready_to_synthesize"
    return {
        "ok": True,
        "task_status": item.get("status"),
        "all_roles_done": statuses <= {"done"},
    }


def _submit_finding_mutation(
    value: Any,
    *,
    task_id: str,
    role: str,
    finding: dict[str, Any],
    board_path: str,
    submission_path: str,
    claim_id: str | None,
    worker: str | None,
    finding_hash: str,
    require_fencing: bool,
    now: str | None,
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    tasks = list(value) if isinstance(value, list) else []
    for item in tasks:
        if item.get("id") != task_id:
            continue
        state = (item.get("roles") or {}).get(role) or {}
        if state.get("status") == "done":
            result.update(_completed_submission_result(
                item,
                state,
                role,
                require_fencing=require_fencing,
                claim_id=claim_id,
                worker=worker,
                finding_hash=finding_hash,
            ))
            return tasks
        fence_error = _claim_fence_error(
            state,
            role,
            require_fencing=require_fencing,
            claim_id=claim_id,
            worker=worker,
        )
        if fence_error:
            result.update({"ok": False, "errors": [fence_error]})
            return tasks
        result.update(_publish_finding(
            item,
            state,
            role,
            finding,
            board_path=board_path,
            submission_path=submission_path,
            claim_id=claim_id,
            worker=worker,
            finding_hash=finding_hash,
            now=now,
        ))
        return tasks
    return tasks


def _record_submitted_finding(
    task: dict[str, Any],
    role: str,
    finding: dict[str, Any],
    *,
    worker: str | None,
    claim_id: str | None,
    finding_hash: str,
    now: str | None,
) -> None:
    task_id = str(task.get("id"))
    record_usage(
        task_id,
        len(json.dumps(finding, ensure_ascii=False)),
        str(task.get("trading_date") or date.today().isoformat()),
        now=now,
    )
    append_turn_event(task_id, {
        "event_type": "research.turn.submitted",
        "task_id": task_id,
        "role": role,
        "worker": worker,
        "claim_id": claim_id,
        "finding_hash": finding_hash,
        "at": _now_text(now),
    })


def submit_finding(
    task_id: str,
    role: str,
    finding: dict[str, Any],
    *,
    worker: str | None = None,
    claim_id: str | None = None,
    config: dict[str, Any] | None = None,
    path: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    config = config or load_config()
    task = find_task(task_id, path)
    if not task:
        return {"ok": False, "errors": ["task not found"]}
    initial_state = (task.get("roles") or {}).get(role) or {}
    expected_worker = worker
    if worker is None:
        worker_field = "submission_worker" if initial_state.get("status") == "done" else "claimed_by"
        expected_worker = initial_state.get(worker_field)
    finding_hash = _finding_hash(finding)
    errors = validate_finding(
        finding,
        task=task,
        role=role,
        config=config,
        claim_id=claim_id,
        worker=expected_worker,
        now=now,
    )
    if errors:
        return {"ok": False, "errors": errors}
    board_path = os.path.join(board_dir(task_id), f"{_safe(role)}.json")
    submission_path = _finding_submission_path(task_id, role, claim_id, finding_hash)
    atomic_write_json(submission_path, finding)
    result: dict[str, Any] = {}
    mutate_json(
        path or queue_file(),
        partial(
            _submit_finding_mutation,
            task_id=task_id,
            role=role,
            finding=finding,
            board_path=board_path,
            submission_path=submission_path,
            claim_id=claim_id,
            worker=expected_worker,
            finding_hash=finding_hash,
            require_fencing=bool(config.get("require_claim_fencing", True)),
            now=now,
            result=result,
        ),
        [],
    )
    if not result:
        return {"ok": False, "errors": ["task disappeared during submit"]}
    if not result.get("ok") or result.get("idempotent"):
        return result
    _record_submitted_finding(
        task,
        role,
        finding,
        worker=expected_worker,
        claim_id=claim_id,
        finding_hash=finding_hash,
        now=now,
    )
    return {"board_path": board_path, **result}


def _fail_role_mutation(
    value: Any,
    *,
    task_id: str,
    role: str,
    error: str,
    retry: bool,
    worker: str | None,
    claim_id: str | None,
    require_fencing: bool,
    max_attempts: int,
    now: str | None,
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    tasks = list(value) if isinstance(value, list) else []
    for item in tasks:
        if item.get("id") != task_id:
            continue
        state = (item.get("roles") or {}).get(role)
        if not isinstance(state, dict):
            result.update({"ok": False, "errors": [f"unknown role {role}"]})
            return tasks
        fence_error = _claim_fence_error(
            state,
            role,
            require_fencing=require_fencing,
            claim_id=claim_id,
            worker=worker,
            allow_unowned_worker=True,
        )
        if fence_error:
            result.update({"ok": False, "errors": [fence_error]})
            return tasks
        attempts = int(state.get("attempts") or 0)
        state.update({
            "status": "failed" if (attempts >= max_attempts or not retry) else "pending",
            "last_error": str(error)[:500],
            "failed_at": _now_text(now),
        })
        for field in ("claimed_by", "claimed_at", "claim_id"):
            state.pop(field, None)
        if state["status"] == "failed":
            item["status"] = "failed"
        result.update({"ok": True, "role_status": state["status"]})
        return tasks
    return tasks


def _record_failed_turn(
    task_id: str,
    role: str,
    error: str,
    *,
    retry: bool,
    worker: str | None,
    claim_id: str | None,
    role_status: str | None,
    now: str | None,
) -> None:
    append_turn_event(task_id, {
        "event_type": "research.turn.failed",
        "task_id": task_id,
        "role": role,
        "worker": worker,
        "claim_id": claim_id,
        "error": str(error)[:500],
        "retry": retry,
        "role_status": role_status,
        "at": _now_text(now),
    })


def fail_role(
    task_id: str,
    role: str,
    error: str,
    *,
    retry: bool = True,
    worker: str | None = None,
    claim_id: str | None = None,
    config: dict[str, Any] | None = None,
    path: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    config = config or load_config()
    result: dict[str, Any] = {}
    mutate_json(
        path or queue_file(),
        partial(
            _fail_role_mutation,
            task_id=task_id,
            role=role,
            error=error,
            retry=retry,
            worker=worker,
            claim_id=claim_id,
            require_fencing=bool(config.get("require_claim_fencing", True)),
            max_attempts=int(config.get("max_attempts_per_role") or 2),
            now=now,
            result=result,
        ),
        [],
    )
    if result.get("ok"):
        _record_failed_turn(
            task_id,
            role,
            error,
            retry=retry,
            worker=worker,
            claim_id=claim_id,
            role_status=result.get("role_status"),
            now=now,
        )
    return result or {"ok": False, "errors": ["task not found"]}


def update_task(
    task_id: str,
    fields: dict[str, Any],
    *,
    path: str | None = None,
) -> dict[str, Any] | None:
    updated: dict[str, Any] = {}

    def _mutate(value: Any) -> list[dict[str, Any]]:
        tasks = list(value) if isinstance(value, list) else []
        for item in tasks:
            if item.get("id") == task_id:
                item.update(fields)
                updated.update(item)
                break
        return tasks

    mutate_json(path or queue_file(), _mutate, [])
    return updated or None


def append_ledger_event(event: dict[str, Any], *, path: str | None = None) -> None:
    from state_store import file_lock

    target = path or ledger_file()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, default=str)
    with file_lock(target):
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def append_turn_event(task_id: str, event: dict[str, Any]) -> None:
    """Append a claim lifecycle event without ever rewriting prior turns."""
    from state_store import file_lock

    target = turns_file(task_id)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    payload = {"event_id": uuid.uuid4().hex, **dict(event)}
    line = json.dumps(payload, ensure_ascii=False, default=str)
    with file_lock(target):
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def queue_summary(path: str | None = None) -> dict[str, Any]:
    tasks = load_tasks(path)
    by_status: dict[str, int] = {}
    for task in tasks:
        status = str(task.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
    return {
        "total": len(tasks),
        "by_status": by_status,
        "active": [
            {
                "id": task.get("id"),
                "kind": task.get("kind"),
                "status": task.get("status"),
                "priority": task.get("priority"),
                "roles": {
                    role: state.get("status")
                    for role, state in (task.get("roles") or {}).items()
                },
            }
            for task in tasks
            if task.get("status") in ACTIVE_STATUSES
        ],
    }
