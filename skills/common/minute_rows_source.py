#!/usr/bin/env python3
"""
分钟行来源解析 — 把「(交易日, 代码) → 规范分钟行」这件事收敛到一处
====================================================================
两条路，可得性完全不同，**都不许互相冒充**：

- ``store``（路径 B，向前累积）：读 minute_derived_store 落盘的 5 分钟增量曲线。
  只覆盖落盘作业上线之后的交易日，历史部分本来就没有 —— 缺就是缺。
- ``sina``（路径 A，历史回填）：新浪分钟 K 历史。实测（2026-08-25）深度上限
  1023 根：scale=5 约 22 个交易日、scale=1 约 5 个交易日，且接口不支持翻页。
  **超出这个窗口的历史，这条路也拿不到**，返回空让上游标 unavailable。

mootdx（通达信 TCP）在 2026-08-25 的探测里 38 个节点全部 ``bars()`` 返回空
（``stocks()`` 正常，是仓内已记录的 bestip 坑），因此本模块**不**把它当分钟来源；
真要加，先补一次能取到分钟深度的实测再说，别照抄「TDX 支持分钟频率」的文档结论。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import minute_derived as md
import minute_derived_store as store
from a_stock_http import fetch_sina_minute_history

Key = Tuple[str, str]

MODE_STORE = "store"
MODE_SINA = "sina"
MODE_AUTO = "auto"
MODE_NONE = "none"


def _market(code: str) -> str:
    return "sh" if str(code).zfill(6).startswith("6") else "sz"


def _event_keys(raw_events: Iterable[Mapping[str, Any]]) -> List[Key]:
    keys: List[Key] = []
    seen = set()
    for event in raw_events or []:
        date = str(event.get("date") or "").strip()
        code = str(event.get("code") or "").zfill(6)
        if not date or code == "000000" or (date, code) in seen:
            continue
        seen.add((date, code))
        keys.append((date, code))
    return keys


def rows_from_store(keys: Iterable[Key]) -> Dict[Key, List[Dict[str, Any]]]:
    """落盘曲线 → 规范行。曲线缺失/损坏的键直接不出现在结果里（上游按 unavailable 处理）。"""
    cache: Dict[str, Dict[str, Any]] = {}
    out: Dict[Key, List[Dict[str, Any]]] = {}
    for date, code in keys:
        record = store.lookup(date, code, cache=cache)
        rows = md.slots_to_rows(record.get("slots"))
        if rows:
            out[(date, code)] = rows
    return out


def rows_from_sina(keys: Iterable[Key], scale: int = 5, sleep: float = 0.2
                   ) -> Dict[Key, List[Dict[str, Any]]]:
    """新浪历史分钟 K → 规范行。每个代码只请求一次（一次返回 22 个交易日），按日切分。"""
    wanted: Dict[str, set] = {}
    for date, code in keys:
        wanted.setdefault(code, set()).add(date)
    out: Dict[Key, List[Dict[str, Any]]] = {}
    for code, dates in wanted.items():
        raw = fetch_sina_minute_history(code, market=_market(code), scale=scale)
        time.sleep(sleep)
        by_date: Dict[str, List[Dict[str, Any]]] = {}
        for row in raw:
            by_date.setdefault(str(row.get("day", ""))[:10], []).append(row)
        for date in dates:
            rows = md.normalize_sina_minute(by_date.get(date))
            if rows:
                out[(date, code)] = rows
    return out


def collect(raw_events: Iterable[Mapping[str, Any]], mode: str = MODE_AUTO,
            scale: int = 5, sleep: float = 0.2
            ) -> Tuple[Dict[Key, List[Dict[str, Any]]], Dict[str, Any]]:
    """(事件表原始事件, 模式) → ({(date, code): 规范行}, 诊断)。

    ``auto``：先吃落盘（免费、无网络），再用新浪补齐剩下的键。诊断里分别记两条路各
    命中多少，零命中时要能一眼看出是「历史超出新浪窗口」还是「落盘作业还没跑过」。
    """
    keys = _event_keys(raw_events)
    diagnostics: Dict[str, Any] = {"mode": mode, "requested_keys": len(keys),
                                   "from_store": 0, "from_sina": 0}
    if mode == MODE_NONE or not keys:
        diagnostics["covered_keys"] = 0
        return {}, diagnostics

    rows: Dict[Key, List[Dict[str, Any]]] = {}
    if mode in (MODE_STORE, MODE_AUTO):
        rows.update(rows_from_store(keys))
        diagnostics["from_store"] = len(rows)
    if mode in (MODE_SINA, MODE_AUTO):
        missing = [key for key in keys if key not in rows]
        fetched = rows_from_sina(missing, scale=scale, sleep=sleep) if missing else {}
        rows.update(fetched)
        diagnostics["from_sina"] = len(fetched)
    diagnostics["covered_keys"] = len(rows)
    diagnostics["coverage_ratio"] = round(len(rows) / len(keys), 4) if keys else 0.0
    return rows, diagnostics


def derived_records(minute_rows: Mapping[str, List[Dict[str, Any]]],
                    baselines: Optional[Mapping[str, Any]] = None,
                    checkpoint: str = "09:45",
                    step_minutes: int = 5,
                    source: str = md.SOURCE_TENCENT_INTRADAY
                    ) -> Dict[str, Dict[str, Any]]:
    """{code: 规范分钟行} → {code: 落盘记录}（供路径 B 的落盘挂载点调用）。

    量比基准 ``baselines[code]``（股/分钟）缺失时只落曲线、量比标 unavailable ——
    曲线本身仍然有价值（回封换手要用），不能因为量比算不出来就整条丢掉。
    """
    out: Dict[str, Dict[str, Any]] = {}
    for code, rows in (minute_rows or {}).items():
        slim = md.downsample_rows(rows, step_minutes=step_minutes)
        if not slim:
            continue
        ratio = md.volume_ratio_at(
            slim, checkpoint=checkpoint,
            baseline_per_minute=(baselines or {}).get(code))
        out[str(code).zfill(6)] = {
            "volume_ratio": ratio["value"],
            "volume_ratio_availability": ratio["availability"],
            "volume_ratio_source": f"{source}:{checkpoint}" if ratio["value"] is not None else None,
            "slots": md.rows_to_slots(slim),
            "slots_availability": md.AVAILABLE,
            "slots_step_minutes": int(step_minutes),
        }
    return out


__all__ = ["MODE_STORE", "MODE_SINA", "MODE_AUTO", "MODE_NONE",
           "collect", "rows_from_store", "rows_from_sina", "derived_records"]
