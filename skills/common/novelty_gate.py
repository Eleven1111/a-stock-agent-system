"""Cross-pipeline content novelty gate for delivery-time news suppression."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from . import delivery_policy
    from .paths import hermes_home
    from .state_store import file_lock
except ImportError:  # pragma: no cover - runtime scripts add skills/common to sys.path.
    import delivery_policy  # type: ignore
    from paths import hermes_home  # type: ignore
    from state_store import file_lock  # type: ignore


SCHEMA = "content_novelty_gate_v1"
NOISE_WORDS = {
    "快讯",
    "公告",
    "通知",
    "消息",
    "转载",
    "来源",
}


@dataclass(frozen=True)
class NoveltyResult:
    items: list[dict[str, Any]]
    duplicate_items: list[dict[str, Any]]
    fail_open: bool = False
    shadow: bool = False

    @property
    def duplicate_count(self) -> int:
        return len(self.duplicate_items)

    @property
    def would_suppress(self) -> bool:
        return bool(self.shadow and self.duplicate_items)


def cache_path() -> Path:
    return Path(hermes_home()) / "delivery" / "novelty_gate.json"


def telemetry_path() -> Path:
    return Path(hermes_home()) / "cron" / "push_telemetry.jsonl"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_title(title: str) -> str:
    text = title.casefold()
    for word in NOISE_WORDS:
        text = text.replace(word.casefold(), "")
    chars = []
    for char in text:
        category = unicodedata.category(char)
        if category[0] in {"P", "S", "Z"}:
            continue
        if char.isspace():
            continue
        chars.append(char)
    return "".join(chars)


def _subject_hint(item: Mapping[str, Any]) -> str:
    for key in ("stock_code", "code", "symbol"):
        value = str(item.get(key) or "").strip()
        if value:
            digits = re.sub(r"\D", "", value)
            return digits.zfill(6) if digits else value.casefold()
    for key in ("stock_name", "name", "subject", "entity"):
        value = str(item.get(key) or "").strip()
        if value:
            return _normalize_title(value)
    title = _normalize_title(
        str(item.get("title") or item.get("headline") or item.get("event_title") or "")
    )
    match = re.search(r"([\u4e00-\u9fffA-Za-z0-9]{2,12})(?:发布|公告|印发|获批|签署|涨停|跌停|异动)", title)
    return _normalize_title(match.group(1)) if match else ""


def content_key(item: Mapping[str, Any]) -> str:
    title = str(item.get("title") or item.get("headline") or item.get("event_title") or "")
    normalized = _normalize_title(title)
    subject = _subject_hint(item)
    raw = f"{subject}|{normalized}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_cache(path: Path) -> tuple[dict[str, Any], bool]:
    if not path.exists():
        return {"schema": SCHEMA, "entries": {}}, False
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"schema": SCHEMA, "entries": {}}, True
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), dict):
        return {"schema": SCHEMA, "entries": {}}, True
    return payload, False


def _write_cache(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _entry_key(key: str) -> str:
    return key


def _item_size(items: Iterable[Mapping[str, Any]]) -> int:
    total = 0
    for item in items:
        try:
            total += len(json.dumps(item, ensure_ascii=False, default=str))
        except TypeError:
            total += len(str(item))
    return total


def _append_shadow_telemetry(
    *,
    job_id: str,
    duplicate_items: Sequence[Mapping[str, Any]],
    now: datetime,
) -> None:
    if not duplicate_items:
        return
    row = {
        "job_id": job_id,
        "trading_date": now.date().isoformat(),
        "delivered": True,
        "output_chars": _item_size(duplicate_items),
        "was_compressed": False,
        "silent_reason": "none",
        "would_suppress": True,
        "suppression_reason": "duplicate_news",
        "suppressed_item_count": len(duplicate_items),
    }
    path = telemetry_path()
    line = json.dumps(row, ensure_ascii=False, default=str)
    with file_lock(str(path)):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def filter_items(
    items: Sequence[Mapping[str, Any]],
    *,
    namespace: str,
    job_id: str,
    now: datetime | None = None,
    policy: Mapping[str, Any] | None = None,
    cache_file: str | Path | None = None,
) -> NoveltyResult:
    current = now or _now()
    active_policy = policy or delivery_policy.load_policy()
    if not delivery_policy.enabled(active_policy, "novelty_gate"):
        return NoveltyResult([dict(item) for item in items], [])

    ttl_days = int(delivery_policy.section(active_policy, "novelty_gate").get("ttl_days") or 7)
    cutoff = current.timestamp() - ttl_days * 86400
    path = Path(cache_file) if cache_file else cache_path()
    prepared = [(dict(item), content_key(item)) for item in items]

    try:
        with file_lock(str(path)):
            cache, corrupt = _load_cache(path)
            if corrupt:
                return NoveltyResult([item for item, _key in prepared], [], fail_open=True)
            entries = {
                key: ts
                for key, ts in (cache.get("entries") or {}).items()
                if (_parse_iso(ts) and _parse_iso(ts).timestamp() >= cutoff)
            }
            fresh: list[dict[str, Any]] = []
            duplicates: list[dict[str, Any]] = []
            seen_keys = set(entries)
            for item, key in prepared:
                scoped_key = _entry_key(key)
                if scoped_key in seen_keys:
                    duplicates.append(item)
                else:
                    fresh.append(item)
                    seen_keys.add(scoped_key)
                entries[scoped_key] = _iso(current)
            cache = {"schema": SCHEMA, "updated_at": _iso(current), "entries": entries}
            _write_cache(path, cache)
    except (OSError, TimeoutError):
        return NoveltyResult([item for item, _key in prepared], [], fail_open=True)

    is_shadow = delivery_policy.shadow(active_policy, "novelty_gate")
    if is_shadow:
        _append_shadow_telemetry(job_id=job_id, duplicate_items=duplicates, now=current)
        return NoveltyResult([item for item, _key in prepared], duplicates, shadow=True)
    return NoveltyResult(fresh, duplicates)


def duplicate_archive_note(result: NoveltyResult) -> str:
    if result.duplicate_count == 0:
        return ""
    return f"另有 {result.duplicate_count} 条重复资讯已归档"
