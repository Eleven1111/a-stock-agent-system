#!/usr/bin/env python3
"""
胜率统计 & 反馈闭环 — 打板口径
================================
这是整个系统**唯一的反馈闭环**：它决定四维打分/打板信号能不能被验证。
因此它必须用**打板原生口径**衡量，而不是 30/60 天的波段涨幅。

打板的交易现实：T+1 隔日卖出（隔日溢价兑现），核心指标是——
  · 隔日溢价（T+1 开盘 vs 信号日收盘）—— 打板选手真实的兑现点
  · 隔日收益（T+1 收盘 vs 信号日收盘）—— 不卖到收盘的结果
  · 连板晋级率（T+1 是否继续涨停）—— 龙头延续性
  · 期望值 = 胜率×均盈 − 败率×均亏，配合盈亏比
  · 相对沪深300 的 alpha（剔除大盘 beta，看是否真有超额）

关键修复（相对旧版）:
  · 取消「首次穿越 +3% 即永久锁定 win」的结构性向上偏置
  · 结算与信号价均取自**前复权 K 线**，规避送转除权导致的收益失真
  · 阈值对称（±5% / 0）

数据源（纯 urllib，cron-safe）：腾讯前复权 K 线 + 沪深300 指数。

Usage:
  python3 performance_tracker.py                          # 查看统计
  python3 performance_tracker.py --record CODE NAME GRADE PRICE
  python3 performance_tracker.py --json
"""

import json
import sys
import os
from datetime import datetime, date
from typing import Dict, List, Optional, Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'common'))
from state_store import read_json, atomic_write_json, update_json_list, mutate_json
from paths import data_file
from tradeability import limit_pct, round_limit
from a_stock_http import fetch_tencent_kline, DataSourceError

HISTORY_FILE = data_file("stock-triage", "signal_history.json")

# 沪深300 基准（腾讯指数代码 sh000300）
BENCH_MARKET = "sh"
BENCH_CODE = "000300"

HOLD_DAYS = 3  # 打板最长观察窗（隔日为主，最多看到 T+3）


# ========== 纯函数：信号结算逻辑（可单测，不触网）==========

def evaluate_signal(
    signal_close: float,
    future_bars: List[Dict[str, Any]],
    limit_pct_val: float,
    index_signal_close: Optional[float] = None,
    index_future_bars: Optional[List[Dict[str, Any]]] = None,
    hold_days: int = HOLD_DAYS,
) -> Optional[Dict[str, Any]]:
    """
    根据信号日后的前复权 K 线结算一个打板信号。
    future_bars: 信号日**之后**的日 K（含 open/close/high/low），按时间正序。
    至少需要 1 根（T+1）才能结算；不足返回 None（保持 pending）。
    """
    if signal_close <= 0 or not future_bars:
        return None

    t1 = future_bars[0]
    t1_open_prem = round((t1["open"] / signal_close - 1) * 100, 2)
    t1_close_ret = round((t1["close"] / signal_close - 1) * 100, 2)

    # 连板晋级：T+1 收盘是否封在涨停价（信号日收盘为 T+1 的昨收）
    # 复用 tradeability.round_limit（round half up），与可成交性闸门口径一致
    t1_limit_up = round_limit(signal_close, limit_pct_val, up=True)
    promoted = t1["close"] >= t1_limit_up - 0.01

    # 持有窗内（最多 hold_days）极值与终值
    window = future_bars[:hold_days]
    hz_ret = round((window[-1]["close"] / signal_close - 1) * 100, 2)
    max_gain = round((max(b["high"] for b in window) / signal_close - 1) * 100, 2)
    max_drawdown = round((min(b["low"] for b in window) / signal_close - 1) * 100, 2)

    # 以 T+1 收盘为主判定结果（打板隔日兑现），阈值对称
    if t1_close_ret >= 5:
        outcome = "win_big"
    elif t1_close_ret >= 0:
        outcome = "win"
    elif t1_close_ret > -5:
        outcome = "loss"
    else:
        outcome = "loss_big"

    # 相对沪深300 的 T+1 alpha
    alpha_t1 = None
    if index_signal_close and index_signal_close > 0 and index_future_bars:
        idx_t1 = round((index_future_bars[0]["close"] / index_signal_close - 1) * 100, 2)
        alpha_t1 = round(t1_close_ret - idx_t1, 2)

    return {
        "outcome": outcome,
        "t1_open_premium": t1_open_prem,
        "t1_close_ret": t1_close_ret,
        "promoted": promoted,
        "horizon_ret": hz_ret,
        "max_gain": max_gain,
        "max_drawdown": max_drawdown,
        "alpha_t1": alpha_t1,
        "bars_observed": len(window),
        "resolved": True,
    }


# ========== 持久化 ==========

def load_history() -> List[Dict]:
    return read_json(HISTORY_FILE, [])


def save_history(records: List[Dict]):
    atomic_write_json(HISTORY_FILE, records)


def record_signal(code: str, name: str, grade: str, score: float, price: float) -> Dict:
    """记录一个新信号。price 为信号日收盘价（仅留档；结算以前复权 K 线为准）。

    用 update_json_list 在单锁内完成"读-追加-写回"，避免并发 --record 互相覆盖丢记录。
    """
    update_json_list(HISTORY_FILE, {
        "code": code, "name": name, "grade": grade, "score": score,
        "signal_date": date.today().isoformat(),
        "signal_price": price,
        "outcome": "pending",
    })
    return {"ok": True, "recorded": f"{name}({code}) {grade}级 @ {price}"}


def _fetch_future_bars(code: str, signal_date: str, market: str) -> Optional[Dict[str, Any]]:
    """取信号日收盘价(前复权) + 之后的 K 线。"""
    try:
        klines = fetch_tencent_kline(code, market, days=120, ktype="day")
    except DataSourceError:
        return None
    if not klines:
        return None
    idx = next((i for i, k in enumerate(klines) if k["date"] == signal_date), None)
    if idx is None:
        # 信号日尚无 K 线 → 取最后一根 <= signal_date
        prior = [i for i, k in enumerate(klines) if k["date"] <= signal_date]
        if not prior:
            return None
        idx = prior[-1]
    return {"signal_close": klines[idx]["close"], "future": klines[idx + 1:]}


def update_outcomes() -> List[Dict]:
    """重新结算所有 pending 信号（已结算的不再改动）。

    并发安全：网络抓取/结算在锁外完成，仅把结果按 (code, signal_date) 收集；
    最终用 mutate_json 在单锁内重新读取最新历史并就地结算回写。这样既不长时间
    持锁等网络，也不会因"读快照→结算→写回"覆盖掉期间并发追加/结算的记录。
    """
    snapshot = load_history()
    bench_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    resolutions: Dict[tuple, Dict[str, Any]] = {}

    for r in snapshot:
        if r.get("outcome") != "pending":
            continue

        code, sdate = r["code"], r["signal_date"]
        market = "sh" if code.startswith("6") else "sz"

        stock = _fetch_future_bars(code, sdate, market)
        if not stock or not stock["future"]:
            continue  # 还没到 T+1，保持 pending

        if sdate not in bench_cache:
            bench_cache[sdate] = _fetch_future_bars(BENCH_CODE, sdate, BENCH_MARKET)
        bench = bench_cache[sdate]

        result = evaluate_signal(
            signal_close=stock["signal_close"],
            future_bars=stock["future"],
            limit_pct_val=limit_pct(code, r.get("name", "")),
            index_signal_close=bench["signal_close"] if bench else None,
            index_future_bars=bench["future"] if bench else None,
        )
        if result:
            resolutions[(code, sdate)] = result

    if not resolutions:
        return snapshot

    def _apply(records: List[Dict]) -> List[Dict]:
        for r in records:
            if r.get("outcome") != "pending":
                continue
            res = resolutions.get((r["code"], r["signal_date"]))
            if res:
                r.update(res)
        return records

    return mutate_json(HISTORY_FILE, _apply, [])


# ========== 统计 ==========

def _expectancy(rets: List[float]) -> Dict[str, float]:
    """期望值 + 盈亏比（基于 T+1 收盘收益序列）。"""
    if not rets:
        return {"expectancy": 0.0, "payoff_ratio": 0.0}
    wins = [x for x in rets if x >= 0]
    losses = [x for x in rets if x < 0]
    win_rate = len(wins) / len(rets)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss
    payoff = (avg_win / avg_loss) if avg_loss else 0.0
    return {"expectancy": round(expectancy, 2), "payoff_ratio": round(payoff, 2)}


def compute_stats(records: List[Dict]) -> Dict:
    # 仅统计**新口径已结算**记录（含 t1_close_ret）。旧 schema 记录（首穿锁定法、
    # 无 t1_close_ret）一律排除，避免把旧方法的虚高胜率混入新口径，污染"可信的数字"。
    closed = [r for r in records if r.get("t1_close_ret") is not None]
    legacy = sum(1 for r in records
                 if r.get("outcome") not in (None, "pending") and r.get("t1_close_ret") is None)
    if not closed:
        msg = "尚无已结算信号（需至少到 T+1）"
        if legacy:
            msg += f"；另有 {legacy} 条旧口径记录已排除（建议重置 signal_history.json）"
        return {"total_signals": len(records), "closed": 0,
                "pending": len(records) - legacy, "legacy_excluded": legacy, "message": msg}

    rets = [r["t1_close_ret"] for r in closed]
    wins = [r for r in closed if r["outcome"].startswith("win")]
    alphas = [r["alpha_t1"] for r in closed if r.get("alpha_t1") is not None]
    promoted = [r for r in closed if r.get("promoted")]

    by_grade = {}
    for g in ["S", "A", "B", "C"]:
        g_closed = [r for r in closed if r.get("grade") == g]
        g_rets = [r["t1_close_ret"] for r in g_closed]
        g_wins = [r for r in g_closed if r["outcome"].startswith("win")]
        by_grade[g] = {
            "total": len([r for r in records if r.get("grade") == g]),
            "closed": len(g_closed),
            "win_rate": round(len(g_wins) / len(g_closed) * 100, 1) if g_closed else 0,
            "avg_t1_close": round(sum(g_rets) / len(g_rets), 2) if g_rets else 0,
            **_expectancy(g_rets),
        }

    return {
        "metric": "打板口径(T+1隔日)",
        "total_signals": len(records),
        "closed": len(closed),
        "pending": len(records) - len(closed) - legacy,
        "legacy_excluded": legacy,
        "win_rate": round(len(wins) / len(closed) * 100, 1),
        "promote_rate": round(len(promoted) / len(closed) * 100, 1),
        "avg_t1_open_premium": round(
            sum(r["t1_open_premium"] for r in closed
                if r.get("t1_open_premium") is not None) / len(closed), 2),
        "avg_t1_close_ret": round(sum(rets) / len(rets), 2) if rets else 0,
        "avg_alpha_t1": round(sum(alphas) / len(alphas), 2) if alphas else None,
        **_expectancy(rets),
        "by_grade": by_grade,
    }


def format_stats(stats: Dict, records: List[Dict]) -> str:
    lines = ["📈 **打板信号胜率统计（T+1 隔日口径）**",
             f"⏰ {datetime.now().strftime('%Y-%m-%d')}", ""]

    if stats.get("closed", 0) == 0:
        lines.append("尚无已结算信号，继续积累数据...")
        lines.append(f"当前 {stats.get('pending', 0)} 个信号待结算（需至少到 T+1）")
        return "\n".join(lines)

    alpha = stats.get("avg_alpha_t1")
    alpha_str = f"{alpha:+.2f}%" if alpha is not None else "N/A"
    lines.append(f"📊 总信号: {stats['total_signals']} | 已结算: {stats['closed']} | "
                 f"待结算: {stats['pending']}")
    lines.append(f"🎯 胜率: **{stats['win_rate']}%** | 连板晋级率: **{stats['promote_rate']}%**")
    lines.append(f"💰 隔日溢价(均): {stats['avg_t1_open_premium']:+.2f}% | "
                 f"隔日收益(均): {stats['avg_t1_close_ret']:+.2f}%")
    lines.append(f"📐 期望值: **{stats['expectancy']:+.2f}%** | 盈亏比: {stats['payoff_ratio']} | "
                 f"超额(α vs 沪深300): **{alpha_str}**")
    lines.append("")
    lines.append("| 等级 | 信号 | 结算 | 胜率 | 隔日收益 | 期望 |")
    lines.append("|------|------|------|------|----------|------|")
    for g in ["S", "A", "B", "C"]:
        gs = stats["by_grade"].get(g, {})
        if gs.get("total", 0) > 0:
            lines.append(f"| {g} | {gs['total']} | {gs.get('closed', 0)} | "
                         f"{gs.get('win_rate', 0)}% | {gs.get('avg_t1_close', 0):+.2f}% | "
                         f"{gs.get('expectancy', 0):+.2f}% |")

    recent = [r for r in records if r.get("outcome") and r["outcome"] != "pending"][-5:]
    if recent:
        lines.append("\n## 最近结算")
        for r in recent:
            emoji = "✅" if r["outcome"].startswith("win") else "❌"
            promo = " 🏆晋级" if r.get("promoted") else ""
            lines.append(f"  {emoji} {r['name']}({r['code']}) {r['grade']}级 → "
                         f"隔日 {r.get('t1_close_ret', 0):+.1f}%{promo}")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--record", nargs=4, metavar=("CODE", "NAME", "GRADE", "PRICE"),
                   help="记录信号: code name grade price")
    p.add_argument("--score", type=float, default=5.0, help="评分")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    if args.record:
        code, name, grade, price = args.record
        result = record_signal(code, name, grade, args.score, float(price))
        print(json.dumps(result, ensure_ascii=False))
    else:
        records = update_outcomes()
        stats = compute_stats(records)
        if args.json:
            print(json.dumps(stats, ensure_ascii=False, indent=2))
        else:
            print(format_stats(stats, records))
