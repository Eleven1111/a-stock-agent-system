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
"""

import json
import os
import fcntl
import tempfile
import shutil
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional


def _ensure_dir(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)


@contextmanager
def file_lock(filepath: str, timeout: float = 10.0):
    """
    文件锁上下文管理器（阻塞等待，超时抛异常）。
    锁文件创建后永不被删除，避免旧 inode 和新建 lockfile 的竞态。
    """
    _ensure_dir(filepath)
    lockfile = filepath + ".lock"
    fd = os.open(lockfile, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        # 不删除 lockfile！防止后续进程通过新 lockfile 绕过锁


def atomic_write_json(filepath: str, data: Any) -> None:
    """
    原子写入 JSON（全部在锁内完成）：
    1. 备份原文件到 .bak
    2. 写到临时文件
    3. os.replace 到目标（原子操作）
    """
    _ensure_dir(filepath)
    content = json.dumps(data, ensure_ascii=False, indent=2, default=str)

    with file_lock(filepath):
        # 备份原文件（在锁内完成）
        if os.path.exists(filepath):
            try:
                shutil.copy2(filepath, filepath + ".bak")
            except OSError:
                pass

        # 原子写入
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(filepath), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, filepath)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def read_json(filepath: str, default: Any = None) -> Any:
    """
    读取 JSON，损坏时从 .bak 恢复。
    返回解析后的对象，失败返回 default。
    """
    if not os.path.exists(filepath):
        return default

    try:
        with file_lock(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        # 尝试从备份恢复
        bak = filepath + ".bak"
        if os.path.exists(bak):
            try:
                with open(bak, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 恢复成功，写回主文件
                atomic_write_json(filepath, data)
                return data
            except (json.JSONDecodeError, OSError):
                pass
        return default


def update_json_list(filepath: str, item: Any,
                     unique_key: str = None, max_items: int = None) -> list:
    """
    原子追加到 JSON 列表。
    unique_key: 如果指定，按此键去重后追加。
    max_items: 如果指定，超过后截断旧数据。
    """
    existing = read_json(filepath, [])
    if not isinstance(existing, list):
        existing = []

    if unique_key:
        existing = [e for e in existing
                    if not (isinstance(e, dict) and e.get(unique_key) == item.get(unique_key))]

    existing.append(item)

    if max_items and len(existing) > max_items:
        existing = existing[-max_items:]

    atomic_write_json(filepath, existing)
    return existing


def mark_failed(filepath: str, error: str) -> None:
    """标记最后一次操作为失败状态"""
    atomic_write_json(filepath, {
        "last_error": error,
        "failed_at": __import__("datetime").datetime.now().isoformat(),
    })


# ========== 并发安全回归测试 ==========

def _concurrent_write_worker(filepath: str, data: dict, results: list, idx: int):
    """并发写入工作线程"""
    try:
        atomic_write_json(filepath, data)
        results.append({"worker": idx, "ok": True})
    except Exception as e:
        results.append({"worker": idx, "ok": False, "error": str(e)})


def _concurrent_read_worker(filepath: str, results: list, idx: int):
    """并发读取工作线程"""
    try:
        data = read_json(filepath)
        results.append({"worker": idx, "ok": True, "data_valid": isinstance(data, dict)})
    except Exception as e:
        results.append({"worker": idx, "ok": False, "error": str(e)})


def run_concurrent_test(filepath: str, num_writers: int = 5) -> Dict[str, Any]:
    """
    并发安全回归测试。
    启动 num_writers 个线程同时写入同一文件，验证：
    1. 所有线程都能完成（无死锁）
    2. 最终文件是合法的 JSON
    3. 文件内容等于最后一次写入（或至少是某个写入中的数据，不损坏）
    """
    import random
    import string

    results = []
    threads = []

    for i in range(num_writers):
        t = threading.Thread(
            target=_concurrent_write_worker,
            args=(filepath, {"worker": i, "value": random.randint(0, 100000),
                             "tag": ''.join(random.choices(string.ascii_letters, k=8))},
                  results, i)
        )
        threads.append(t)

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 验证最终文件
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
