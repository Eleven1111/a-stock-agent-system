"""板块动量与轮动信号 — 全市场行业板块的动量分级 + 资金轮动方向。

Issue #89 的架构补缺：系统此前只看个股不看板块，2026-07 医药板块全周爆发
（医药生物 +12.18%）却零信号。本模块把东财行业板块快照（约 500 个板块，一次
clist 请求）折算成机器可读的 `sector_momentum` / `sector_rotation`，落入
signal_context 供情绪面、候选发现与简报消费。

设计口径（单快照自足，不依赖本地历史文件——避免缓存退化成 no-op 的老坑）：
- 东财 clist 一次给出 1日/5日涨幅 与 1日/5日主力净额，前4日净额 = 5日 - 1日；
- 轮动 = 「今日净流入排名」相对「前4日净流入排名」的位移，无需滚动状态；
- 指数 5 日基准来自上证指数日 K（6 根收盘价）。

信号分级：
- strong:       5日涨幅 > 10% 且跑赢大盘 > 5pp（板块主升）
- emerging:     当日涨幅 ≥ 3% 且当日主力净流入 且 5日涨幅 ≥ 3%（爆发初期 Day1-2）
- weakening:    5日涨幅 > 10% 但当日回调 ≤ -2%（高位退潮）
- rotating_out: 当日净流出>2亿 且 前4日净流出>5亿 且 当日跌>1%（显著资金撤离；
                A股板块主力常态小额净流出，宽阈值会命中上百板块全是噪声）
- neutral:      其余
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

SCHEMA = "sector_momentum_v1"
ROTATION_SCHEMA = "sector_rotation_v1"

# signal_context 是共享缓存，只保留有信息量的板块，控制体积。
MAX_SECTORS_IN_CONTEXT = 30
ROTATION_TOP_N = 5


def _num(value: Any) -> Optional[float]:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_board_rows(diff: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """东财 clist diff 行 → 标准化板块指标行。

    字段：f12 板块代码 / f14 名称 / f3 当日涨跌幅% / f109 5日涨跌幅% /
    f8 换手率% / f62 当日主力净额(元) / f164 5日主力净额(元) /
    f104 上涨家数 / f105 下跌家数。净额统一折算为亿。
    """
    rows: list[dict[str, Any]] = []
    for item in diff or []:
        name = str(item.get("f14") or "").strip()
        return_1d = _num(item.get("f3"))
        if not name or return_1d is None:
            continue
        net_1d = _num(item.get("f62"))
        net_5d = _num(item.get("f164"))
        net_inflow_1d = round(net_1d / 1e8, 2) if net_1d is not None else None
        net_inflow_5d = round(net_5d / 1e8, 2) if net_5d is not None else None
        prior_4d = (
            round(net_inflow_5d - net_inflow_1d, 2)
            if net_inflow_5d is not None and net_inflow_1d is not None
            else None
        )
        rows.append({
            "code": str(item.get("f12") or ""),
            "name": name,
            "return_1d": return_1d,
            "return_5d": _num(item.get("f109")),
            "turnover_pct": _num(item.get("f8")),
            "net_inflow_1d": net_inflow_1d,
            "net_inflow_5d": net_inflow_5d,
            "net_inflow_prior_4d": prior_4d,
            "up_count": int(_num(item.get("f104")) or 0),
            "down_count": int(_num(item.get("f105")) or 0),
        })
    return rows


def classify_sector_signal(row: Mapping[str, Any],
                           index_return_5d: Optional[float]) -> dict[str, Any]:
    """单板块指标 → 动量信号分级。返回 {signal, reason, vs_index_5d}。"""
    return_1d = _num(row.get("return_1d")) or 0.0
    return_5d = _num(row.get("return_5d"))
    net_1d = _num(row.get("net_inflow_1d"))
    prior_4d = _num(row.get("net_inflow_prior_4d"))
    vs_index = (
        round(return_5d - index_return_5d, 2)
        if return_5d is not None and index_return_5d is not None
        else None
    )

    if return_5d is not None and return_5d > 10:
        if return_1d <= -2:
            return {
                "signal": "weakening",
                "reason": f"5日涨幅{return_5d:.1f}%但当日回调{return_1d:.1f}%",
                "vs_index_5d": vs_index,
            }
        if vs_index is not None and vs_index > 5:
            return {
                "signal": "strong",
                "reason": f"5日涨幅{return_5d:.1f}%且强于大盘{vs_index:.1f}pp",
                "vs_index_5d": vs_index,
            }
    if (return_1d >= 3
            and net_1d is not None and net_1d > 0
            and return_5d is not None and return_5d >= 3):
        return {
            "signal": "emerging",
            "reason": f"当日涨{return_1d:.1f}%+主力净流入{net_1d:.1f}亿，5日{return_5d:.1f}%",
            "vs_index_5d": vs_index,
        }
    if (net_1d is not None and net_1d < -2
            and prior_4d is not None and prior_4d < -5
            and return_1d < -1):
        return {
            "signal": "rotating_out",
            "reason": f"主力连续净流出(当日{net_1d:.1f}亿/前4日{prior_4d:.1f}亿)且板块下跌",
            "vs_index_5d": vs_index,
        }
    return {"signal": "neutral", "reason": "", "vs_index_5d": vs_index}


def build_sector_momentum(rows: Sequence[Mapping[str, Any]],
                          *,
                          index_return_5d: Optional[float],
                          trading_date: str,
                          sector_limitups: Optional[Mapping[str, Any]] = None,
                          max_sectors: int = MAX_SECTORS_IN_CONTEXT) -> dict[str, Any]:
    """全部板块行 → sector_momentum 载荷。

    保留策略：非 neutral 信号板块优先（strong→emerging→weakening→rotating_out），
    其余按 |5日涨幅| 补齐；总量硬顶 max_sectors（signal_context 是共享缓存，
    体积失控会拖垮 runtime context 投影）。signal_counts 统计截断前的全量。
    """
    limitups = dict(sector_limitups or {})
    scored: list[dict[str, Any]] = []
    for row in rows:
        verdict = classify_sector_signal(row, index_return_5d)
        entry = {
            "name": row.get("name"),
            "code": row.get("code"),
            "return_1d": row.get("return_1d"),
            "return_5d": row.get("return_5d"),
            "vs_index_5d": verdict["vs_index_5d"],
            "turnover_pct": row.get("turnover_pct"),
            "net_inflow_1d": row.get("net_inflow_1d"),
            "net_inflow_5d": row.get("net_inflow_5d"),
            "limitup_count": int(limitups.get(str(row.get("name")), 0) or 0),
            "signal": verdict["signal"],
            "signal_reason": verdict["reason"],
        }
        scored.append(entry)

    signaled = [e for e in scored if e["signal"] != "neutral"]
    neutral = [e for e in scored if e["signal"] == "neutral"]
    neutral.sort(key=lambda e: -abs(e.get("return_5d") or 0.0))
    order = {"strong": 0, "emerging": 1, "weakening": 2, "rotating_out": 3}
    signaled.sort(key=lambda e: (order.get(e["signal"], 9),
                                 -(e.get("return_5d") or 0.0)))
    kept = (signaled + neutral)[:max_sectors]

    return {
        "schema": SCHEMA,
        "trading_date": trading_date,
        "index_return_5d": index_return_5d,
        "total_sectors": len(scored),
        "sectors": kept,
        "signal_counts": {
            key: sum(1 for e in signaled if e["signal"] == key)
            for key in ("strong", "emerging", "weakening", "rotating_out")
        },
    }


def detect_sector_rotation(rows: Sequence[Mapping[str, Any]],
                           *,
                           trading_date: str,
                           top_n: int = ROTATION_TOP_N) -> dict[str, Any]:
    """资金轮动：当日净流入排名 vs 前4日净流入排名的位移。

    排名显著上移 = 资金流入方向；显著下移 = 资金流出方向。
    """
    usable = [
        row for row in rows
        if _num(row.get("net_inflow_1d")) is not None
        and _num(row.get("net_inflow_prior_4d")) is not None
    ]
    if len(usable) < 10:
        return {
            "schema": ROTATION_SCHEMA,
            "trading_date": trading_date,
            "status": "insufficient_data",
            "inflow_sectors": [],
            "outflow_sectors": [],
            "rotation_signal": "",
        }

    by_today = sorted(usable, key=lambda r: -float(r["net_inflow_1d"]))
    by_prior = sorted(usable, key=lambda r: -float(r["net_inflow_prior_4d"]))
    rank_today = {r["name"]: i for i, r in enumerate(by_today)}
    rank_prior = {r["name"]: i for i, r in enumerate(by_prior)}

    shifts = []
    for row in usable:
        name = row["name"]
        shifts.append({
            "name": name,
            "rank_shift": rank_prior[name] - rank_today[name],
            "net_inflow_1d": float(row["net_inflow_1d"]),
        })

    # 流入方向：排名上移最快且今日确实净流入；流出方向对称。
    inflow = sorted(
        (s for s in shifts if s["rank_shift"] > 0 and s["net_inflow_1d"] > 0),
        key=lambda s: (-s["rank_shift"], -s["net_inflow_1d"]),
    )[:top_n]
    outflow = sorted(
        (s for s in shifts if s["rank_shift"] < 0 and s["net_inflow_1d"] < 0),
        key=lambda s: (s["rank_shift"], s["net_inflow_1d"]),
    )[:top_n]

    signal = ""
    if inflow or outflow:
        parts = []
        if inflow:
            parts.append("流入：" + "、".join(s["name"] for s in inflow))
        if outflow:
            parts.append("流出：" + "、".join(s["name"] for s in outflow))
        signal = "；".join(parts)

    return {
        "schema": ROTATION_SCHEMA,
        "trading_date": trading_date,
        "status": "ok",
        "inflow_sectors": [s["name"] for s in inflow],
        "outflow_sectors": [s["name"] for s in outflow],
        "detail": {
            "inflow": inflow,
            "outflow": outflow,
        },
        "rotation_signal": signal,
    }


def index_return_from_klines(klines: Sequence[str]) -> Optional[float]:
    """上证指数日K行（'date,close'）→ 5日涨跌幅%。需要 ≥6 根K线。"""
    closes = []
    for line in klines or []:
        parts = str(line).split(",")
        if len(parts) >= 2:
            close = _num(parts[1])
            if close is not None:
                closes.append(close)
    if len(closes) < 6 or closes[-6] <= 0:
        return None
    return round((closes[-1] / closes[-6] - 1) * 100, 2)


def momentum_boost(sector: Optional[str],
                   momentum: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """个股所属板块的动量加成（供 sentiment_boost 消费）。

    strong +1.0 / emerging +0.5 / weakening -0.3 / rotating_out -0.5。
    """
    if not sector or not isinstance(momentum, Mapping):
        return {"delta": 0.0, "note": None}
    entry = next(
        (e for e in momentum.get("sectors") or []
         if str(e.get("name")) == str(sector)),
        None,
    )
    if not entry:
        return {"delta": 0.0, "note": None}
    signal = str(entry.get("signal") or "neutral")
    deltas = {"strong": 1.0, "emerging": 0.5, "weakening": -0.3, "rotating_out": -0.5}
    delta = deltas.get(signal, 0.0)
    if delta == 0.0:
        return {"delta": 0.0, "note": None}
    labels = {
        "strong": "板块主升",
        "emerging": "板块启动",
        "weakening": "板块高位退潮",
        "rotating_out": "板块资金撤离",
    }
    note = f"{labels[signal]}({sector}: {entry.get('signal_reason', '')})"
    return {"delta": delta, "note": note}
