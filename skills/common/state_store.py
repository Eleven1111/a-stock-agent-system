"""
共享状态存储模块 — 原子写 + 并发安全
====================================
所有 JSON 状态文件统一走此模块，提供：
- 原子写入（先写临时文件再 rename）
- 自动备份（写入前 .bak，在锁内完成）
- 崩溃恢复（JSON 损坏时从 .bak 恢复）
- 文件锁（macOS/Linux fcntl.flock，锁文件保留不删除）

并发安全：lockfile 创建后永不删除，后续进程等待时阻塞在 fcntl.flock
直到前一个进程释放锁。备份、临时写、os.replace 全在锁内完成。

update_json_list 使用内部 _read_json_unlocked / _write_json_unlocked，
确保"读-改-写"全过程在同一把锁内，不会因两次独立加锁丢更新。
"""

import json
import os
import fcntl
import tempfile
import shutil
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict


CRITICAL_JSON_FILES = {
    "portfolio.json",
    "trade_history.json",
    "cash_flow.json",
    "monitor_registry.json",
    "strategy_registry.json",
}


def _ensure_dir(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)


@contextmanager
def file_lock(filepath: str, timeout: float = 10.0):
    """
    文件锁上下文管理器（非阻塞轮询，超时抛 TimeoutError）。
    锁文件创建后永不被删除，避免旧 inode 和新建 lockfile 的竞态。
    timeout <= 0 时回退到阻塞等待（兼容旧调用方）。
    """
    _ensure_dir(filepath)
    lockfile = filepath + ".lock"
    fd = os.open(lockfile, os.O_CREAT | os.O_RDWR)

    if timeout > 0:
        # 非阻塞轮询 + deadline
        deadline = time.monotonic() + timeout
        acquired = False
        while time.monotonic() < deadline:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                time.sleep(0.05)
        if not acquired:
            os.close(fd)
            raise TimeoutError(
                f"file_lock({filepath}) timeout after {timeout}s"
            )
    else:
        # timeout <= 0 → 无限阻塞（兼容旧调用）
        fcntl.flock(fd, fcntl.LOCK_EX)

    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _critical_relative_path(filepath: str) -> str | None:
    try:
        from paths import hermes_home

        root = os.path.abspath(os.path.expanduser(hermes_home()))
        path = os.path.abspath(os.path.expanduser(filepath))
        if os.path.commonpath([root, path]) != root:
            return None
        if os.path.basename(path) not in CRITICAL_JSON_FILES:
            return None
        return os.path.relpath(path, root)
    except (ImportError, OSError, ValueError):
        return None


def _critical_backup_dir(filepath: str) -> str | None:
    relative = _critical_relative_path(filepath)
    if relative is None:
        return None
    from paths import backup_home

    return os.path.join(
        backup_home(),
        os.path.dirname(relative),
        os.path.basename(relative) + ".versions",
    )


def _snapshot_critical_file(filepath: str) -> None:
    backup_dir = _critical_backup_dir(filepath)
    if backup_dir is None or not os.path.exists(filepath):
        return
    try:
        with open(filepath, "r", encoding="utf-8") as handle:
            json.load(handle)
        os.makedirs(backup_dir, mode=0o700, exist_ok=True)
        snapshot = os.path.join(
            backup_dir,
            f"{time.time_ns()}-{os.getpid()}.json",
        )
        shutil.copy2(filepath, snapshot)
        os.chmod(snapshot, 0o600)
        keep = max(1, int(os.environ.get("A_STOCK_BACKUP_KEEP", "20")))
        snapshots = sorted(
            (
                os.path.join(backup_dir, name)
                for name in os.listdir(backup_dir)
                if name.endswith(".json")
            ),
            key=os.path.getmtime,
            reverse=True,
        )
        for old in snapshots[keep:]:
            os.unlink(old)
    except (OSError, ValueError, json.JSONDecodeError):
        return


def _backup_candidates(filepath: str) -> list[str]:
    candidates: list[str] = []
    backup_dir = _critical_backup_dir(filepath)
    if backup_dir and os.path.isdir(backup_dir):
        candidates.extend(
            sorted(
                (
                    os.path.join(backup_dir, name)
                    for name in os.listdir(backup_dir)
                    if name.endswith(".json")
                ),
                key=os.path.getmtime,
                reverse=True,
            )
        )
    candidates.append(filepath + ".bak")
    return candidates


def _recover_json_unlocked(filepath: str) -> Any:
    for candidate in _backup_candidates(filepath):
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            _write_json_unlocked(filepath, data, create_backups=False)
            return data
        except (json.JSONDecodeError, OSError):
            continue
    raise FileNotFoundError(filepath)


def _read_json_unlocked(filepath: str, default: Any = None) -> Any:
    """无锁读取 JSON（调用者必须已持有 file_lock）。返回解析后的对象，失败返回 default。"""
    if not os.path.exists(filepath):
        try:
            return _recover_json_unlocked(filepath)
        except FileNotFoundError:
            return default
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        try:
            return _recover_json_unlocked(filepath)
        except FileNotFoundError:
            return default


def _write_json_unlocked(
    filepath: str,
    data: Any,
    *,
    create_backups: bool = True,
) -> None:
    """无锁写入 JSON（调用者必须已持有 file_lock）。备份 + 临时写 + os.replace。"""
    content = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    _ensure_dir(filepath)

    if create_backups and os.path.exists(filepath):
        try:
            shutil.copy2(filepath, filepath + ".bak")
        except OSError:
            pass

    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(filepath), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, filepath)
        if create_backups:
            _snapshot_critical_file(filepath)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ========== 公开 API（自带锁）==========


def atomic_write_json(filepath: str, data: Any) -> None:
    """原子写入 JSON（自带锁）。"""
    with file_lock(filepath):
        _write_json_unlocked(filepath, data)


def read_json(filepath: str, default: Any = None) -> Any:
    """原子读取 JSON（自带锁），损坏时从 .bak 恢复。"""
    with file_lock(filepath):
        return _read_json_unlocked(filepath, default)


def update_json_list(filepath: str, item: Any,
                     unique_key: str = None, max_items: int = None) -> list:
    """
    原子追加到 JSON 列表（读-改-写全过程在同一把锁内）。
    unique_key: 如果指定，按此键去重后追加。
    max_items: 如果指定，超过后截断旧数据。
    """
    with file_lock(filepath):
        existing = _read_json_unlocked(filepath, [])
        if not isinstance(existing, list):
            existing = []

        if unique_key:
            existing = [e for e in existing
                        if not (isinstance(e, dict) and e.get(unique_key) == item.get(unique_key))]

        existing.append(item)

        if max_items and len(existing) > max_items:
            existing = existing[-max_items:]

        _write_json_unlocked(filepath, existing)
    return existing


def mutate_json(filepath: str, mutator, default: Any = None) -> Any:
    """
    事务式读-改-写（读取、mutator 变更、写回全程在同一把锁内）。
    mutator: 接收当前数据（损坏/缺失时为 default），返回写回的新数据。
    用于"读改写"事务——例如批量结算 pending 记录——避免读与写分两次独立加锁
    时被并发追加/写回覆盖丢失。返回 mutator 产出的新数据。
    """
    with file_lock(filepath):
        current = _read_json_unlocked(filepath, default)
        updated = mutator(current)
        _write_json_unlocked(filepath, updated)
        return updated


def mark_failed(filepath: str, error: str) -> None:
    """标记最后一次操作为失败状态"""
    atomic_write_json(filepath, {
        "last_error": error,
        "failed_at": __import__("datetime").datetime.now().isoformat(),
    })


# ========== 并发安全回归测试 ==========

def _concurrent_append_worker(filepath: str, results: list, idx: int,
                              unique_key: str = None):
    """并发追加工作线程"""
    try:
        update_json_list(filepath, {"worker": idx, "seq": idx}, unique_key=unique_key)
        results.append({"worker": idx, "ok": True})
    except Exception as e:
        results.append({"worker": idx, "ok": False, "error": str(e)})


def run_concurrent_append_test(filepath: str, num_workers: int = 30) -> Dict[str, Any]:
    """
    并发追加回归测试：num_workers 个线程同时 update_json_list。
    验证最终列表长度 == num_workers（无丢更新）。
    """
    results = []
    threads = []

    for i in range(num_workers):
        t = threading.Thread(
            target=_concurrent_append_worker,
            args=(filepath, results, i, "worker"),
        )
        threads.append(t)

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 读取最终结果
    with file_lock(filepath):
        final_list = _read_json_unlocked(filepath, [])

    return {
        "total_workers": num_workers,
        "final_list_length": len(final_list),
        "all_completed": all(r.get("ok") for r in results),
        "no_data_loss": len(final_list) == num_workers,
        "worker_details": results,
    }


def run_concurrent_test(filepath: str, num_writers: int = 5) -> Dict[str, Any]:
    """并发写入回归测试（旧接口，向前兼容）"""
    import random
    import string
    results = []
    threads = []

    def writer(fp, data, rlist, idx):
        try:
            atomic_write_json(fp, data)
            rlist.append({"worker": idx, "ok": True})
        except Exception as e:
            rlist.append({"worker": idx, "ok": False, "error": str(e)})

    for i in range(num_writers):
        t = threading.Thread(
            target=writer,
            args=(filepath, {"worker": i, "value": random.randint(0, 100000),
                             "tag": ''.join(random.choices(string.ascii_letters, k=8))},
                  results, i)
        )
        threads.append(t)

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final_data = read_json(filepath)
    json_valid = isinstance(final_data, dict)
    all_ok = all(r.get("ok") for r in results)

    return {
        "total_workers": num_writers,
        "all_completed": all_ok,
        "final_json_valid": json_valid,
        "final_data_worker": final_data.get("worker") if json_valid else None,
        "worker_details": results,
    }
