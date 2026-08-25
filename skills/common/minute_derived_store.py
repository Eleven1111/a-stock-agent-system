#!/usr/bin/env python3
"""
分钟线派生字段 — 按交易日落盘（有界）
======================================
**只存派生值，不存原始分钟条**：5000 只 × 240 分钟的原始分时每天上百 MB，存了也只会
被再算一遍。这里每只票一天只留一行派生结果（约 200 字节），全市场满打满算 < 1.5 MB/天。

有界性两道闸：单日 ``max_codes`` 条上限（超出按代码序截断并记 ``truncated``），以及
``prune_days`` 只保留最近 N 个交易日的文件。两个数都进 config（minute_derived 节）。

写入路径 ``$A_STOCK_STATE_HOME/skills/daban-stock-picker/data/minute_derived/<date>.json``
必须出现在 cron manifest 的 allowed_state_writes 里，否则生产写入会被拦（仓内教训）。
"""

from __future__ import annotations

import os
import re
from typing import Any, Mapping, Optional

from paths import skill_data_dir
from state_store import atomic_write_json, read_json

SCHEMA = "minute_derived_v1"
SKILL = "daban-stock-picker"
DIRNAME = "minute_derived"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DEFAULT_MAX_CODES = 800
DEFAULT_PRUNE_DAYS = 60

# 一行派生记录只保留这些键 —— 白名单而非黑名单，避免把整份分钟条顺手写进去。
#
# ``slots`` 是 5 分钟粒度的增量成交股数曲线（全天 48 个数），不是原始分钟条：
# 封板前累计换手要的是「截至回封时刻的累计量」，而回封时刻当天盘中并不知道
# （炸板次数要收盘后的涨停池才有），所以只能存曲线、事后按 reseal_time 取值。
# 48 个数约 500 字节/票，全市场 800 票 ≈ 0.4 MB/天，比存 240 根原始分时小两个量级。
RECORD_FIELDS = (
    "volume_ratio", "volume_ratio_availability", "volume_ratio_source",
    "slots", "slots_availability", "slots_step_minutes",
)

# (值字段, 跟着它一起搬的元字段)。合并时以「值字段是否 available」为准搬整组，
# 避免出现值来自上午、availability 来自下午这种自相矛盾的行。
_FIELD_GROUPS = (
    ("volume_ratio", ("volume_ratio_availability", "volume_ratio_source")),
    ("slots", ("slots_availability", "slots_step_minutes")),
)


def store_dir() -> str:
    return os.path.join(skill_data_dir(SKILL), DIRNAME)


def store_path(date: str) -> str:
    if not _DATE_RE.match(str(date or "")):
        raise ValueError(f"minute_derived 落盘日期必须是 YYYY-MM-DD，收到 {date!r}")
    return os.path.join(store_dir(), f"{date}.json")


def slim_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """派生结果 → 落盘行（白名单裁剪；缺的键不补默认值，缺就是缺）。"""
    return {key: record[key] for key in RECORD_FIELDS if key in record}


def _is_better(incoming: Any, current: Any) -> bool:
    """本轮的值该不该盖掉已有的？

    - 标量（量比）：新值有效就盖，无效（None）绝不盖 —— 09:50 算出的有效量比，
      不能被 13:15 那轮的一次抓取失败抹掉。
    - 曲线（slots）：**只有更长的曲线才盖**。晚一轮天然覆盖更多时段；若某轮只抓到
      前几根就写回去，会把上午已经攒够的曲线截短，这正是 fail-closed 要防的倒退。
    """
    if incoming is None:
        return False
    if isinstance(incoming, Mapping):
        return len(incoming) >= len(current) if isinstance(current, Mapping) else True
    return True


def merge_records(existing: Optional[Mapping[str, Any]],
                  incoming: Mapping[str, Mapping[str, Any]],
                  max_codes: int = DEFAULT_MAX_CODES) -> dict[str, Any]:
    """已有当日记录 + 本轮新算的 → 合并后的 records（纯函数，可单测）。

    冲突口径：**新值只在自己 available 时才覆盖旧值**。盘中 09:50 那轮算出的量比是
    有效的，13:15 那轮若因为数据窗口不同变成 unavailable，不能把上午的有效值抹掉。
    """
    merged: dict[str, Any] = {
        str(code): dict(row) for code, row in (existing or {}).items()
        if isinstance(row, Mapping)
    }
    for code, row in (incoming or {}).items():
        if not isinstance(row, Mapping):
            continue
        code = str(code).zfill(6)
        slim = slim_record(row)
        if not slim:
            continue
        current = merged.get(code)
        if current is None:
            merged[code] = slim
            continue
        updated = dict(current)
        for value_key, meta_keys in _FIELD_GROUPS:
            if not _is_better(slim.get(value_key), current.get(value_key)):
                continue
            updated[value_key] = slim[value_key]
            for meta_key in meta_keys:
                if meta_key in slim:
                    updated[meta_key] = slim[meta_key]
        merged[code] = updated
    if len(merged) <= int(max_codes):
        return {"records": merged, "truncated": 0}
    keep = sorted(merged)[: int(max_codes)]
    return {"records": {code: merged[code] for code in keep},
            "truncated": len(merged) - len(keep)}


def load_daily(date: str) -> dict[str, Any]:
    """当日落盘记录；文件不存在 / schema 不匹配 → 空表（不抛，调用方按 unavailable 处理）。"""
    payload = read_json(store_path(date), default=None)
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return {}
    records = payload.get("records")
    return records if isinstance(records, dict) else {}


def write_daily(date: str, incoming: Mapping[str, Mapping[str, Any]],
                max_codes: int = DEFAULT_MAX_CODES) -> dict[str, Any]:
    """合并写入当日派生记录，返回落盘后的元信息（不含 records 全量，避免日志爆炸）。"""
    merged = merge_records(load_daily(date), incoming, max_codes=max_codes)
    payload = {
        "schema": SCHEMA,
        "date": str(date),
        "count": len(merged["records"]),
        "truncated": merged["truncated"],
        "records": merged["records"],
    }
    atomic_write_json(store_path(date), payload)
    return {"path": store_path(date), "date": str(date),
            "count": payload["count"], "truncated": payload["truncated"]}


def prune(keep_days: int = DEFAULT_PRUNE_DAYS) -> list[str]:
    """只留最近 keep_days 个日期文件，返回删掉的文件名（目录不存在 → 空）。"""
    directory = store_dir()
    if not os.path.isdir(directory):
        return []
    dated = sorted(
        name for name in os.listdir(directory)
        if name.endswith(".json") and _DATE_RE.match(name[:-5])
    )
    removed: list[str] = []
    for name in dated[: max(0, len(dated) - int(keep_days))]:
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            continue
        removed.append(name)
    return removed


def lookup(date: str, code: str, cache: Optional[dict[str, dict[str, Any]]] = None
           ) -> dict[str, Any]:
    """(date, code) → 派生行；缺 → {}。``cache`` 传入可避免逐事件重读同一天的文件。"""
    if cache is None:
        records = load_daily(date)
    else:
        if date not in cache:
            cache[date] = load_daily(date)
        records = cache[date]
    row = records.get(str(code).zfill(6))
    return dict(row) if isinstance(row, Mapping) else {}


__all__ = [
    "SCHEMA", "RECORD_FIELDS", "DEFAULT_MAX_CODES", "DEFAULT_PRUNE_DAYS",
    "store_dir", "store_path", "slim_record", "merge_records",
    "load_daily", "write_daily", "prune", "lookup",
]
