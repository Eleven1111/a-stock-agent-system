"""Memoised per-file facts for the snapshot GC.

The GC needs two derived facts about a lot of files: what a snapshot's
``dataset`` / ``captured_at`` are, and which snapshots a state file references.
Both are pure functions of file content, and between two daily runs almost every
one of those files is byte-identical — so re-deriving them costs a full parse of
the whole corpus every day for no new information. On 2026-08-05 that was 2.4 GB
and 33.7 s against a 120 s budget, growing ~1.3 s per trading day.

Facts are keyed by ``(size, mtime_ns)`` and that key is the point, not the
speed. A cached fact is reused only when the filesystem can cheaply attest the
file is unchanged in both dimensions; **anything else falls back to reading the
file** — key mismatch, malformed entry, wrong index version, unreadable or
corrupt index. The failure mode of this cache is therefore "slow", never "the
GC kept a stale belief about a file it is about to delete".

Persistence is best-effort in both directions: a GC run must not fail because
the cache could not be read or written. Losing the index costs one slow run.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

INDEX_VERSION = 1
INDEX_FILENAME = ".gc_index.json"

#: Facts about a file modified less than this long ago are computed but never
#: stored. ``(size, mtime_ns)`` only proves a file is unchanged as far as the
#: filesystem's mtime granularity goes; on a mount that rounds mtime to the
#: second, a same-size rewrite within that second is invisible. Refusing to
#: memoise anything that recent closes the window at the cost of one re-read.
SETTLE_SECONDS = 2.0

#: Sentinel for "no usable cached fact" — distinct from a cached ``None``.
MISS = object()


def index_path(state_home: Path) -> Path:
    """Where the index lives: beside the snapshot tree, not inside it.

    Inside ``market/snapshots/`` the index would be scanned as a snapshot and
    counted against the size cap; one directory up it is invisible to both.
    """
    return state_home / "market" / INDEX_FILENAME


class FactsIndex:
    """Read-old / write-new fact cache, sectioned by fact kind.

    Lookups read the previous run's sections; ``put`` writes into fresh ones, so
    files that no longer exist simply do not carry over and the index cannot
    grow without bound. Callers must ``put`` every fact they end up using —
    including the ones they just read from the cache.
    """

    __slots__ = ("path", "_old", "_new", "_hits", "_misses", "_settle_before_ns")

    def __init__(
        self,
        path: Path,
        old: dict[str, dict[str, Any]] | None = None,
        *,
        now_ns: int | None = None,
    ) -> None:
        self.path = path
        self._old = old or {}
        self._new: dict[str, dict[str, Any]] = {}
        self._hits = 0
        self._misses = 0
        clock = time.time_ns() if now_ns is None else now_ns
        self._settle_before_ns = clock - int(SETTLE_SECONDS * 1_000_000_000)

    @staticmethod
    def _stamp(stat: os.stat_result) -> list[int]:
        return [stat.st_size, stat.st_mtime_ns]

    def get(self, section: str, key: str, stat: os.stat_result) -> Any:
        """Return the cached fact for ``key``, or :data:`MISS`."""
        entry = self._old.get(section, {}).get(key)
        if not isinstance(entry, dict) or entry.get("k") != self._stamp(stat):
            self._misses += 1
            return MISS
        if "v" not in entry:
            self._misses += 1
            return MISS
        self._hits += 1
        return entry["v"]

    def put(self, section: str, key: str, stat: os.stat_result, value: Any) -> None:
        """Memoise ``value``, unless the file is too freshly written to trust."""
        if stat.st_mtime_ns > self._settle_before_ns:
            return
        self._new.setdefault(section, {})[key] = {"k": self._stamp(stat), "v": value}

    @property
    def stats(self) -> dict[str, int]:
        return {"hits": self._hits, "misses": self._misses}

    def save(self) -> bool:
        """Persist the new sections. Returns whether the write succeeded."""
        payload = {"version": INDEX_VERSION, "sections": self._new}
        temporary = self.path.parent / f"{self.path.name}.tmp"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"))
            os.replace(temporary, self.path)
            return True
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass
            return False


def load_index(path: Path, *, now_ns: int | None = None) -> FactsIndex:
    """Load the index, degrading to an empty one on anything unexpected."""
    empty = FactsIndex(path, now_ns=now_ns)
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return empty
    if not isinstance(payload, dict) or payload.get("version") != INDEX_VERSION:
        return empty
    sections = payload.get("sections")
    if not isinstance(sections, dict):
        return empty
    usable = {
        name: bucket for name, bucket in sections.items() if isinstance(bucket, dict)
    }
    return FactsIndex(path, usable, now_ns=now_ns)
