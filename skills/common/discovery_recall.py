#!/usr/bin/env python3
"""D0 → 竞价 → 开盘 的漏斗召回统计（纯函数，不触网、不落盘）。

这些函数原先住在 skills/daban-stock-picker/scripts/auction_collector.py 里，
但它们与「竞价采集」无关：只是对已捕获的产物做口径统计。放在采集器里的代价是
scripts/discovery_recall_report.py 为了调一个纯函数，得把整个采集器连同它的
网络/落盘依赖一起拖进来，并为此做一次 sys.path 手术（PR #192 引入）。

迁到 common 之后，调用方走既有的 `import skills.common` 引导即可。
采集器仍需要 `_code_set` / `is_recall_target_event`，反向从这里 import。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional

import skills.common  # noqa: F401,E402  -- 使本模块可被独立加载
import candidate_pipeline  # noqa: E402
from tradeability import limit_pct  # noqa: E402


def _code_set(values: Iterable[Any]) -> set[str]:
    result: set[str] = set()
    for value in values:
        raw = value.get("code") if isinstance(value, Mapping) else value
        if raw:
            result.add(candidate_pipeline.naked_code(raw))
    return result


def _quote_change_pct(quote: Mapping[str, Any]) -> Optional[float]:
    try:
        if quote.get("change_pct") is not None:
            return float(quote["change_pct"])
        previous = float(quote.get("prev_close") or 0)
        price = float(quote.get("price") or 0)
        return (price - previous) / previous * 100 if previous > 0 else None
    except (TypeError, ValueError):
        return None


def is_recall_target_event(quote: Mapping[str, Any]) -> bool:
    """Return whether a 09:24 quote is a measurable strong-board event.

    The monitor is deliberately descriptive: it recognises an explicit
    provider flag when available, otherwise uses the quote's limit percentage
    and a conservative near-limit threshold.  It never feeds this flag into
    ranking or execution gates.
    """
    if any(quote.get(key) is True for key in ("target_event", "is_limit_up", "strong_board")):
        return True
    change = _quote_change_pct(quote)
    if change is None:
        return False
    code = candidate_pipeline.naked_code(quote.get("code"))
    name = str(quote.get("name") or "")
    try:
        limit_gap = float(limit_pct(code, name))
    except (TypeError, ValueError):
        limit_gap = 10.0
    # A near-limit event captures both ordinary 10cm boards and 20cm boards;
    # the 7% floor keeps the metric useful for strong (but not yet sealed)
    # boards while excluding routine small advances.
    return change >= 7.0 or change >= min(9.5, limit_gap - 0.5)


def _recall_rate(covered: int, total: int) -> Optional[float]:
    return round(covered / total, 4) if total else None


def _stage_payload(
    target_codes: set, codes: set, *, available: bool = True
) -> Dict[str, Any]:
    """One funnel stage's coverage; an unavailable stage reports None, not zero."""
    covered = len(target_codes & codes)
    return {
        "available": available,
        "target_count": len(target_codes),
        "covered_count": covered if available else None,
        "lost_count": (len(target_codes) - covered) if available else None,
        "recall": _recall_rate(covered, len(target_codes)) if available else None,
        "covered_codes": sorted(target_codes & codes) if available else [],
    }


def _would_have_been_count(rows: List[Mapping[str, Any]], outside: List[str]) -> int:
    """Rows the pool missed: an explicit upstream flag, else membership in ``outside``."""
    return sum(
        1
        for item in rows
        if item.get("would_have_been_candidate") is True
        or (
            "would_have_been_candidate" not in item
            and item.get("code")
            and candidate_pipeline.naked_code(item.get("code")) in outside
        )
    )


def build_discovery_recall_report(
    quotes: Iterable[Mapping[str, Any]],
    *,
    prefilter_codes: Iterable[Any],
    auction_codes: Iterable[Any],
    executable_codes: Iterable[Any] | None = None,
    open_codes: Iterable[Any] | None = None,
    asof: str,
    source_stage: str = "09:24_full_market",
    generated_at: str | None = None,
) -> Dict[str, Any]:
    """Build the bounded, fail-closed recall report for one trading date."""
    rows = [dict(item) for item in quotes]
    target_codes = {
        candidate_pipeline.naked_code(item.get("code"))
        for item in rows
        if item.get("code") and is_recall_target_event(item)
    }
    prefilter = _code_set(prefilter_codes)
    auction = _code_set(auction_codes)
    executable = _code_set(executable_codes or [])
    opened = _code_set(open_codes or []) if open_codes is not None else None

    d0 = _stage_payload(target_codes, prefilter)
    auction_stage = _stage_payload(target_codes, auction)
    executable_stage = _stage_payload(
        target_codes, executable, available=executable_codes is not None,
    )
    open_stage = _stage_payload(target_codes, opened or set(), available=opened is not None)
    outside = sorted(target_codes - prefilter)
    staged_loss = {
        "target_count": len(target_codes),
        "outside_pool_strong_count": len(outside),
        "outside_pool_strong_codes": outside[:200],
        "loss_by_stage": {
            "d0_prefilter_loss_count": d0["lost_count"],
            "auction_pool_loss_count": auction_stage["lost_count"],
            "executable_loss_count": executable_stage["lost_count"],
            "open_confirmation_loss_count": open_stage["lost_count"],
        },
        "d0_prefilter": d0,
        "auction": auction_stage,
        "open": open_stage,
        "d0_to_auction_lost_count": len((target_codes & prefilter) - auction),
        "auction_to_open_lost_count": (
            len((target_codes & auction) - (opened or set())) if opened is not None else None
        ),
        "open_pending": opened is None,
    }
    return {
        "schema": "discovery_recall_report_v1",
        "asof": asof,
        "generated_at": generated_at or datetime.now().isoformat(timespec="seconds"),
        "source_stage": source_stage,
        "status": "ready" if opened is not None else "pending",
        "target_event": "near_limit_or_strong_board",
        "target_count": len(target_codes),
        "discovery_recall": d0["recall"],
        "auction_recall": auction_stage["recall"],
        "executable_recall": executable_stage["recall"],
        "open_recall": open_stage["recall"],
        "staged_loss": staged_loss,
        "coverage": {
            "d0_prefilter": d0,
            "auction_pool": auction_stage,
            "executable": executable_stage,
            "open_confirmation": open_stage,
        },
        "outside_pool_strong_count": len(outside),
        "outside_pool_strong_codes": outside[:200],
        "would_have_been_candidate_count": _would_have_been_count(rows, outside),
        "execution_gate_unchanged": True,
        "note": "召回统计仅用于优化D0预筛阈值，不扩大竞价池，不进入执行排名",
    }
