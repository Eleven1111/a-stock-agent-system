"""Deterministic L1 rule engine + L1/L2 queue management for the news pipeline.

L1 is pure rule scoring (source-rank weight + keyword hits) and dedup — no
model tokens spent. Items that pass L1 are appended to a bounded queue file
under ``$A_STOCK_STATE_HOME/skills/news-pipeline/data/l1_queue.json``. L2
(``scripts/news_grader.py``) claims batches from that queue, following the
same claim/submit/TTL shape as ``research_bus.claim_next_work`` /
``expert_runner.py`` so a model turn can pick this up with the same mental
model.

Dedup reuses ``novelty_gate.content_key`` (title normalization + subject hint
fingerprint) so the same underlying story reported by two sources, or the
same source polled twice, only enters the queue once.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from .novelty_gate import content_key
    from .paths import skill_data_dir
    from .state_store import atomic_write_json, mutate_json, read_json
except ImportError:  # pragma: no cover - script-style sys.path imports
    from novelty_gate import content_key  # type: ignore
    from paths import skill_data_dir  # type: ignore
    from state_store import atomic_write_json, mutate_json, read_json  # type: ignore


SKILL = "news-pipeline"
L1_QUEUE_SCHEMA = "news_l1_queue_v1"
L1_ENTRY_STATUSES = {"pending", "claimed", "graded", "expired"}
BJ = timezone(timedelta(hours=8))


def now_bj_iso() -> str:
    return datetime.now(BJ).isoformat(timespec="seconds")


def l1_queue_path() -> str:
    return os.path.join(skill_data_dir(SKILL), "l1_queue.json")


def l1_seen_path() -> str:
    return os.path.join(skill_data_dir(SKILL), "l1_seen.json")


def l1_runs_dir() -> str:
    return os.path.join(skill_data_dir(SKILL), "l1_runs")


# ---------------------------------------------------------------------------
# L1 rule scoring
# ---------------------------------------------------------------------------

def _default_l1_config() -> dict[str, Any]:
    return {
        "rank_weight": {"S5": 5, "S4": 4, "S3": 3, "S2": 2, "S1": 1, "S0": 0},
        "materiality_keywords": {"critical": [], "high": [], "medium": []},
        "min_title_len": 4,
        "generic_titles": [],
        "pass_threshold_score": 3,
        "queue_max_entries": 2000,
        "excerpt_max_chars": 200,
    }


def score_item(item: dict[str, Any], l1_config: dict[str, Any]) -> dict[str, Any] | None:
    """Score one collected item against the L1 rule set.

    Returns ``None`` when the item is out-of-bounds noise (title too short,
    generic nav chrome). Otherwise returns the item enriched with
    ``keyword_tier``, ``matched_keywords``, ``rank_weight``, ``l1_score`` and
    ``passed`` (bool, score >= pass_threshold_score).
    """
    cfg = {**_default_l1_config(), **(l1_config or {})}
    title = str(item.get("title") or "").strip()
    if len(title) < int(cfg["min_title_len"]):
        return None
    if title in set(cfg.get("generic_titles") or []):
        return None

    rank = str(item.get("source_rank") or "S0")
    rank_weight = int((cfg.get("rank_weight") or {}).get(rank, 0))

    keywords_cfg = cfg.get("materiality_keywords") or {}
    tier_scores = {"critical": 3, "high": 2, "medium": 1}
    best_tier = None
    matched: list[str] = []
    for tier, words in keywords_cfg.items():
        hits = [word for word in (words or []) if word in title]
        if hits:
            matched.extend(hits)
            if best_tier is None or tier_scores.get(tier, 0) > tier_scores.get(best_tier, 0):
                best_tier = tier

    keyword_score = tier_scores.get(best_tier, 0) if best_tier else 0
    l1_score = rank_weight + keyword_score
    passed = bool(matched) and l1_score >= int(cfg["pass_threshold_score"])

    excerpt_max = int(cfg.get("excerpt_max_chars") or 200)
    enriched = dict(item)
    enriched.update({
        "keyword_tier": best_tier,
        "matched_keywords": sorted(set(matched))[:10],
        "rank_weight": rank_weight,
        "l1_score": l1_score,
        "passed": passed,
        "excerpt": title[:excerpt_max],
    })
    return enriched


def run_l1_scan(
    collected: list[dict[str, Any]],
    l1_config: dict[str, Any],
) -> dict[str, Any]:
    """Score every collected item, returning passed/rejected splits (no I/O)."""
    scored = [score_item(item, l1_config) for item in collected]
    scored = [item for item in scored if item is not None]
    passed = [item for item in scored if item["passed"]]
    rejected = [item for item in scored if not item["passed"]]
    return {
        "scanned": len(collected),
        "scored": len(scored),
        "passed": passed,
        "rejected_count": len(rejected),
    }


# ---------------------------------------------------------------------------
# Dedup (reuses novelty_gate's content fingerprint, scoped to this pipeline)
# ---------------------------------------------------------------------------

def dedupe_items(
    items: list[dict[str, Any]],
    *,
    max_seen: int = 5000,
    seen_path: str | None = None,
    now: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Drop items whose content fingerprint has already been queued.

    Uses a dedicated seen-set (not the shared delivery-time novelty_gate
    cache) because L1 dedup happens far earlier in the pipeline, at
    collection time, and must not consume/share TTL slots with the
    delivery-time suppression cache used by other jobs.
    """
    path = seen_path or l1_seen_path()
    duplicate_count = 0
    fresh: list[dict[str, Any]] = []

    def _mutate(value: Any) -> dict[str, Any]:
        nonlocal duplicate_count, fresh
        payload = value if isinstance(value, dict) else {}
        seen = set(payload.get("fingerprints") or [])
        fresh = []
        for item in items:
            key = content_key(item)
            if key in seen:
                duplicate_count += 1
                continue
            fresh.append({**item, "fingerprint": key})
            seen.add(key)
        ordered = list(seen)
        if len(ordered) > max_seen:
            ordered = ordered[-max_seen:]
        return {
            "schema": "news_l1_seen_fingerprints_v1",
            "updated_at": now or now_bj_iso(),
            "fingerprints": ordered,
        }

    mutate_json(path, _mutate, {})
    return fresh, duplicate_count


# ---------------------------------------------------------------------------
# L1 queue (deterministic writer, consumed by L2 grader)
# ---------------------------------------------------------------------------

def enqueue_l1_items(
    items: list[dict[str, Any]],
    *,
    queue_max_entries: int = 2000,
    now: str | None = None,
) -> int:
    """Append newly-passed items to the L1 queue as pending L2 work.

    Returns the number of items actually enqueued. The queue is a bounded
    list; oldest terminal (graded/expired) entries are trimmed first when the
    cap is exceeded so in-flight (pending/claimed) work is never dropped.
    """
    if not items:
        return 0
    stamp = now or now_bj_iso()
    added = 0

    def _mutate(value: Any) -> list[dict[str, Any]]:
        nonlocal added
        queue = list(value) if isinstance(value, list) else []
        existing_fps = {entry.get("fingerprint") for entry in queue}
        for item in items:
            fp = item.get("fingerprint")
            if fp and fp in existing_fps:
                continue
            entry = {
                "schema": "news_l1_entry_v1",
                "fingerprint": fp,
                "title": item.get("title"),
                "url": item.get("url"),
                "source_id": item.get("source_id"),
                "source_name": item.get("source_name"),
                "source_rank": item.get("source_rank"),
                "matched_keywords": item.get("matched_keywords") or [],
                "keyword_tier": item.get("keyword_tier"),
                "l1_score": item.get("l1_score"),
                "excerpt": item.get("excerpt"),
                "published_hint": item.get("published_hint"),
                "collected_at": stamp,
                "status": "pending",
                "claimed_by": None,
                "claimed_at": None,
                "attempts": 0,
            }
            queue.append(entry)
            if fp:
                existing_fps.add(fp)
            added += 1
        if len(queue) > queue_max_entries:
            terminal = [e for e in queue if e.get("status") in ("graded", "expired")]
            live = [e for e in queue if e.get("status") not in ("graded", "expired")]
            overflow = len(queue) - queue_max_entries
            terminal = terminal[max(0, len(terminal) - max(0, len(terminal) - overflow)):] \
                if overflow < len(terminal) else []
            queue = terminal + live
            if len(queue) > queue_max_entries:
                queue = queue[-queue_max_entries:]
        return queue

    mutate_json(l1_queue_path(), _mutate, [])
    return added


def persist_l1_run(result: dict[str, Any]) -> None:
    atomic_write_json(
        os.path.join(skill_data_dir(SKILL), "l1_latest.json"), result,
    )
    stamp = str(result.get("checked_at") or now_bj_iso())
    safe_stamp = stamp.replace(":", "").replace("-", "")
    run_dir = os.path.join(l1_runs_dir(), stamp[:10])
    atomic_write_json(os.path.join(run_dir, f"{safe_stamp}.json"), result)


# ---------------------------------------------------------------------------
# L2 claim/submit contract (used by scripts/news_grader.py)
# ---------------------------------------------------------------------------

def _expire_stale_claims(queue: list[dict[str, Any]], *, ttl_minutes: int, current: datetime) -> None:
    expiry = current - timedelta(minutes=max(1, ttl_minutes))
    for entry in queue:
        if entry.get("status") != "claimed":
            continue
        try:
            claimed_at = datetime.fromisoformat(str(entry.get("claimed_at")))
        except (TypeError, ValueError):
            claimed_at = datetime.min.replace(tzinfo=BJ)
        if claimed_at.tzinfo is None:
            claimed_at = claimed_at.replace(tzinfo=BJ)
        if claimed_at <= expiry:
            entry["status"] = "pending"
            entry["claimed_by"] = None
            entry["claimed_at"] = None


def claim_l1_batch(
    worker: str,
    *,
    batch_size: int = 20,
    ttl_minutes: int = 30,
    max_attempts: int = 2,
    now: str | None = None,
) -> list[dict[str, Any]]:
    """Claim up to ``batch_size`` pending L1 entries for L2 grading.

    Mirrors ``research_bus.claim_next_work``'s claim-with-TTL shape: entries
    move to ``claimed`` with a timestamp; a batch whose grader never submits
    is auto-recovered to ``pending`` after ``ttl_minutes``.
    """
    stamp = now or now_bj_iso()
    current = datetime.fromisoformat(stamp)
    claimed: list[dict[str, Any]] = []

    def _mutate(value: Any) -> list[dict[str, Any]]:
        nonlocal claimed
        queue = list(value) if isinstance(value, list) else []
        _expire_stale_claims(queue, ttl_minutes=ttl_minutes, current=current)
        taken = 0
        for entry in queue:
            if taken >= batch_size:
                break
            if entry.get("status") != "pending":
                continue
            if int(entry.get("attempts") or 0) >= max_attempts:
                entry["status"] = "expired"
                continue
            entry["status"] = "claimed"
            entry["claimed_by"] = worker
            entry["claimed_at"] = stamp
            entry["attempts"] = int(entry.get("attempts") or 0) + 1
            claimed.append(dict(entry))
            taken += 1
        return queue

    mutate_json(l1_queue_path(), _mutate, [])
    return claimed


def submit_l2_grades(
    grades: list[dict[str, Any]],
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """Mark claimed entries as graded, attaching the L2 finding payload.

    ``grades`` items must carry ``fingerprint`` plus the validated grading
    fields; entries not found in the queue (already reaped/expired) are
    reported back as ``missing`` rather than raising, since the queue may
    have rotated between claim and submit.
    """
    stamp = now or now_bj_iso()
    by_fp = {g.get("fingerprint"): g for g in grades if g.get("fingerprint")}
    missing = set(by_fp)
    updated: list[dict[str, Any]] = []

    def _mutate(value: Any) -> list[dict[str, Any]]:
        queue = list(value) if isinstance(value, list) else []
        for entry in queue:
            fp = entry.get("fingerprint")
            grade = by_fp.get(fp)
            if not grade:
                continue
            missing.discard(fp)
            entry["status"] = "graded"
            entry["graded_at"] = stamp
            entry["grade"] = {
                "materiality": grade.get("materiality"),
                "affected_sectors": grade.get("affected_sectors") or [],
                "time_window": grade.get("time_window"),
                "needs_deep_review": bool(grade.get("needs_deep_review")),
            }
            updated.append(dict(entry))
        return queue

    mutate_json(l1_queue_path(), _mutate, [])
    return {"graded": len(updated), "missing": sorted(missing), "entries": updated}


def queue_summary() -> dict[str, Any]:
    queue = read_json(l1_queue_path(), [])
    if not isinstance(queue, list):
        queue = []
    by_status: dict[str, int] = {}
    for entry in queue:
        status = str(entry.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
    return {"total": len(queue), "by_status": by_status}
