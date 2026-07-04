"""L3 dynamic theme registry — registration + tombstone semantics.

The taxonomy already separates ``industry`` (stable classification) from
``sector`` (a tradable theme) and blocks coarse exchange labels from posing as
themes. That is defence only. This module adds the offence the plan (§4a) asks
for: a registered set of L3 dynamic themes that are *runtime data, not code
constants* — each theme carries an id, name, members (constituent codes),
created_at, source evidence (limit-up commonality / news lead / policy pointer),
and a lifecycle status.

Registration is subscription-with-tombstone, mirroring ``monitor_registry``:
an ``archived`` (dead) theme must never be silently resurrected by automatic
discovery. Only an explicit ``force`` re-registration or a manual reopen can
bring a tombstoned theme back.

Fail-closed on membership: a theme cannot be registered or refreshed with an
empty member set. Membership comes only from mechanisms the repo already owns
(hot-money ladder sector tags, industry_map cache, eastmoney concept boards);
this module never fabricates constituents.

Pure standard library, cron-safe. No model name or vendor is referenced.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from paths import data_file  # noqa: E402
from sector_taxonomy import is_broad_sector_label  # noqa: E402
from state_store import mutate_json, read_json  # noqa: E402

SCHEMA = "theme_registry_v1"
REGISTRY_FILE = data_file("stock-triage", "theme_registry.json")

# Lifecycle statuses. ``emerging`` .. ``fading`` are live stages managed by the
# strength/lifecycle engine; ``archived`` is the single-directional tombstone.
LIVE_STAGES = ("emerging", "mainline", "diverging", "fading")
ARCHIVED = "archived"
VALID_STATUSES = (*LIVE_STAGES, ARCHIVED)

VALID_EVIDENCE_KINDS = {"limitup_commonality", "news_lead", "policy_pointer"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today(value: date | str | None = None) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10]) if value else date.today()


def _theme_id(name: str) -> str:
    """Stable slug id derived from the theme name (runtime data, not a code
    constant). Non-alphanumeric runs collapse to a single hyphen."""
    slug = re.sub(r"[^0-9a-z一-鿿]+", "-", str(name).strip().lower())
    slug = slug.strip("-")
    return f"theme:{slug}" if slug else "theme:unknown"


def _norm_code(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith(("sh", "sz", "bj")):
        text = text[2:]
    return text.zfill(6) if text else ""


def _norm_members(members: Iterable[Any]) -> list[str]:
    seen: list[str] = []
    for raw in members or []:
        code = _norm_code(raw)
        if code and code not in seen:
            seen.append(code)
    return seen


def _norm_evidence(evidence: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in evidence or []:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("kind") or "").strip()
        if kind not in VALID_EVIDENCE_KINDS:
            continue
        result.append({
            "kind": kind,
            "detail": str(item.get("detail") or "").strip(),
            "asof": str(item.get("asof") or "")[:10] or None,
        })
    return result


def load_registry() -> list[dict[str, Any]]:
    value = read_json(REGISTRY_FILE, [])
    return value if isinstance(value, list) else []


def get_theme(theme_id: str) -> dict[str, Any] | None:
    return next((item for item in load_registry() if item.get("id") == theme_id), None)


def active_themes(asof: date | str | None = None) -> list[dict[str, Any]]:
    """Every non-archived theme. ``asof`` is accepted for symmetry with the
    monitor registry but live themes have no calendar expiry — they die only
    through the lifecycle FSM (fading -> archived)."""
    _ = asof
    return [item for item in load_registry() if item.get("status") != ARCHIVED]


def register(
    name: str,
    members: Sequence[Any],
    *,
    source_evidence: Sequence[Mapping[str, Any]] | None = None,
    created_at: str | None = None,
    status: str = "emerging",
    force: bool = False,
) -> dict[str, Any]:
    """Register (or refresh) a theme.

    Fail-closed: an empty normalised member set is rejected — a theme with no
    constituents is never created. A tombstoned (``archived``) theme is not
    reopened by automatic callers unless ``force=True`` (explicit revival).
    """
    if is_broad_sector_label(name):
        return {"changed": False, "reason": "broad_label_rejected", "id": _theme_id(name)}
    normalized_members = _norm_members(members)
    if not normalized_members:
        return {"changed": False, "reason": "empty_members_fail_closed", "id": _theme_id(name)}
    if status not in LIVE_STAGES:
        status = "emerging"

    theme_id = _theme_id(name)
    now = created_at or _now()
    outcome: dict[str, Any] = {}

    def _mut(records: Any) -> list[dict[str, Any]]:
        items = list(records) if isinstance(records, list) else []
        existing = next((item for item in items if item.get("id") == theme_id), None)
        if existing and existing.get("status") == ARCHIVED and not force:
            outcome.update(changed=False, reason="archived_tombstone", theme=dict(existing))
            return items
        if existing is None:
            existing = {"id": theme_id, "created_at": now}
            items.append(existing)
        merged_evidence = list(existing.get("source_evidence") or [])
        merged_evidence.extend(_norm_evidence(source_evidence))
        existing.update({
            "name": str(name).strip(),
            "members": normalized_members,
            "member_count": len(normalized_members),
            "source_evidence": merged_evidence,
            "status": status,
            "updated_at": now,
        })
        existing.setdefault("created_at", now)
        outcome.update(changed=True, reason="registered", theme=dict(existing))
        return items

    mutate_json(REGISTRY_FILE, _mut, [])
    return outcome


def set_stage(
    theme_id: str,
    stage: str,
    *,
    reason: str,
    now: str | None = None,
) -> dict[str, Any]:
    """Apply a lifecycle stage transition decided by the lifecycle FSM.

    ``archived`` is single-directional: once a theme reaches it, this function
    will not move it to any live stage (the tombstone). Reviving requires
    ``register(..., force=True)`` — an explicit, auditable act.
    """
    if stage not in VALID_STATUSES:
        raise ValueError(f"unknown stage: {stage}")
    stamp = now or _now()
    outcome: dict[str, Any] = {}

    def _mut(records: Any) -> list[dict[str, Any]]:
        items = list(records) if isinstance(records, list) else []
        existing = next((item for item in items if item.get("id") == theme_id), None)
        if existing is None:
            outcome.update(changed=False, reason="not_found")
            return items
        if existing.get("status") == ARCHIVED:
            outcome.update(changed=False, reason="archived_tombstone", theme=dict(existing))
            return items
        prior = existing.get("status")
        if prior == stage:
            outcome.update(changed=False, reason="no_change", theme=dict(existing))
            return items
        history = list(existing.get("stage_history") or [])
        history.append({"from": prior, "to": stage, "reason": reason, "at": stamp})
        existing.update({
            "status": stage,
            "stage_reason": reason,
            "updated_at": stamp,
            "stage_history": history[-40:],
        })
        outcome.update(changed=True, reason="stage_set", theme=dict(existing))
        return items

    mutate_json(REGISTRY_FILE, _mut, [])
    return outcome


def theme_stage_by_sector(asof: date | str | None = None) -> dict[str, dict[str, Any]]:
    """Map a normalised theme *name* -> {id, stage, member_count} for every
    live theme, so callers (candidate weighting hook, evidence pack) can resolve
    a candidate's sector to its owning theme's lifecycle stage.

    Keyed by lower-cased theme name; the registry id is included for reference.
    """
    result: dict[str, dict[str, Any]] = {}
    for theme in active_themes(asof):
        name_key = str(theme.get("name") or "").strip().lower()
        if not name_key:
            continue
        result[name_key] = {
            "id": theme.get("id"),
            "stage": theme.get("status"),
            "member_count": theme.get("member_count"),
        }
    return result


def theme_stage_for_code(code: str, asof: date | str | None = None) -> dict[str, Any] | None:
    """Return the live theme (id/name/stage) that lists ``code`` as a member, or
    ``None``. If several themes claim the code, the earliest-created wins for
    determinism."""
    normalized = _norm_code(code)
    if not normalized:
        return None
    owners = [
        theme for theme in active_themes(asof)
        if normalized in (theme.get("members") or [])
    ]
    if not owners:
        return None
    owners.sort(key=lambda t: (str(t.get("created_at") or ""), str(t.get("id") or "")))
    winner = owners[0]
    return {
        "id": winner.get("id"),
        "name": winner.get("name"),
        "stage": winner.get("status"),
        "member_count": winner.get("member_count"),
    }


def _cli_list(_: argparse.Namespace) -> None:
    print(json.dumps(load_registry(), ensure_ascii=False, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description="L3 theme registry CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)
    list_parser = sub.add_parser("list", help="Dump the theme registry")
    list_parser.set_defaults(func=_cli_list)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
