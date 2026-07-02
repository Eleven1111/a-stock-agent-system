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

数据源（共享 data-access 层，cron-safe）：腾讯前复权 K 线 + 沪深300 指数。

Usage:
  python3 performance_tracker.py                          # 查看统计
  python3 performance_tracker.py --record CODE NAME GRADE PRICE
  python3 performance_tracker.py --record CODE NAME GRADE PRICE --strategy-id daban:first_board_reseal
  python3 performance_tracker.py --json
"""

import json
import os
import sys
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Any, Mapping

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'common'))
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.insert(0, ROOT)
from state_store import read_json, atomic_write_json, update_json_list, mutate_json
from paths import data_file
from tradeability import limit_pct, round_limit
from a_stock_http import fetch_tencent_kline, DataSourceError
import signal_ledger
from scripts.cron_budget_report import build_push_report, read_push_telemetry

HISTORY_FILE = data_file("stock-triage", "signal_history.json")
LEDGER_FILE = signal_ledger.LEDGER_FILE

# 沪深300 基准（腾讯指数代码 sh000300）
BENCH_MARKET = "sh"
BENCH_CODE = "000300"

HOLD_DAYS = 3  # 打板最长观察窗（隔日为主，最多看到 T+3）
EVIDENCE_PIPELINE_KEYWORDS = (
    "candidate",
    "auction",
    "open-confirmation",
    "social-attention",
    "hot-money",
    "four-dim",
    "capital-flow",
    "catalyst",
    "serenity",
    "news-monitor",
    "official-policy-watch",
    "market-pulse",
    "stock-intelligence",
)


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

    final = len(window) >= hold_days
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
        "settlement_status": "final" if final else "provisional",
        "resolved": final,
    }


# ========== 持久化 ==========

def load_history() -> List[Dict]:
    canonical = signal_ledger.project_signals(ledger_file=LEDGER_FILE)
    legacy = read_json(HISTORY_FILE, [])
    return signal_ledger.merge_legacy_signals(canonical, legacy)


def save_history(records: List[Dict]):
    atomic_write_json(HISTORY_FILE, records)


def record_signal(
    code: str,
    name: str,
    grade: str,
    score: float,
    price: float,
    strategy_id: Optional[str] = None,
    signal_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    recommendation_id: Optional[str] = None,
    trade_id: Optional[str] = None,
    monitor_id: Optional[str] = None,
    signal_date: Optional[str] = None,
) -> Dict:
    """记录一个新信号。price 为信号日收盘价（仅留档；结算以前复权 K 线为准）。

    用 update_json_list 在单锁内完成"读-追加-写回"，避免并发 --record 互相覆盖丢记录。
    """
    links = signal_ledger.make_links(
        recommendation_id,
        correlation_id=correlation_id,
        signal_id=signal_id,
        trade_id=trade_id,
        monitor_id=monitor_id,
    )
    if not links.get("signal_id"):
        seed = {
            "code": code,
            "signal_date": signal_date or date.today().isoformat(),
            "strategy_id": strategy_id or "default",
            "grade": grade,
        }
        links = signal_ledger.legacy_signal_links(seed)
    record = {
        "code": code, "name": name, "grade": grade, "score": score,
        "signal_date": signal_date or date.today().isoformat(),
        "signal_price": price,
        **{key: value for key, value in links.items() if value is not None},
        "outcome": "pending",
    }
    if strategy_id:
        record["strategy_id"] = strategy_id
    signal_ledger.append_event(
        "signal.opened",
        links,
        signal_ledger.signal_opened_event(record, links)["payload"],
        idempotency_key=f"signal.opened:{links['signal_id']}",
        ledger_file=LEDGER_FILE,
    )
    update_json_list(HISTORY_FILE, record, unique_key="signal_id")
    suffix = f" [{strategy_id}]" if strategy_id else ""
    return {"ok": True, "recorded": f"{name}({code}) {grade}级 @ {price}{suffix}"}


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
    resolutions: Dict[str, Dict[str, Any]] = {}
    records_by_id: Dict[str, Dict[str, Any]] = {}

    for r in snapshot:
        if r.get("settlement_status") == "final":
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
            signal_id = signal_ledger.legacy_signal_links(r)["signal_id"]
            resolutions[str(signal_id)] = result
            records_by_id[str(signal_id)] = r

    if not resolutions:
        return snapshot

    ledger_events = []
    for signal_id, resolution in resolutions.items():
        record = records_by_id[signal_id]
        links = signal_ledger.legacy_signal_links(record)
        stage = "t3" if resolution.get("settlement_status") == "final" else "t1"
        ledger_events.extend([
            signal_ledger.signal_opened_event(record, links),
            signal_ledger.settlement_event(record, resolution, stage=stage),
        ])
    signal_ledger.append_events(ledger_events, ledger_file=LEDGER_FILE)

    def _apply(records: List[Dict]) -> List[Dict]:
        if not isinstance(records, list):
            records = []
        seen = set()
        for r in records:
            if r.get("settlement_status") == "final":
                seen.add(signal_ledger.legacy_signal_links(r)["signal_id"])
                continue
            signal_id = str(signal_ledger.legacy_signal_links(r)["signal_id"])
            seen.add(signal_id)
            res = resolutions.get(signal_id)
            if res:
                r.update(res)
                r.update({
                    key: value
                    for key, value in signal_ledger.legacy_signal_links(r).items()
                    if value is not None
                })
        for signal_id, res in resolutions.items():
            if signal_id in seen:
                continue
            compat = dict(records_by_id[signal_id])
            compat.update(res)
            compat.update({
                key: value
                for key, value in signal_ledger.legacy_signal_links(compat).items()
                if value is not None
            })
            records.append(compat)
        return records

    mutate_json(HISTORY_FILE, _apply, [])
    return load_history()


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


def _attribution_returns(
    records: List[Dict],
    *,
    final_only: bool = False,
) -> Dict[str, List[float]]:
    """Direction-normalized returns for research evidence co-occurrence.

    A bullish tag keeps the stock return; a bearish tag negates it. The result
    describes conditional performance when evidence was present, not causal
    contribution to the primary strategy.
    """
    grouped: Dict[str, List[float]] = {}
    for record in records:
        if record.get("t1_close_ret") is None:
            continue
        if final_only and record.get("settlement_status") == "provisional":
            continue
        seen = set()
        for item in record.get("strategy_attributions") or []:
            strategy_id = str(item.get("strategy_id") or "").strip()
            if not strategy_id or strategy_id in seen:
                continue
            seen.add(strategy_id)
            value = float(record["t1_close_ret"])
            if item.get("direction") == "bearish":
                value = -value
            grouped.setdefault(strategy_id, []).append(value)
    return grouped


def _attribution_stats(
    records: List[Dict],
    *,
    final_only: bool = False,
) -> Dict[str, Dict[str, float]]:
    grouped = _attribution_returns(records, final_only=final_only)
    return {
        strategy_id: {
            "total": len(values),
            "closed": len(values),
            "win_rate": round(
                sum(1 for value in values if value >= 0) / len(values) * 100,
                1,
            ) if values else 0,
            "avg_t1_close": round(sum(values) / len(values), 2) if values else 0,
            **_expectancy(values),
        }
        for strategy_id, values in sorted(grouped.items())
    }


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _known_evidence_pipelines() -> set[str]:
    manifest = os.path.join(_repo_root(), "cron", "hermes-cron-manifest.json")
    try:
        with open(manifest, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return set()
    pipelines = set()
    for job in payload.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("id") or "")
        command = str((job.get("run") or {}).get("command") or job.get("command") or "")
        haystack = f"{job_id} {command}"
        if any(keyword in haystack for keyword in EVIDENCE_PIPELINE_KEYWORDS):
            pipelines.add(job_id)
    return pipelines


def _record_date(record: Dict[str, Any]) -> Optional[date]:
    for key in ("signal_date", "date", "created_at", "resolved_at"):
        raw = record.get(key)
        if not raw:
            continue
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
    return None


def _evidence_source_stats(
    records: List[Dict],
    *,
    asof: Optional[str] = None,
    known_evidence_pipelines: Optional[set[str]] = None,
) -> tuple[Dict[str, Dict[str, Any]], List[str]]:
    asof_date = date.fromisoformat(asof) if asof else date.today()
    window_start = asof_date - timedelta(days=30)
    known = set(known_evidence_pipelines or _known_evidence_pipelines())
    grouped: Dict[str, Dict[str, Any]] = {}
    active_recent: set[str] = set()

    for record in records:
        sources = signal_ledger.normalize_evidence_sources(
            record.get("evidence_sources")
        )
        record_day = _record_date(record)
        seen_for_record: set[str] = set()
        for item in sources:
            source = item["source"]
            weight = item["weight_hint"]
            known.add(source)
            bucket = grouped.setdefault(
                source,
                {
                    "primary_recommendations": 0,
                    "t3_closed": 0,
                    "t3_hits": 0,
                    "t3_hit_rate": 0.0,
                    "avg_excess_return": None,
                    "_alphas": [],
                },
            )
            if weight == "primary":
                bucket["primary_recommendations"] += 1
            if (
                weight in {"primary", "supporting"}
                and record_day is not None
                and window_start <= record_day <= asof_date
            ):
                active_recent.add(source)
            if source in seen_for_record or weight not in {"primary", "supporting"}:
                continue
            seen_for_record.add(source)
            if record.get("t1_close_ret") is None:
                continue
            if record.get("settlement_status") == "provisional":
                continue
            bucket["t3_closed"] += 1
            if str(record.get("outcome") or "").startswith("win"):
                bucket["t3_hits"] += 1
            if record.get("alpha_t1") is not None:
                bucket["_alphas"].append(float(record["alpha_t1"]))

    for bucket in grouped.values():
        closed_count = bucket["t3_closed"]
        bucket["t3_hit_rate"] = (
            round(bucket["t3_hits"] / closed_count * 100, 1)
            if closed_count
            else 0.0
        )
        alphas = bucket.pop("_alphas")
        bucket["avg_excess_return"] = (
            round(sum(alphas) / len(alphas), 2) if alphas else None
        )

    inactive = sorted(
        pipeline for pipeline in known
        if pipeline not in active_recent and pipeline != "unknown"
    )
    return dict(sorted(grouped.items())), inactive


def compute_stats(
    records: List[Dict],
    *,
    asof: Optional[str] = None,
    known_evidence_pipelines: Optional[set[str]] = None,
) -> Dict:
    by_evidence_source, inactive_evidence_pipelines = _evidence_source_stats(
        records,
        asof=asof,
        known_evidence_pipelines=known_evidence_pipelines,
    )
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
                "pending": len(records) - legacy, "legacy_excluded": legacy, "message": msg,
                "by_evidence_source": by_evidence_source,
                "inactive_evidence_pipelines_30d": inactive_evidence_pipelines}

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

    by_strategy = {}
    for strategy_id in sorted({r.get("strategy_id", "default") for r in records}):
        s_closed = [r for r in closed if r.get("strategy_id", "default") == strategy_id]
        s_rets = [r["t1_close_ret"] for r in s_closed]
        s_wins = [r for r in s_closed if r["outcome"].startswith("win")]
        by_strategy[strategy_id] = {
            "total": len([r for r in records if r.get("strategy_id", "default") == strategy_id]),
            "closed": len(s_closed),
            "win_rate": round(len(s_wins) / len(s_closed) * 100, 1) if s_closed else 0,
            "avg_t1_close": round(sum(s_rets) / len(s_rets), 2) if s_rets else 0,
            **_expectancy(s_rets),
        }

    # T+1 provisional is useful for observation, but strategy enable/disable
    # decisions must wait for the final T+3 settlement.
    final_closed = [
        r for r in closed
        if r.get("settlement_status") != "provisional"
    ]
    gating_by_strategy = {}
    for strategy_id in sorted({r.get("strategy_id", "default") for r in records}):
        s_closed = [
            r for r in final_closed
            if r.get("strategy_id", "default") == strategy_id
        ]
        gating_by_strategy[strategy_id] = {
            "closed": len(s_closed),
            **_expectancy([r["t1_close_ret"] for r in s_closed]),
        }
    by_attribution_strategy = _attribution_stats(closed)
    gating_by_attribution_strategy = _attribution_stats(
        final_closed,
        final_only=True,
    )

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
        "by_strategy": by_strategy,
        "gating_by_strategy": gating_by_strategy,
        "by_attribution_strategy": by_attribution_strategy,
        "gating_by_attribution_strategy": gating_by_attribution_strategy,
        "by_evidence_source": by_evidence_source,
        "inactive_evidence_pipelines_30d": inactive_evidence_pipelines,
    }


# ========== 反馈闭环：策略门控（gating，不是 refit）==========

GATING_MIN_SAMPLES = 12  # 至少结算 N 笔才门控，避免小样本误杀


def evaluate_strategy_gating(by_strategy: Dict[str, Dict],
                             min_samples: int = GATING_MIN_SAMPLES) -> List[Dict]:
    """根据 by_strategy 期望值决定门控（纯函数）。

    期望<0 且样本≥min_samples → disable；≥0 且样本≥min_samples → enable；
    样本不足或 default → skip。**只淘汰策略、不回拟合入场规则**（改规则走 research_gate），
    避免用近期实盘噪声过拟合。返回决定列表。
    """
    decisions = []
    for sid, s in (by_strategy or {}).items():
        if sid == "default":
            continue
        closed = s.get("closed", 0)
        exp = s.get("expectancy", 0.0)
        if closed < min_samples:
            action, reason = "skip", "样本不足"
        elif exp < 0:
            action, reason = "disable", f"实盘期望{exp:+.2f}%<0"
        else:
            action, reason = "enable", f"实盘期望{exp:+.2f}%≥0"
        decisions.append({"strategy_id": sid, "action": action,
                          "expectancy": exp, "closed": closed, "reason": reason})
    return decisions


def apply_strategy_gating(decisions: List[Dict]) -> List[Dict]:
    """把门控决定写入 strategy_registry（skip 不写）。返回已应用项。"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'common'))
    import strategy_registry as sr
    applied = []
    for d in decisions:
        if d["action"] == "skip":
            continue
        sr.set_gating(d["strategy_id"], enabled=(d["action"] == "enable"),
                      reason=d["reason"], expectancy=d["expectancy"], samples=d["closed"])
        applied.append(d)
    return applied


def attach_push_report(stats: Dict) -> Dict:
    enriched = dict(stats)
    enriched["push_report"] = build_push_report(read_push_telemetry())
    return enriched


def format_push_report(report: Mapping[str, Any] | None) -> str:
    if not report:
        return ""
    lines = ["## 推送与token计量"]
    jobs = report.get("jobs") if isinstance(report.get("jobs"), Mapping) else {}
    if not jobs:
        lines.append("暂无推送计量数据。")
        return "\n".join(lines)
    lines.append("| 任务 | 日均推送 | 日均字符 | 静默率 | 压缩率 |")
    lines.append("|------|----------|----------|--------|--------|")
    for job_id, row in jobs.items():
        lines.append(
            f"| {job_id} | {row.get('daily_avg_pushes', 0):.3f} | "
            f"{row.get('daily_avg_chars', 0):.3f} | "
            f"{row.get('silent_rate', 0):.3f} | "
            f"{row.get('compression_rate', 0):.3f} |"
        )
    top5 = report.get("char_top5") if isinstance(report.get("char_top5"), list) else []
    if top5:
        summary = ", ".join(
            f"{item.get('job_id')}={item.get('output_chars', 0)}"
            for item in top5
        )
        lines.append(f"字符量Top5: {summary}")
    daily = report.get("daily_total_push_chars")
    if isinstance(daily, Mapping) and daily:
        latest_day = sorted(daily)[-1]
        lines.append(f"最近交易日总推送字符: {latest_day}={daily[latest_day]}")
    return "\n".join(lines)


def format_stats(stats: Dict, records: List[Dict]) -> str:
    lines = ["📈 **打板信号胜率统计（T+1 隔日口径）**",
             f"⏰ {datetime.now().strftime('%Y-%m-%d')}", ""]

    if stats.get("closed", 0) == 0:
        lines.append("尚无已结算信号，继续积累数据...")
        lines.append(f"当前 {stats.get('pending', 0)} 个信号待结算（需至少到 T+1）")
        push_report = format_push_report(stats.get("push_report"))
        if push_report:
            lines.extend(["", push_report])
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

    by_strategy = stats.get("by_strategy", {})
    if by_strategy:
        lines.append("")
        lines.append("| 策略 | 信号 | 结算 | 胜率 | 隔日收益 | 期望 |")
        lines.append("|------|------|------|------|----------|------|")
        for strategy_id, ss in by_strategy.items():
            lines.append(f"| {strategy_id} | {ss['total']} | {ss.get('closed', 0)} | "
                         f"{ss.get('win_rate', 0)}% | {ss.get('avg_t1_close', 0):+.2f}% | "
                         f"{ss.get('expectancy', 0):+.2f}% |")

    by_attribution = stats.get("by_attribution_strategy", {})
    if by_attribution:
        lines.append("")
        lines.append("证据信号为方向归一化共现统计，不代表独立因果贡献。")
        lines.append("| 证据信号 | 样本 | 胜率 | 方向收益 | 期望 |")
        lines.append("|----------|------|------|----------|------|")
        for strategy_id, ss in by_attribution.items():
            lines.append(
                f"| {strategy_id} | {ss.get('closed', 0)} | "
                f"{ss.get('win_rate', 0)}% | "
                f"{ss.get('avg_t1_close', 0):+.2f}% | "
                f"{ss.get('expectancy', 0):+.2f}% |"
            )

    by_evidence_source = stats.get("by_evidence_source", {})
    inactive_pipelines = stats.get("inactive_evidence_pipelines_30d", [])
    if by_evidence_source or inactive_pipelines:
        lines.append("")
        lines.append("## 证据来源归因")
        if by_evidence_source:
            lines.append("| evidence_source | primary推荐 | T+3样本 | T+3命中率 | 平均超额 |")
            lines.append("|-----------------|------------|---------|-----------|----------|")
            for source, ss in by_evidence_source.items():
                avg = ss.get("avg_excess_return")
                avg_str = f"{avg:+.2f}%" if avg is not None else "N/A"
                lines.append(
                    f"| {source} | {ss.get('primary_recommendations', 0)} | "
                    f"{ss.get('t3_closed', 0)} | {ss.get('t3_hit_rate', 0)}% | "
                    f"{avg_str} |"
                )
        if inactive_pipelines:
            lines.append("")
            lines.append(
                "30天未作为 primary/supporting 出现: "
                + "、".join(inactive_pipelines)
            )

    recent = [r for r in records if r.get("outcome") and r["outcome"] != "pending"][-5:]
    if recent:
        lines.append("\n## 最近结算")
        for r in recent:
            emoji = "✅" if r["outcome"].startswith("win") else "❌"
            promo = " 🏆晋级" if r.get("promoted") else ""
            lines.append(f"  {emoji} {r['name']}({r['code']}) {r['grade']}级 → "
                         f"隔日 {r.get('t1_close_ret', 0):+.1f}%{promo}")

    push_report = format_push_report(stats.get("push_report"))
    if push_report:
        lines.extend(["", push_report])
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--record", nargs=4, metavar=("CODE", "NAME", "GRADE", "PRICE"),
                   help="记录信号: code name grade price")
    p.add_argument("--score", type=float, default=5.0, help="评分")
    p.add_argument("--strategy-id", help="可选策略标识，如 daban:first_board_reseal")
    p.add_argument("--gate", action="store_true",
                   help="根据实盘期望值更新策略门控（写 strategy_registry）；建议周报 cron 带上")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    if args.record:
        code, name, grade, price = args.record
        result = record_signal(code, name, grade, args.score, float(price), args.strategy_id)
        print(json.dumps(result, ensure_ascii=False))
    else:
        records = update_outcomes()
        stats = compute_stats(records)
        if args.gate:
            main_decisions = evaluate_strategy_gating(
                stats.get("gating_by_strategy", {})
            )
            evidence_decisions = evaluate_strategy_gating(
                stats.get("gating_by_attribution_strategy", {})
            )
            stats["gating_applied"] = apply_strategy_gating(
                main_decisions + evidence_decisions
            )
        stats = attach_push_report(stats)
        if args.json:
            print(json.dumps(stats, ensure_ascii=False, indent=2))
        else:
            print(format_stats(stats, records))
            for d in stats.get("gating_applied", []):
                print(f"⚖️ 门控 {d['strategy_id']}: {d['action']} ({d['reason']})")
