"""大面股库：专门收集失败模式（升级方案 P6，源自原书第十四课）。

原书的要求很具体：**不要只收集成功龙头，要专门复盘"当天涨停、隔日从高位出现超过
15% 左右大幅下跌"的大面案例**。理由是逆向思维——只研究成功案例会得到一套无法证伪
的叙事，而失败案例是唯一能告诉你"这套逻辑在什么条件下会崩"的样本。

淘股吧手册里的幸存者偏差警告是同一件事：亏钱的帖子少发或不发，于是公开可见的方法论
天然偏乐观。系统里如果只有候选池和命中记录、没有大面库，复盘就只能重复这个偏差。

口径（全部可配置，默认对齐原书）：
- **入库条件**：T 日封板 ∧ T+1 日从当日最高价回撤 >= ``drawdown_pct``（默认 15%）。
  用**日内最高→收盘**的回撤而不是收盘涨跌幅——一只 T+1 高开 5% 然后跌停的票，
  收盘跌幅口径下只有 -5% 上下，完全看不出它把追进去的人埋了多深。
- **每条记录**带情绪状态、板位、题材、以及触发当日的可观测事实，供后续分层统计。

三条纪律（与仓内其他模块一致）：
1. 判定所需字段缺失 → 该条记录标 ``unavailable`` 并计数，**不猜、不用收盘价顶替最高价**；
2. append-only，同一 (date, code) 幂等——重跑不产生重复行；
3. 样本量不足时 ``summarize`` 明确返回 UNVERIFIED，不给分层结论（30 条门槛与
   state_pnl / tail_risk_metrics 同口径）。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

AVAILABLE = "available"
UNAVAILABLE = "unavailable"
UNVERIFIED = "UNVERIFIED"

SCHEMA = "big_loss_ledger_v1"

#: 判定一条大面所必需的字段。缺任一项即无法判定——不允许用别的字段近似顶替。
REQUIRED_FIELDS = ("date", "code", "next_high", "next_close")

__all__ = ["AVAILABLE", "UNAVAILABLE", "UNVERIFIED", "SCHEMA", "REQUIRED_FIELDS",
           "drawdown_from_high_pct", "classify_event", "collect", "merge_records",
           "summarize"]


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def drawdown_from_high_pct(high: Any, close: Any) -> dict[str, Any]:
    """T+1 日内最高 → 收盘的回撤百分比（负数）。

    刻意不用"收盘涨跌幅"：一只 T+1 高开 5% 后跌停的票，收盘口径只有 -5% 上下，
    完全看不出它把在高位追进去的人埋了多深，而那才是大面的实际伤害。
    """
    high_value, close_value = _num(high), _num(close)
    if high_value is None or close_value is None:
        return {"status": UNAVAILABLE, "value": None, "reason": "missing_price"}
    if high_value <= 0:
        return {"status": UNAVAILABLE, "value": None, "reason": "non_positive_high"}
    return {"status": AVAILABLE,
            "value": round((close_value / high_value - 1.0) * 100.0, 4),
            "reason": None}


def classify_event(record: Mapping[str, Any], *, drawdown_pct: float = -15.0
                   ) -> dict[str, Any]:
    """单条事件是否入库。``drawdown_pct`` 取负值（默认 −15%，原书口径）。"""
    missing = [field for field in REQUIRED_FIELDS if record.get(field) in (None, "")]
    if missing:
        return {"status": UNAVAILABLE, "is_big_loss": None,
                "missing_fields": missing, "drawdown_pct": None,
                "reason": "required_fields_missing"}
    drawdown = drawdown_from_high_pct(record.get("next_high"), record.get("next_close"))
    if drawdown["status"] != AVAILABLE:
        return {"status": UNAVAILABLE, "is_big_loss": None,
                "missing_fields": [], "drawdown_pct": None,
                "reason": drawdown["reason"]}
    value = float(drawdown["value"])
    return {
        "status": AVAILABLE,
        "is_big_loss": value <= float(drawdown_pct),
        "drawdown_pct": value,
        "threshold_pct": float(drawdown_pct),
        "missing_fields": [],
        "reason": None,
    }


def _key(record: Mapping[str, Any]) -> tuple[str, str]:
    return str(record.get("date") or ""), str(record.get("code") or "").zfill(6)


def collect(events: Sequence[Mapping[str, Any]], *, drawdown_pct: float = -15.0
            ) -> dict[str, Any]:
    """从涨停事件里筛出大面样本。

    返回同时带 ``undecidable`` 计数——"没有大面"和"判不了"必须可区分，否则一份
    因为缺字段而空的库会被读成"这段时间没人吃面"。
    """
    hits: list[dict[str, Any]] = []
    undecidable: list[dict[str, Any]] = []
    examined = 0
    for record in events or ():
        if not isinstance(record, Mapping):
            continue
        examined += 1
        verdict = classify_event(record, drawdown_pct=drawdown_pct)
        if verdict["status"] != AVAILABLE:
            undecidable.append({"date": record.get("date"), "code": record.get("code"),
                                "reason": verdict["reason"],
                                "missing_fields": verdict["missing_fields"]})
            continue
        if not verdict["is_big_loss"]:
            continue
        hits.append({
            "date": str(record.get("date")),
            "code": str(record.get("code")).zfill(6),
            "name": record.get("name"),
            "drawdown_pct": verdict["drawdown_pct"],
            "board_level": record.get("board_level"),
            "sector": record.get("sector"),
            "sentiment_state": record.get("sentiment_state"),
            "first_seal_time": record.get("first_seal_time"),
            "open_board_count": record.get("open_board_count"),
        })
    return {
        "schema": SCHEMA,
        "threshold_pct": float(drawdown_pct),
        "examined": examined,
        "records": sorted(hits, key=_key),
        "undecidable": undecidable,
        "undecidable_count": len(undecidable),
    }


def merge_records(existing: Iterable[Mapping[str, Any]],
                  incoming: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """append-only 合并，(date, code) 幂等。

    同键重复出现时保留**已有**记录：重跑同一天不应改写历史判定，否则库里的样本会
    随着上游数据的每次修订而漂移，分层统计就没有可复现性了。
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for record in existing or ():
        if isinstance(record, Mapping):
            merged[_key(record)] = dict(record)
    for record in incoming or ():
        if not isinstance(record, Mapping):
            continue
        key = _key(record)
        if key in merged:
            continue
        merged[key] = dict(record)
    return [merged[key] for key in sorted(merged)]


def summarize(records: Sequence[Mapping[str, Any]], *, group_by: str = "sentiment_state",
              min_samples: int = 30) -> dict[str, Any]:
    """按维度分层统计。样本不足 → UNVERIFIED 且**扣住均值**。

    大面库的价值在于"哪种条件下更容易吃面"，那是一个需要样本量的结论；30 条以下
    给出的分层均值只会制造一种看起来很懂的错觉。
    """
    total = [record for record in (records or ()) if isinstance(record, Mapping)]
    if not total:
        return {"status": UNAVAILABLE, "n": 0, "groups": {},
                "reason": "empty_ledger"}
    if len(total) < int(min_samples):
        return {"status": UNVERIFIED, "n": len(total), "groups": {},
                "withheld_reason": f"n<{int(min_samples)}",
                "reason": "insufficient_sample"}
    groups: dict[str, dict[str, Any]] = {}
    for record in total:
        key = str(record.get(group_by) or "unknown")
        value = _num(record.get("drawdown_pct"))
        if value is None:
            continue
        groups.setdefault(key, {"n": 0, "sum": 0.0})
        groups[key]["n"] += 1
        groups[key]["sum"] += value
    return {
        "status": AVAILABLE,
        "n": len(total),
        "group_by": group_by,
        "groups": {
            key: {"n": stats["n"],
                  "mean_drawdown_pct": round(stats["sum"] / stats["n"], 4)}
            for key, stats in sorted(groups.items())
        },
        "reason": None,
    }
