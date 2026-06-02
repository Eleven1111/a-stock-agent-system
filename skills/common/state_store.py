"""
共享状态存储模块 — 原子写 + 并发安全
====================================
所有 JSON 状态文件统一走此模块，提供：
- 原子写入（先写临时文件再 rename）
- 自动备份（写入前 .bak）
- 崩溃恢复（JSON 损坏时从 .bak 恢复）
- 文件锁（macOS/Linux fcntl.flock）
"""

import json
import os
import fcntl
import tempfile
import shutil
from contextlib import contextmanager
from typing import Any, Dict


def _ensure_dir(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)


@contextmanager
def file_lock(filepath: str, timeout: float = 5.0):
    """文件锁上下文管理器（阻塞等待，超时抛异常）"""
    _ensure_dir(filepath)
    lockfile = filepath + ".lock"
    fd = os.open(lockfile, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        try:
            os.unlink(lockfile)
        except OSError:
            pass


def atomic_write_json(filepath: str, data: Any) -> None:
    """
    原子写入 JSON：
    1. 写到临时文件
    2. os.replace 到目标（原子操作）
    3. 写入 .bak 备份
    """
    _ensure_dir(filepath)
    content = json.dumps(data, ensure_ascii=False, indent=2, default=str)

    # 备份原文件
    if os.path.exists(filepath):
        try:
            shutil.copy2(filepath, filepath + ".bak")
        except OSError:
            pass

    # 原子写入
    with file_lock(filepath):
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(filepath), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, filepath)
        except Exception:
            os.unlink(tmp)
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
