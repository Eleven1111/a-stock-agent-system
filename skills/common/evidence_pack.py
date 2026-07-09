"""Deterministic, token-bounded evidence packs for research-plane experts.

One pack is built per research task and shared by every expert role on that
task. The builder only reads facts that already exist (agent state, cron
artifacts, candidate pool, deep-research cache), applies a hard character
budget with a deterministic reduction sequence, and content-addresses the
result so identical inputs reuse the cached pack. Experts never read raw
state; they read the pack.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from agent_state import load_agent_state
from paths import data_file
from runtime_context import load_latest_artifact
from state_store import atomic_write_json, read_json


PACK_SCHEMA = "research_evidence_pack_v1"
BUILDER_VERSION = "evidence_pack_v1"

DEFAULT_SECTION_LIMITS = {
    "agent_state_chars": 6000,
    "artifact_chars": 1500,
    "artifact_excerpt_chars": 1200,
    "subject_data_chars": 6000,
    "max_artifacts": 6,
    "recent_recommendations": 3,
    "recent_signals": 3,
}

_REC_FIELDS = (
    "code", "name", "date", "action", "grade", "confidence",
    "outcome", "strategy_id",
)
_SIGNAL_FIELDS = (
    "code", "name", "date", "signal_date", "action", "grade",
    "settlement_status", "t1_return_pct", "t3_return_pct", "outcome",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _size(value: Any) -> int:
    return len(_canonical(value))


def _fit(value: Any, max_chars: int) -> Any:
    text = _canonical(value)
    if len(text) <= max_chars:
        return value
    return {"_truncated": True, "_preview": text[:max_chars]}


def _norm_code(value: Any) -> str:
    code = str(value or "").strip()
    return code.zfill(6) if code.isdigit() and code else code


def _pick(record: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {key: record[key] for key in fields if record.get(key) is not None}


def _slice_agent_state(
    state: dict[str, Any] | None,
    subject_code: str,
    limits: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(state, dict):
        return None
    recommendations = [
        rec for rec in state.get("recommendations") or []
        if isinstance(rec, dict)
    ]
    signals = [
        sig for sig in state.get("signals") or []
        if isinstance(sig, dict)
    ]
    positions = [
        pos for pos in (state.get("portfolio") or {}).get("positions") or []
        if isinstance(pos, dict)
    ]
    subject_recs = [
        rec for rec in recommendations
        if _norm_code(rec.get("code")) == subject_code
    ]
    recent = int(limits.get("recent_recommendations") or 3)
    picked_recs = subject_recs or sorted(
        recommendations,
        key=lambda rec: str(rec.get("date") or ""),
        reverse=True,
    )[:recent]
    subject_signals = [
        sig for sig in signals
        if _norm_code(sig.get("code")) == subject_code
    ][-int(limits.get("recent_signals") or 3):]
    subject_position = next(
        (pos for pos in positions if _norm_code(pos.get("code")) == subject_code),
        None,
    )
    sliced = {
        "generated_at": state.get("generated_at"),
        "position_count": len(positions),
        "subject_position": _pick(subject_position, ("code", "name"))
        if subject_position else None,
        "recommendations": [_pick(rec, _REC_FIELDS) for rec in picked_recs],
        "subject_signals": [_pick(sig, _SIGNAL_FIELDS) for sig in subject_signals],
        "signal_count": len(signals),
        "behavior_risk": state.get("behavior_risk"),
        "pending_settlement_count": len(state.get("pending_settlements") or []),
    }
    return _fit(sliced, int(limits.get("agent_state_chars") or 6000))


def _artifact_entry(
    job_id: str,
    trading_date: str,
    limits: dict[str, Any],
) -> dict[str, Any]:
    artifact = load_latest_artifact(job_id)
    if not artifact:
        return {"job_id": job_id, "missing": True}
    entry: dict[str, Any] = {
        "job_id": job_id,
        "trading_date": artifact.get("trading_date"),
        "status": artifact.get("status"),
        "finished_at": artifact.get("finished_at"),
        "summary": artifact.get("summary") or {},
    }
    if artifact.get("trading_date") not in (None, trading_date):
        entry["stale"] = True
    excerpt = str(artifact.get("stdout_tail") or "").strip()
    if excerpt:
        entry["stdout_excerpt"] = excerpt[
            : int(limits.get("artifact_excerpt_chars") or 1200)
        ]
    return _fit(entry, int(limits.get("artifact_chars") or 1500))


def _deep_research_gap(
    cache: dict[str, Any] | None,
) -> str | None:
    """Return why deep research coverage is insufficient, or None if fresh."""
    if not isinstance(cache, dict):
        return "missing"
    if cache.get("stale"):
        return "stale"
    return None


def _maybe_pull_serenity_refresh(
    *,
    task: dict[str, Any],
    subject_code: str,
    gap_reason: str,
) -> str | None:
    """Demand-pull hook (§6b): enqueue a serenity_refresh when a
    candidate_deep_dive pack finds deep-research coverage missing/stale.

    Only fires for candidate_deep_dive packs (the "复核拉动深研" demand-pull
    the plan describes) — other kinds (anomaly_review, postmortem,
    user_request, and serenity_refresh itself) never trigger this hook.
    Guarded against recursion: a serenity_refresh task's own pack build must
    never enqueue another serenity_refresh. Idempotent/dedup is inherited
    from research_bus.enqueue_task (active-task + cooldown checks).
    """
    if task.get("kind") != "candidate_deep_dive":
        return None
    try:
        import research_bus
    except ImportError:
        return None
    reason = f"deep_research_{gap_reason}_in_pack"
    outcome = research_bus.enqueue_task(
        "serenity_refresh",
        {"code": subject_code, "name": (task.get("subject") or {}).get("name")},
        reason=reason,
        trigger={
            "source": "evidence_pack.build_pack",
            "origin_task_id": task.get("id"),
            "origin_kind": task.get("kind"),
        },
        trading_date=str(task.get("trading_date") or ""),
        priority=task.get("priority"),
        config=research_bus.load_config(),
    )
    if outcome.get("enqueued"):
        research_bus.append_ledger_event({
            "event_type": "research.enqueued",
            "task_id": outcome["task"]["id"],
            "kind": "serenity_refresh",
            "reason": reason,
            "trading_date": str(task.get("trading_date") or ""),
        })
    return reason


_NEWS_FIELDS = (
    "title", "url", "source_name", "source_rank", "materiality",
    "time_window", "graded_at",
)
DEFAULT_NEWS_LOOKBACK_DAYS = 7
DEFAULT_NEWS_MAX_ITEMS = 8


def _news_sectors_for_subject(subject_code: str) -> list[str]:
    """Sector labels for the subject, read from the already-loaded candidate pool.

    Reuses the candidate pool's own ``sector``/``industry`` fields (already
    resolved by ``sector_taxonomy``/``industry_map`` upstream) instead of
    re-deriving sector membership here, so this stays a thin read.
    """
    pool = read_json(data_file("stock-triage", "candidate_pool_latest.json"), {})
    sectors: list[str] = []
    for candidate in (pool or {}).get("candidates") or []:
        if _norm_code((candidate or {}).get("code")) != subject_code:
            continue
        for key in ("sector", "industry"):
            value = str((candidate or {}).get(key) or "").strip()
            if value:
                sectors.append(value)
        break
    return sectors


def _news_evidence(subject_code: str) -> dict[str, Any] | None:
    """近 N 天 L2 已评级资讯：直接点名该股 或 命中其所属板块。

    fail-open: 资讯池为空/不可达时证据包仍照常生成，但 ``status`` 必须显式
    标注（``empty``/``unavailable``），绝不静默缺席这一段；资讯只作为证据
    附加，不改变候选排序或信号（news_pipeline.read_graded_news 是纯读取，
    评级本身不参与打分）。
    """
    if not subject_code:
        return None
    sectors = _news_sectors_for_subject(subject_code)
    try:
        import news_pipeline

        result = news_pipeline.read_graded_news(
            code=subject_code,
            sectors=sectors,
            days=DEFAULT_NEWS_LOOKBACK_DAYS,
            limit=DEFAULT_NEWS_MAX_ITEMS,
        )
    except Exception:  # noqa: BLE001 - news pool must never block a pack
        return {"status": "unavailable", "items": []}
    items = [
        _pick(item, _NEWS_FIELDS)
        for item in (result.get("items") or [])
        if isinstance(item, dict)
    ]
    return {"status": result.get("status") or "unavailable", "items": items}


_QA_FIELDS = ("date", "question", "reply", "has_reply", "platform", "url")


def _interactive_qa_evidence(subject_code: str) -> dict[str, Any] | None:
    """投资者关注热点: recent investor Q&A, graded per source_grading.md.

    A company reply is a traceable official statement (grade B supporting
    evidence); the investor question alone is only an attention/lead signal
    and must never be cited as a fact. ``status`` surfaces the adapter's
    fail-closed/best-effort outcome (e.g. ``sse_unavailable``) so a missing
    section reads as "not fetched", never as "no investor attention".
    """
    try:
        from stock_intelligence import read_interactive_qa
    except Exception:  # noqa: BLE001 - optional section, never blocks a pack
        return None
    result = read_interactive_qa(subject_code)
    if not result.get("available"):
        return {"status": result.get("status") or "missing", "items": []}
    items = []
    for row in result.get("rows") or []:
        picked = {key: row[key] for key in _QA_FIELDS if row.get(key) is not None}
        picked["grade"] = "B" if row.get("has_reply") else "attention_only"
        items.append(picked)
    return {
        "status": result.get("status"),
        "market": result.get("market"),
        "asof": result.get("asof"),
        "items": items,
    }


def _current_regime(trading_date: str) -> str | None:
    """Best-effort current emotion-temperature tier for pack regime filtering.

    Never raises and never blocks a pack: any failure (missing context, stale
    date) yields ``None`` which means "show all applicable packs", not "no
    signal". This is read-only and does not influence ranking.
    """
    try:
        from market_temperature import read_temperature

        tier = str((read_temperature(event_asof=trading_date or None) or {}).get("tier") or "")
    except Exception:  # noqa: BLE001 - optional, explanation-only section
        return None
    # "neutral" means the temperature calculator has no usable signal — treat it
    # as "no regime filter" (show all applicable packs), not as its own regime.
    if not tier or tier == "neutral":
        return None
    return tier


def _strategy_pack_hints(
    candidate_entry: Any,
    trading_date: str,
) -> dict[str, Any] | None:
    """Interpretation-only ``strategy_pack_hints`` section.

    Reports which declarative strategy packs' judgement criteria the candidate
    hits (with per-condition hit/miss reasons). PURELY EXPLANATORY: it never
    changes ranking, scoring, or signals — the ``advisory_delta`` values are
    surfaced for narration and must not be folded into any live score. Returns
    ``None`` when there is no candidate evidence to interpret.
    """
    if not isinstance(candidate_entry, dict):
        return None
    try:
        import strategy_packs

        regime = _current_regime(trading_date)
        hints = strategy_packs.evaluate_pack_hints(candidate_entry, regime=regime)
    except Exception:  # noqa: BLE001 - explanation-only, never blocks a pack build
        return None
    if not hints:
        return None
    # Compact projection: full interpretation/explanation text lives in the
    # pack files; the evidence pack ships only ids, hit/miss and miss reasons
    # so three subject_data sections keep fitting the shared char budget.
    compact_packs = [
        {
            "pack": hint["pack"],
            "display_name": hint["display_name"],
            "category": hint["category"],
            "hit_count": hint["hit_count"],
            "condition_count": hint["condition_count"],
            "advisory_delta": hint["advisory_delta"],
            "influences_live_ranking": False,
            "conditions": [
                {key: cond[key] for key in ("id", "hit", "reason") if key in cond}
                for cond in hint["conditions"]
            ],
        }
        for hint in hints
    ]
    return {
        "regime": _current_regime(trading_date),
        "influences_live_ranking": False,
        "note": "解释性策略假设，不影响实盘排序/评分/信号；升级需过 research_gate",
        "packs": compact_packs,
    }


def _subject_data(
    subject_code: str,
    trading_date: str,
    limits: dict[str, Any],
    *,
    task: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not subject_code:
        return None
    data: dict[str, Any] = {}
    pool = read_json(data_file("stock-triage", "candidate_pool_latest.json"), {})
    for candidate in (pool or {}).get("candidates") or []:
        if _norm_code((candidate or {}).get("code")) == subject_code:
            data["candidate_entry"] = candidate
            data["candidate_pool_date"] = (pool or {}).get("trading_date")
            break
    try:
        from deep_research_cache import read_deep_research

        cache = read_deep_research(subject_code, today=trading_date)
    except Exception:
        cache = None
    if isinstance(cache, dict):
        data["deep_research"] = {
            key: cache.get(key)
            for key in ("asof", "stale", "age_days", "scores", "summary")
            if cache.get(key) is not None
        }
    try:
        import theme_registry

        theme_stage = theme_registry.theme_stage_for_code(subject_code, asof=trading_date)
    except Exception:  # noqa: BLE001 - theme lookup must never block a pack
        theme_stage = None
    if theme_stage:
        data["theme_stage"] = theme_stage
    interactive_qa = _interactive_qa_evidence(subject_code)
    if interactive_qa is not None:
        data["interactive_qa"] = interactive_qa
    news_evidence = _news_evidence(subject_code)
    if news_evidence is not None:
        data["news_evidence"] = news_evidence
    pack_hints = _strategy_pack_hints(data.get("candidate_entry"), trading_date)
    if pack_hints is not None:
        data["strategy_pack_hints"] = pack_hints
    gap_reason = _deep_research_gap(cache)
    if gap_reason and task is not None:
        pulled_reason = _maybe_pull_serenity_refresh(
            task=task, subject_code=subject_code, gap_reason=gap_reason,
        )
        if pulled_reason:
            data["deep_research_gap"] = pulled_reason
    if not data:
        return None
    return _fit(data, int(limits.get("subject_data_chars") or 4000))


def _quality(
    payload: dict[str, Any],
    required_sections: list[str],
) -> dict[str, Any]:
    missing: list[str] = []
    degraded: list[str] = []
    if "agent_state" in required_sections and payload.get("agent_state") is None:
        missing.append("agent_state")
    artifacts = payload.get("fact_artifacts") or []
    present = [entry for entry in artifacts if not entry.get("missing")]
    if "fact_artifacts" in required_sections and not present:
        missing.append("fact_artifacts")
    absent = [entry.get("job_id") for entry in artifacts if entry.get("missing")]
    stale = [entry.get("job_id") for entry in present if entry.get("stale")]
    if absent:
        degraded.append(f"missing_artifacts:{','.join(map(str, absent))}")
    if stale:
        degraded.append(f"stale_artifacts:{','.join(map(str, stale))}")
    if missing:
        status = "insufficient"
    elif degraded:
        status = "degraded"
    else:
        status = "ok"
    return {"status": status, "missing": missing, "degraded": degraded}


def _reduce_to_budget(
    payload: dict[str, Any],
    budget_chars: int,
) -> tuple[dict[str, Any], list[str]]:
    reductions: list[str] = []

    def _over() -> bool:
        return _size(payload) > budget_chars

    if _over():
        for entry in payload.get("fact_artifacts") or []:
            entry.pop("stdout_excerpt", None)
        reductions.append("dropped_artifact_excerpts")
    if _over() and payload.get("subject_data") is not None:
        payload["subject_data"] = None
        reductions.append("dropped_subject_data")
    if _over():
        artifacts = payload.get("fact_artifacts") or []
        payload["fact_artifacts"] = artifacts[:3]
        reductions.append("kept_first_3_artifacts")
    if _over() and payload.get("agent_state") is not None:
        payload["agent_state"] = _fit(payload["agent_state"], 1500)
        reductions.append("truncated_agent_state")
    if _over():
        payload["fact_artifacts"] = [
            {"job_id": entry.get("job_id"), "status": entry.get("status")}
            for entry in payload.get("fact_artifacts") or []
        ]
        reductions.append("collapsed_artifacts")
    return payload, reductions


def build_pack(
    task: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    from research_bus import load_config, packs_dir

    config = config or load_config()
    kind_cfg = (config.get("task_kinds") or {}).get(str(task.get("kind"))) or {}
    limits = {**DEFAULT_SECTION_LIMITS, **(config.get("pack_sections") or {})}
    budget_chars = int(kind_cfg.get("pack_budget_chars") or 20000)
    required = list(kind_cfg.get("required_sections") or [])
    trading_date = str(task.get("trading_date") or "")
    code = _norm_code((task.get("subject") or {}).get("code"))

    jobs = list(kind_cfg.get("pack_jobs") or [])[
        : int(limits.get("max_artifacts") or 6)
    ]
    subject_data = _subject_data(code, trading_date, limits, task=task)
    deep_research_gap = (
        subject_data.get("deep_research_gap")
        if isinstance(subject_data, dict) else None
    )
    payload: dict[str, Any] = {
        "task_id": task.get("id"),
        "kind": task.get("kind"),
        "subject": task.get("subject") or {},
        "trading_date": trading_date,
        "agent_state": _slice_agent_state(load_agent_state(), code, limits),
        "fact_artifacts": [
            _artifact_entry(job_id, trading_date, limits) for job_id in jobs
        ],
        "subject_data": subject_data,
    }
    payload, reductions = _reduce_to_budget(payload, budget_chars)
    payload["quality"] = _quality(payload, required)
    if deep_research_gap:
        # Recorded even if _reduce_to_budget dropped subject_data under
        # pressure — the evidence pack must still surface "深度面证据缺失"
        # honestly on the task/pack metadata (§6b requirement).
        payload["quality"]["deep_research_gap"] = deep_research_gap

    digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    ref = f"sha256:{digest}"
    path = os.path.join(packs_dir(), f"{digest}.json")
    cached = os.path.exists(path)
    if not cached:
        atomic_write_json(path, {
            "schema": PACK_SCHEMA,
            "ref": ref,
            "builder_version": BUILDER_VERSION,
            "budget_chars": budget_chars,
            "size_chars": _size(payload),
            "reductions": reductions,
            "payload": payload,
        })
    return {
        "ref": ref,
        "path": path,
        "cached": cached,
        "quality": payload["quality"],
        "size_chars": _size(payload),
        "payload": payload,
    }


def load_pack(ref: str) -> dict[str, Any] | None:
    from research_bus import packs_dir

    digest = str(ref or "").removeprefix("sha256:")
    if not digest:
        return None
    value = read_json(os.path.join(packs_dir(), f"{digest}.json"), None)
    if not isinstance(value, dict) or value.get("schema") != PACK_SCHEMA:
        return None
    return value
