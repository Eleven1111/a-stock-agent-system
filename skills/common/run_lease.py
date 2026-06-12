"""Atomic run leases for deduplicating Hermes and OpenClaw executions."""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from paths import hermes_home


DEFAULT_TTL_SECONDS = 30 * 60


def _safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)


def lease_path(job_id: str, trading_date: str, batch_id: str) -> str:
    return os.path.join(
        hermes_home(),
        "runtime",
        "leases",
        _safe(trading_date),
        _safe(batch_id),
        f"{_safe(job_id)}.lease",
    )


def _read_holder(path: str) -> dict[str, Any]:
    try:
        with open(os.path.join(path, "holder.json"), encoding="utf-8") as handle:
            value = json.load(handle)
            return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _remove_stale(path: str, ttl_seconds: int) -> bool:
    try:
        age = time.time() - os.stat(path).st_mtime
    except FileNotFoundError:
        return True
    if age <= ttl_seconds:
        return False
    stale_path = f"{path}.stale-{uuid.uuid4().hex}"
    try:
        os.rename(path, stale_path)
    except (FileNotFoundError, OSError):
        return False
    shutil.rmtree(stale_path, ignore_errors=True)
    return True


@contextmanager
def claim(
    job_id: str,
    *,
    trading_date: str,
    batch_id: str,
    run_id: str,
    runtime: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> Iterator[dict[str, Any]]:
    """Claim an active-run lease using atomic directory creation."""
    path = lease_path(job_id, trading_date, batch_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    token = uuid.uuid4().hex
    acquired = False

    for _attempt in range(2):
        try:
            os.mkdir(path)
            acquired = True
            break
        except FileExistsError:
            if not _remove_stale(path, ttl_seconds):
                break

    if not acquired:
        yield {
            "acquired": False,
            "lease_path": path,
            "holder": _read_holder(path),
        }
        return

    holder = {
        "schema": "a_stock_run_lease_v1",
        "job_id": job_id,
        "trading_date": trading_date,
        "batch_id": batch_id,
        "run_id": run_id,
        "runtime": runtime,
        "token": token,
        "acquired_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pid": os.getpid(),
    }
    with open(os.path.join(path, "holder.json"), "w", encoding="utf-8") as handle:
        json.dump(holder, handle, ensure_ascii=False, indent=2)

    try:
        yield {
            "acquired": True,
            "lease_path": path,
            "holder": holder,
        }
    finally:
        current = _read_holder(path)
        if current.get("token") == token:
            shutil.rmtree(path, ignore_errors=True)
