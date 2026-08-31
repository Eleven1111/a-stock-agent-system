"""Bounded retention for immutable snapshots and isolated cron artifacts."""

from __future__ import annotations

import gzip
import json
import mmap
import os
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from data_access_config import storage_settings
from gc_index import MISS, FactsIndex, index_path as gc_index_path, load_index
from paths import hermes_home


SNAPSHOT_SCHEMA = "market_snapshot_v1"

SNAPSHOT_SECTION = "snapshots"
REFERENCE_SECTION = "references"

_SNAPSHOT_PATH_KEY = b'"snapshot_path"'


def _snapshot_path_tokens(payload: mmap.mmap):
    """Yield JSON string tokens assigned to ``snapshot_path`` in mapped bytes.

    ``mmap.find`` uses the platform's fast byte search.  A regex with an
    arbitrary-length JSON-string branch was surprisingly quadratic on the
    multi-MiB state files that contain no reference at all.
    """
    position = 0
    size = len(payload)
    while True:
        key_at = payload.find(_SNAPSHOT_PATH_KEY, position)
        if key_at < 0:
            return
        cursor = key_at + len(_SNAPSHOT_PATH_KEY)
        while cursor < size and payload[cursor] in b" \t\r\n":
            cursor += 1
        if cursor >= size or payload[cursor] != ord(":"):
            position = cursor + 1
            continue
        cursor += 1
        while cursor < size and payload[cursor] in b" \t\r\n":
            cursor += 1
        if cursor >= size or payload[cursor] != ord('"'):
            position = cursor + 1
            continue
        start = cursor
        cursor += 1
        escaped = False
        while cursor < size:
            byte = payload[cursor]
            if escaped:
                escaped = False
            elif byte == ord("\\"):
                escaped = True
            elif byte == ord('"'):
                yield payload[start:cursor + 1]
                cursor += 1
                break
            cursor += 1
        position = max(cursor, key_at + len(_SNAPSHOT_PATH_KEY))


@dataclass(frozen=True)
class SnapshotEntry:
    path: Path
    dataset: str
    captured_at: datetime
    size: int


def _parse_datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _tree_bytes(root: Path) -> int:
    total = 0
    if not root.exists():
        return total
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _snapshot_facts(path: Path) -> dict[str, str]:
    """Read the retention-relevant metadata out of one snapshot file.

    Raises for anything that is not a well-formed snapshot; the caller turns
    that into an ``invalid`` entry, which is reported but never deleted.
    """
    record = _read_json(path)
    if not isinstance(record, dict) or record.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError("unsupported snapshot schema")
    dataset = record.get("dataset")
    captured_at = record.get("captured_at")
    if not isinstance(dataset, str) or not dataset or not captured_at:
        raise ValueError("missing snapshot metadata")
    return {"dataset": dataset, "captured_at": str(captured_at)}


def _scan_snapshots(
    root: Path, index: FactsIndex | None = None
) -> tuple[list[SnapshotEntry], list[str]]:
    entries: list[SnapshotEntry] = []
    invalid: list[str] = []
    if not root.exists():
        return entries, invalid

    for path in root.rglob("*.json"):
        key = str(path)
        try:
            stat = path.stat()
        except OSError:
            invalid.append(key)
            continue
        facts: Any = MISS if index is None else index.get(SNAPSHOT_SECTION, key, stat)
        if facts is MISS:
            try:
                facts = _snapshot_facts(path)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                facts = None
        try:
            if facts is None:
                invalid.append(key)
            else:
                entries.append(
                    SnapshotEntry(
                        path=path.resolve(strict=False),
                        dataset=facts["dataset"],
                        captured_at=_parse_datetime(facts["captured_at"]),
                        size=stat.st_size,
                    )
                )
        except (KeyError, TypeError, ValueError):
            # A cached fact that no longer parses is treated exactly like an
            # unreadable snapshot: reported, never deleted.
            invalid.append(key)
            continue
        if index is not None:
            index.put(SNAPSHOT_SECTION, key, stat, facts)
    return entries, invalid


def _extract_snapshot_paths(
    value: Any,
    references: set[Path],
    *,
    state_home: Path,
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "snapshot_path" and isinstance(child, str) and child:
                path = Path(os.path.expanduser(child))
                if not path.is_absolute():
                    path = state_home / path
                references.add(path.resolve(strict=False))
            else:
                _extract_snapshot_paths(child, references, state_home=state_home)
    elif isinstance(value, list):
        for child in value:
            _extract_snapshot_paths(child, references, state_home=state_home)


def _read_references_into(path: Path, found: set[Path], *, state_home: Path) -> None:
    """Fill ``found`` with the snapshot paths ``path`` references.

    May raise partway through a ``.jsonl`` file, in which case ``found`` holds
    the references read so far — and the caller keeps them. Dropping a partially
    read file's references would un-protect snapshots that a corrupt tail of the
    ledger has nothing to say about.
    """
    if path.suffix == ".jsonl":
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    _extract_snapshot_paths(
                        json.loads(line), found, state_home=state_home
                    )
        return
    # State snapshots can be tens of megabytes and the recent-reference corpus
    # is multiple GiB.  Building every JSON object merely to find one leaf key
    # made snapshot-gc exceed its 120s budget before it could persist its fact
    # index.  Scan the immutable bytes instead; mmap keeps memory bounded and
    # JSON-decodes only the matched string token (so escaped paths still work).
    with path.open("rb") as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            return
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as payload:
            for token in _snapshot_path_tokens(payload):
                try:
                    child = json.loads(token.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                _extract_snapshot_paths(
                    {"snapshot_path": child}, found, state_home=state_home
                )


def _scan_recent_references(
    state_home: Path,
    snapshot_dir: Path,
    *,
    cutoff: datetime,
    index: FactsIndex | None = None,
) -> tuple[set[Path], int]:
    references: set[Path] = set()
    scanned = 0
    if not state_home.exists():
        return references, scanned

    # Skipped whether or not the index is in use, so that ``use_index=False``
    # stays a true equivalent rather than a run that also scans the cache.
    index_file = gc_index_path(state_home)
    # A cold index used to parse every recent JSON file (2.88 GiB in
    # production) just to discover that almost none contained this field.
    # ripgrep is a safe prefilter: it only decides which files need Python's
    # exact path-token parser.  If unavailable or interrupted, fall back to a
    # portable walk with identical semantics.
    candidate_paths: list[Path] | None = None
    rg = shutil.which("rg")
    if rg:
        try:
            searched = subprocess.run(
                [
                    rg, "--files-with-matches", "--fixed-strings", "--no-messages",
                    "--glob", "*.json", "--glob", "*.jsonl",
                    "--glob", "!market/snapshots/**", '"snapshot_path"', ".",
                ],
                cwd=state_home,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            searched = None
        if searched is not None and searched.returncode in {0, 1}:
            candidate_paths = [
                (state_home / line.strip()).resolve(strict=False)
                for line in searched.stdout.splitlines()
                if line.strip()
            ]
    if candidate_paths is None:
        candidate_paths = []
        for current, directories, filenames in os.walk(state_home):
            current_path = Path(current)
            if current_path == snapshot_dir:
                directories.clear()
                continue
            directories[:] = [
                name for name in directories
                if current_path / name != snapshot_dir
            ]
            candidate_paths.extend(
                current_path / name
                for name in filenames
                if Path(name).suffix in {".json", ".jsonl"}
            )

    for path in candidate_paths:
        try:
            stat = path.stat()
            if datetime.fromtimestamp(stat.st_mtime, timezone.utc) < cutoff:
                continue
        except OSError:
            continue
        if path == index_file:
            continue  # the cache never references snapshots; do not parse it

        key = str(path)
        cached = MISS if index is None else index.get(REFERENCE_SECTION, key, stat)
        found: set[Path] = set()
        if isinstance(cached, list):
            found = {Path(item) for item in cached}
        else:
            try:
                _read_references_into(path, found, state_home=state_home)
            except (OSError, json.JSONDecodeError):
                # Partially read: keep what was found, but never cache an
                # incomplete fact set.
                references |= found
                continue
        references |= found
        if index is not None:
            index.put(REFERENCE_SECTION, key, stat, sorted(str(item) for item in found))
        scanned += 1
    return {
        path for path in references if _is_within(path, snapshot_dir)
    }, scanned


def _retention_days(entry: SnapshotEntry, settings: Mapping[str, Any]) -> int:
    key = (
        "snapshot_input_retention_days"
        if entry.dataset.endswith("-input")
        else "snapshot_output_retention_days"
    )
    return int(settings[key])


def _sidecar_bytes(path: Path) -> int:
    total = 0
    for suffix in (".bak",):
        sidecar = Path(f"{path}{suffix}")
        try:
            total += sidecar.stat().st_size
        except OSError:
            continue
    return total


def archive_dir(home: Path) -> Path:
    return home / "archive" / "snapshots"


def _archive_file(path: Path, *, root: Path, archive_root: Path) -> int:
    """Gzip-copy an expiring snapshot into the cold archive tier before deletion.

    Best-effort: a GC run must not fail because a single archive write failed
    (disk full, permissions, etc.) — the file is still eligible for deletion
    either way, same as the existing invalid-snapshot handling.
    """
    try:
        relative = path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return 0
    target = archive_root / relative.parent / f"{relative.name}.gz"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with path.open("rb") as source, gzip.open(target, "wb") as dest:
            shutil.copyfileobj(source, dest)
        return target.stat().st_size
    except OSError:
        return 0


def _delete_file(path: Path, *, root: Path, apply: bool) -> int:
    if not _is_within(path, root):
        raise ValueError(f"refusing to delete outside retention root: {path}")
    reclaimed = 0
    for target in (path, Path(f"{path}.bak")):
        try:
            reclaimed += target.stat().st_size
        except OSError:
            continue
        if apply:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
    return reclaimed


def cleanup_storage(
    *,
    state_home: str | Path | None = None,
    settings: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    apply: bool = False,
    use_index: bool = True,
) -> dict[str, Any]:
    """Plan or apply bounded storage cleanup without breaking recent lineage.

    ``use_index`` memoises per-file facts across runs (see :mod:`gc_index`); it
    is a pure cache, so the plan is identical either way and dry runs refresh it
    too. Pass ``False`` to force a full re-read of every file.
    """
    home = Path(state_home or hermes_home()).expanduser().resolve(strict=False)
    snapshot_dir = home / "market" / "snapshots"
    cron_dir = home / "cron" / "output"
    policy = dict(settings or storage_settings())
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    max_delete = int(policy["gc_max_delete_files"])
    min_keep = int(policy["snapshot_min_keep_per_dataset"])
    max_snapshot_bytes = int(float(policy["snapshot_max_total_mb"]) * 1024 * 1024)

    index = load_index(gc_index_path(home)) if use_index else None
    entries, invalid_files = _scan_snapshots(snapshot_dir, index)
    references, reference_files_scanned = _scan_recent_references(
        home,
        snapshot_dir,
        cutoff=current - timedelta(days=int(policy["reference_protection_days"])),
        index=index,
    )
    index_saved = index.save() if index is not None else False
    index_stats = index.stats if index is not None else {"hits": 0, "misses": 0}
    counts = Counter(entry.dataset for entry in entries)
    selected: dict[Path, str] = {}
    protected_references = sum(entry.path in references for entry in entries)

    for entry in sorted(entries, key=lambda item: item.captured_at):
        if len(selected) >= max_delete:
            break
        if entry.path in references or counts[entry.dataset] <= min_keep:
            continue
        cutoff = current - timedelta(days=_retention_days(entry, policy))
        if entry.captured_at < cutoff:
            selected[entry.path] = "expired"
            counts[entry.dataset] -= 1

    snapshot_bytes = _tree_bytes(snapshot_dir)
    planned_snapshot_bytes = snapshot_bytes - sum(
        entry.size + _sidecar_bytes(entry.path)
        for entry in entries
        if entry.path in selected
    )
    if planned_snapshot_bytes > max_snapshot_bytes:
        remaining = sorted(
            (
                entry
                for entry in entries
                if entry.path not in selected and entry.path not in references
            ),
            key=lambda item: item.captured_at,
        )
        for entry in remaining:
            if len(selected) >= max_delete:
                break
            if counts[entry.dataset] <= min_keep:
                continue
            selected[entry.path] = "size_cap"
            counts[entry.dataset] -= 1
            planned_snapshot_bytes -= entry.size + _sidecar_bytes(entry.path)
            if planned_snapshot_bytes <= max_snapshot_bytes:
                break

    cron_candidates: list[Path] = []
    cron_cutoff = current - timedelta(days=int(policy["cron_artifact_retention_days"]))
    if cron_dir.exists():
        for path in cron_dir.rglob("*.json"):
            if path == cron_dir / "job_runs.json":
                continue
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            except OSError:
                continue
            if modified < cron_cutoff:
                cron_candidates.append(path.resolve(strict=False))
    cron_candidates.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0)
    cron_selected = cron_candidates[: max(0, max_delete - len(selected))]

    archive_enabled = bool(policy.get("snapshot_cold_archive_enabled", True))
    archive_root = archive_dir(home)
    archived_count = 0
    archived_bytes = 0

    reclaimed = 0
    for path in selected:
        if archive_enabled:
            if apply:
                written = _archive_file(path, root=snapshot_dir, archive_root=archive_root)
                if written:
                    archived_count += 1
                    archived_bytes += written
            else:
                archived_count += 1
        reclaimed += _delete_file(path, root=snapshot_dir, apply=apply)
    for path in cron_selected:
        reclaimed += _delete_file(path, root=cron_dir, apply=apply)

    expired_count = sum(reason == "expired" for reason in selected.values())
    cap_count = sum(reason == "size_cap" for reason in selected.values())
    remaining_snapshot_bytes = (
        _tree_bytes(snapshot_dir) if apply else max(0, planned_snapshot_bytes)
    )
    retained_lock_files = (
        sum(1 for path in snapshot_dir.rglob("*.lock"))
        if snapshot_dir.exists()
        else 0
    )
    return {
        "schema": "a_stock_storage_gc_v1",
        "status": "ok",
        "mode": "apply" if apply else "dry_run",
        "state_home": str(home),
        "policy": policy,
        "scanned": {
            "snapshots": len(entries),
            "cron_artifacts": len(cron_candidates),
            "reference_files": reference_files_scanned,
            "invalid_snapshots": len(invalid_files),
        },
        "deleted": {
            "expired_snapshots": expired_count,
            "size_cap_snapshots": cap_count,
            "cron_artifacts": len(cron_selected),
        },
        "protected": {
            "referenced_snapshots": protected_references,
            "minimum_per_dataset": min_keep,
        },
        "archived": {
            "enabled": archive_enabled,
            "archive_root": str(archive_root),
            "count": archived_count,
            "bytes": archived_bytes if apply else None,
        },
        "index": {
            "enabled": index is not None,
            "path": str(gc_index_path(home)),
            "saved": index_saved,
            "reused_facts": index_stats["hits"],
            "read_files": index_stats["misses"],
        },
        "reclaimed_bytes": reclaimed,
        "remaining_snapshot_bytes": remaining_snapshot_bytes,
        "snapshot_capacity_bytes": max_snapshot_bytes,
        "capacity_satisfied": remaining_snapshot_bytes <= max_snapshot_bytes,
        "delete_limit_reached": (
            len(selected) + len(cron_selected) >= max_delete
            and (
                len(cron_candidates) > len(cron_selected)
                or remaining_snapshot_bytes > max_snapshot_bytes
            )
        ),
        "invalid_snapshot_paths": invalid_files,
        "retained_lock_files": retained_lock_files,
        "retained_lock_reason": "state_store lock files are never unlinked to avoid split-lock races",
    }
