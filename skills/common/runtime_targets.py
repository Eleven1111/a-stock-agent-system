"""Resolve dynamic stock and topic targets from shared runtime state."""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Mapping

import monitor_registry
from paths import data_file
from state_store import read_json


def normalize_stock_code(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw.startswith(("sh", "sz", "bj")):
        raw = raw[2:]
    digits = "".join(character for character in raw if character.isdigit())
    code = digits[-6:].zfill(6) if digits else ""
    return code if code.strip("0") else ""


def is_active_entry(
    entry: Mapping[str, Any],
    asof: date | None = None,
) -> bool:
    if entry.get("status") != "active":
        return False
    expires_at = entry.get("expires_at")
    if not expires_at:
        return True
    try:
        expires = date.fromisoformat(str(expires_at)[:10])
    except (TypeError, ValueError):
        return False
    return expires >= (asof or date.today())


def cancelled_stock_codes(
    registry: Iterable[Mapping[str, Any]] | None,
) -> set[str]:
    return {
        normalize_stock_code(entry.get("key"))
        for entry in registry or []
        if entry.get("kind") == "stock" and entry.get("manual_cancelled")
    }


def build_stock_targets(
    *,
    portfolio: Mapping[str, Any] | None = None,
    registry: Iterable[Mapping[str, Any]] | None = None,
    candidate_pool: Mapping[str, Any] | None = None,
    candidate_limit: int = 0,
    asof: date | None = None,
) -> list[dict[str, str]]:
    registry_items = list(registry or [])
    cancelled = cancelled_stock_codes(registry_items)
    targets: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(item: Mapping[str, Any], source: str, code_key: str = "code") -> None:
        code = normalize_stock_code(item.get(code_key))
        if not code or code in cancelled or code in seen:
            return
        seen.add(code)
        targets.append({
            "code": code,
            "name": str(item.get("name") or item.get("label") or code),
            "source": source,
        })

    for position in (portfolio or {}).get("positions") or []:
        add(position, "portfolio")
    for entry in registry_items:
        if entry.get("kind") == "stock" and is_active_entry(entry, asof):
            add(entry, "monitor", code_key="key")
    candidates = (candidate_pool or {}).get("candidates") or []
    for candidate in candidates[:max(0, candidate_limit)]:
        add(candidate, "candidate_pool")
    return targets


def build_topics(
    *,
    registry: Iterable[Mapping[str, Any]] | None = None,
    hot_money_selection: Mapping[str, Any] | None = None,
    candidate_pool: Mapping[str, Any] | None = None,
    asof: date | None = None,
) -> list[dict[str, str]]:
    target_date = asof or date.today()
    registry_items = list(registry or [])
    cancelled_names = {
        str(value).strip()
        for entry in registry_items
        if entry.get("kind") in {"sector", "theme"}
        and entry.get("manual_cancelled")
        for value in (entry.get("key"), entry.get("label"))
        if str(value or "").strip()
    }
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, key: Any, label: Any = None) -> None:
        normalized = str(key or "").strip()
        normalized_label = str(label or normalized).strip()
        identity = (kind, normalized)
        if (
            not normalized
            or normalized in cancelled_names
            or normalized_label in cancelled_names
            or identity in seen
        ):
            return
        seen.add(identity)
        result.append({"kind": kind, "key": normalized, "label": normalized_label})

    for entry in registry_items:
        kind = str(entry.get("kind") or "")
        if kind not in {"sector", "theme"} or not is_active_entry(entry, target_date):
            continue
        key = str(entry.get("key") or "").strip()
        add(kind, key, entry.get("label"))

    artifact_date = target_date.isoformat()

    def is_current(artifact: Mapping[str, Any] | None) -> bool:
        value = artifact or {}
        return str(value.get("asof") or value.get("trading_date") or "")[:10] == artifact_date

    def is_mainline(rank: Any, state: Any, selection: Mapping[str, Any]) -> bool:
        if str(state or "") in {"confirmed", "emerging"}:
            return True
        try:
            limit = int((selection.get("config") or {}).get("mainline_top_n") or 0)
            return limit > 0 and int(rank) <= limit
        except (TypeError, ValueError):
            return False

    selection = hot_money_selection or {}
    if is_current(selection):
        for row in selection.get("sectors") or []:
            if isinstance(row, Mapping) and is_mainline(
                row.get("rank"), row.get("state"), selection
            ):
                add("sector", row.get("sector"))

    pool = candidate_pool or {}
    if is_current(pool):
        pool_selection = pool.get("hot_money_selection") or {}
        for row in pool_selection.get("sectors") or []:
            if isinstance(row, Mapping) and is_mainline(
                row.get("rank"), row.get("state"), pool_selection
            ):
                add("sector", row.get("sector"))
        for row in pool.get("candidates") or []:
            if isinstance(row, Mapping) and is_mainline(
                row.get("sector_rank"), row.get("sector_state"), pool_selection
            ):
                add("sector", row.get("sector"))
    return result


def load_stock_targets(candidate_limit: int = 0) -> list[dict[str, str]]:
    return build_stock_targets(
        portfolio=read_json(
            data_file("stock-triage", "portfolio.json"),
            {"positions": []},
        ),
        registry=monitor_registry.load_registry(),
        candidate_pool=read_json(
            data_file("stock-triage", "candidate_pool_latest.json"),
            {"candidates": []},
        ),
        candidate_limit=candidate_limit,
    )


def stock_map(candidate_limit: int = 0) -> dict[str, str]:
    return {
        target["code"]: target["name"]
        for target in load_stock_targets(candidate_limit=candidate_limit)
    }


def load_topics(asof: date | None = None) -> list[dict[str, str]]:
    return build_topics(
        registry=monitor_registry.load_registry(),
        hot_money_selection=read_json(
            data_file("stock-triage", "hot_money_selection_latest.json"),
            {},
        ),
        candidate_pool=read_json(
            data_file("stock-triage", "candidate_pool_latest.json"),
            {},
        ),
        asof=asof,
    )
