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

成本口径：策略门控（enable/disable）消费**税后**期望——税前口径会系统性高估
短线策略，A 股双边成本约 11-16bps，打板隔日兑现的边际信号正好落在这层里。
税前指标同时保留上报（expectancy / win_rate），税后见 *_net 与 cost_model。

关键修复（相对旧版）:
  · 取消「首次穿越 +3% 即永久锁定 win」的结构性向上偏置
  · 结算与信号价均取自**前复权 K 线**，规避送转除权导致的收益失真
  · 阈值对称（±5% / 0）

数据源（共享 data-access 层，cron-safe）：收盘后 BaoStock 前复权日线缓存，
个股与沪深300 基准都只读 ``market/history.sqlite3``；缓存缺失时保持 pending。

Usage:
  python3 performance_tracker.py                          # 查看统计
  python3 performance_tracker.py --record CODE NAME GRADE PRICE
  python3 performance_tracker.py --record CODE NAME GRADE PRICE --strategy-id daban:first_board_reseal
  python3 performance_tracker.py --json
"""

import json
import math
import os
import sqlite3
import sys
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Any, Mapping

import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.insert(0, ROOT)
from state_store import read_json, atomic_write_json, update_json_list, mutate_json
from paths import data_file
from tradeability import limit_pct, round_limit
import local_market_history
from execution_model import FEE_SCHEDULE, net_return_pct
import signal_ledger
from scripts.cron_budget_report import build_push_report, read_push_telemetry

HISTORY_FILE = data_file("stock-triage", "signal_history.json")
LEDGER_FILE = signal_ledger.LEDGER_FILE

# 沪深300 基准（BaoStock 指数代码 sh.000300，缓存键仍为六位代码）
BENCH_MARKET = "sh"
BENCH_CODE = "000300"

HOLD_DAYS = 3  # 打板最长观察窗（隔日为主，最多看到 T+3）
AGED_PENDING_DAYS = 3
TERMINAL_UNRESOLVED_DAYS = 7
MIN_SETTLEMENT_COVERAGE = 0.95
MAX_TERMINAL_AMBIGUITY_RATIO = 0.02
# 信号不记手数，成本率必须挂在一个显式的名义本金上（最低佣金 5 元使成本率随
# 本金变化）。取模拟盘配置口径：initial_cash 100000 / max_positions 5 = 20000
# （config/paper_trading.json）。该假设随统计结果一起上报，读者可按自身仓位重算。
SETTLEMENT_NOTIONAL = 20000.0
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
    limit_reference_close: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """
    根据事前可观察的入场参考价与后续前复权 K 线结算一个打板信号。
    signal_close 保留旧参数名以兼容调用方，但语义是信号发生时已记录的入场参考价。
    limit_reference_close 是交易所计算 T+1 涨停价使用的信号日收盘；若未提供，
    为兼容旧调用而回退到 signal_close。
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
    limit_base = limit_reference_close or signal_close
    t1_limit_up = round_limit(limit_base, limit_pct_val, up=True)
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
        # 结算所对应的 T+1 日期。此前只用了这根 K 线的价格就把日期丢掉，导致
        # 下游无法在不重算交易日历的前提下知道结果归属哪一天。
        "settled_on": str(t1.get("date") or "") or None,
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
    """记录一个新信号。price 必须是信号发生时已观察到的入场参考价。

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
    """Read signal-day close plus future qfq bars from the local cache only."""
    try:
        cached = local_market_history.get_daily_bars(
            [str(code).zfill(6)], date.today().isoformat(), 120, adjust_flag="qfq"
        )
    except (OSError, sqlite3.Error, ValueError):
        return None
    klines = []
    for row in cached:
        normalized = dict(row)
        normalized["date"] = str(row.get("trading_date") or row.get("date") or "")
        klines.append(normalized)
    if not klines:
        return None
    idx = next((i for i, k in enumerate(klines) if k["date"] == signal_date), None)
    if idx is None:
        return None
    return {"signal_close": klines[idx]["close"], "future": klines[idx + 1:]}


def _observable_entry_price(record: Mapping[str, Any]) -> Optional[tuple[float, str]]:
    """Return a positive, finite price captured when the signal was emitted.

    Canonical ``signal.opened`` projections use ``signal_price``. The other
    names keep legacy/recommendation records settleable without consulting a
    later market close. Missing or invalid values fail closed.
    """
    for key in (
        "signal_price",
        "entry_price",
        "reference_price",
        "recommendation_price",
    ):
        raw = record.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0 and math.isfinite(value):
            return value, key
    return None


def _signal_age_days(record: Mapping[str, Any], *, asof: date | None = None) -> int | None:
    try:
        signal_day = date.fromisoformat(str(record.get("signal_date") or record.get("date"))[:10])
    except ValueError:
        return None
    return max(0, ((asof or date.today()) - signal_day).days)


def _unresolved_settlement(reason: str, age_days: int | None) -> Dict[str, Any]:
    return {
        "settlement_status": "terminal_unresolved",
        "settlement_observation_status": reason,
        "settlement_age_days": age_days,
        "resolved": False,
        "outcome": "unresolved",
    }


def update_outcomes() -> List[Dict]:
    """重新结算所有 pending 信号（已结算的不再改动）。

    并发安全：网络抓取/结算在锁外完成，仅把结果按 (code, signal_date) 收集；
    最终用 mutate_json 在单锁内重新读取最新历史并就地结算回写。这样既不长时间
    持锁等网络，也不会因"读快照→结算→写回"覆盖掉期间并发追加/结算的记录。
    """
    snapshot = load_history()
    bench_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    resolutions: Dict[str, Dict[str, Any]] = {}
    observations: Dict[str, Dict[str, Any]] = {}
    records_by_id: Dict[str, Dict[str, Any]] = {}

    for r in snapshot:
        if r.get("settlement_status") == "final":
            continue

        entry = _observable_entry_price(r)
        if entry is None:
            signal_id = str(signal_ledger.legacy_signal_links(r)["signal_id"])
            resolutions[signal_id] = _unresolved_settlement(
                "entry_price_missing", _signal_age_days(r)
            )
            records_by_id[signal_id] = r
            continue  # 无事前可观察价格，禁止回退到信号日收盘
        entry_price, entry_price_source = entry

        code, sdate = r["code"], r["signal_date"]
        market = "sh" if code.startswith("6") else "sz"

        stock = _fetch_future_bars(code, sdate, market)
        if not stock or not stock["future"]:
            signal_id = str(signal_ledger.legacy_signal_links(r)["signal_id"])
            age_days = _signal_age_days(r)
            if age_days is not None and age_days >= TERMINAL_UNRESOLVED_DAYS:
                resolutions[signal_id] = _unresolved_settlement(
                    "market_data_unavailable_or_tradeability_unknown", age_days
                )
                records_by_id[signal_id] = r
            elif age_days is not None and age_days >= AGED_PENDING_DAYS:
                observations[signal_id] = {
                    "settlement_observation_status": "aged_pending",
                    "settlement_age_days": age_days,
                }
            continue  # 未到 T+1 或短时数据缺口，保持 pending

        if sdate not in bench_cache:
            bench_cache[sdate] = _fetch_future_bars(BENCH_CODE, sdate, BENCH_MARKET)
        bench = bench_cache[sdate]
        if not bench or not bench["future"]:
            signal_id = str(signal_ledger.legacy_signal_links(r)["signal_id"])
            age_days = _signal_age_days(r)
            if age_days is not None and age_days >= TERMINAL_UNRESOLVED_DAYS:
                resolutions[signal_id] = _unresolved_settlement(
                    "benchmark_data_unavailable", age_days
                )
                records_by_id[signal_id] = r
            elif age_days is not None and age_days >= AGED_PENDING_DAYS:
                observations[signal_id] = {
                    "settlement_observation_status": "aged_pending_benchmark",
                    "settlement_age_days": age_days,
                }
            continue

        result = evaluate_signal(
            signal_close=entry_price,
            future_bars=stock["future"],
            limit_pct_val=limit_pct(code, r.get("name", "")),
            index_signal_close=bench["signal_close"],
            index_future_bars=bench["future"],
            limit_reference_close=stock["signal_close"],
        )
        if result:
            result.update({
                "settlement_entry_price": entry_price,
                "settlement_entry_price_source": entry_price_source,
            })
            signal_id = signal_ledger.legacy_signal_links(r)["signal_id"]
            resolutions[str(signal_id)] = result
            records_by_id[str(signal_id)] = r

    if not resolutions and not observations:
        return snapshot

    ledger_events = []
    for signal_id, resolution in resolutions.items():
        record = records_by_id[signal_id]
        links = signal_ledger.legacy_signal_links(record)
        status = resolution.get("settlement_status")
        stage = "t3" if status == "final" else "t1" if status == "provisional" else None
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
            elif signal_id in observations:
                r.update(observations[signal_id])
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


def _net_return(record: Mapping[str, Any], gross: float) -> Optional[float]:
    """把税前收益换成税后。日期缺失或早于费率表生效日 → None（不猜，不静默回退）。"""
    record_day = _record_date(dict(record))
    if record_day is None:
        return None
    try:
        priced = net_return_pct(
            gross_return_pct=gross,
            notional=SETTLEMENT_NOTIONAL,
            asof=record_day.isoformat(),
        )
    except ValueError:
        return None
    return float(priced["net_return_pct"])


def _net_returns(records: List[Dict]) -> List[float]:
    """已结算记录的税后 T+1 收益序列；不可定价的记录被剔除而非补零。"""
    values = [
        _net_return(record, float(record["t1_close_ret"]))
        for record in records
        if record.get("t1_close_ret") is not None
    ]
    return [value for value in values if value is not None]


def _cost_model(records: List[Dict]) -> Dict[str, Any]:
    """成本口径元数据。priced 为 0 时下游必须按样本不足处理，不得当作零成本。"""
    priceable = _net_returns(records)
    settled = [r for r in records if r.get("t1_close_ret") is not None]
    return {
        "basis": "net_of_estimated_cost",
        "assumed_notional": SETTLEMENT_NOTIONAL,
        "fee_schedule_version": FEE_SCHEDULE["version"],
        "priced": len(priceable),
        "unpriceable": max(0, len(settled) - len(priceable)),
        "authoritative_source": "broker_statement",
    }


def _net_metrics(records: List[Dict]) -> Dict[str, Any]:
    """税后指标；样本为空一律返回 None，避免空集被读成 0.0 的假绿。"""
    net_rets = _net_returns(records)
    if not net_rets:
        return {
            "closed_net": 0,
            "win_rate_net": None,
            "avg_t1_close_ret_net": None,
            "expectancy_net": None,
        }
    return {
        "closed_net": len(net_rets),
        "win_rate_net": round(
            sum(1 for value in net_rets if value >= 0) / len(net_rets) * 100, 1
        ),
        "avg_t1_close_ret_net": round(sum(net_rets) / len(net_rets), 2),
        "expectancy_net": _expectancy(net_rets)["expectancy"],
    }


def _attribution_returns(
    records: List[Dict],
    *,
    final_only: bool = False,
    net_of_cost: bool = False,
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
            if net_of_cost:
                priced = _net_return(record, value)
                if priced is None:
                    continue
                value = priced
            grouped.setdefault(strategy_id, []).append(value)
    return grouped


def _attribution_stats(
    records: List[Dict],
    *,
    final_only: bool = False,
    net_of_cost: bool = False,
) -> Dict[str, Dict[str, float]]:
    grouped = _attribution_returns(
        records, final_only=final_only, net_of_cost=net_of_cost
    )
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
        run = job.get("run") or {}
        command = " ".join(
            str(item) for item in (run.get("argv") or job.get("command_argv") or [])
        ) or str(run.get("command") or job.get("command") or "")
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
    legacy = sum(
        1 for r in records
        if r.get("outcome") not in (None, "pending")
        and r.get("t1_close_ret") is None
        and r.get("settlement_status") != "terminal_unresolved"
    )
    terminal_unresolved = [
        r for r in records if r.get("settlement_status") == "terminal_unresolved"
    ]
    eligible = max(0, len(records) - legacy)
    terminal_count = len(closed) + len(terminal_unresolved)
    coverage_ratio = terminal_count / eligible if eligible else 1.0
    resolved_ratio = len(closed) / eligible if eligible else 1.0
    ambiguity_ratio = len(terminal_unresolved) / eligible if eligible else 0.0
    gating_reason = (
        "coverage_insufficient"
        if coverage_ratio < MIN_SETTLEMENT_COVERAGE
        else "terminal_ambiguity"
        if ambiguity_ratio > MAX_TERMINAL_AMBIGUITY_RATIO
        else None
    )
    settlement_coverage = {
        "eligible": eligible,
        "terminal": terminal_count,
        "terminal_unresolved": len(terminal_unresolved),
        "aged_pending": sum(
            r.get("settlement_observation_status") == "aged_pending" for r in records
        ),
        "ratio": round(coverage_ratio, 4),
        "resolved_ratio": round(resolved_ratio, 4),
        "terminal_ambiguity_ratio": round(ambiguity_ratio, 4),
        "maximum_terminal_ambiguity_ratio": MAX_TERMINAL_AMBIGUITY_RATIO,
        "minimum": MIN_SETTLEMENT_COVERAGE,
        "status": "sufficient" if coverage_ratio >= MIN_SETTLEMENT_COVERAGE else "coverage_insufficient",
        "gating_status": "sufficient" if gating_reason is None else "blocked",
        "gating_reason": gating_reason,
    }
    if not closed:
        msg = "尚无已结算信号（需至少到 T+1）"
        if legacy:
            msg += f"；另有 {legacy} 条旧口径记录已排除（建议重置 signal_history.json）"
        return {"total_signals": len(records), "closed": 0,
                "pending": eligible - len(terminal_unresolved), "legacy_excluded": legacy, "message": msg,
                "settlement_coverage": settlement_coverage,
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
            **_net_metrics(s_closed),
        }

    # T+1 provisional is useful for observation, but strategy enable/disable
    # decisions must wait for the final T+3 settlement.
    final_closed = [
        r for r in closed
        if r.get("settlement_status") != "provisional"
    ]
    # 门控口径为**税后**：税前期望会系统性高估短线策略（A 股双边成本约 11-16bps，
    # 打板隔日兑现的边际信号正是被这层成本吃掉的那批）。closed 同步取税后可定价
    # 样本数，使不可定价记录不会把样本量凑够而绕过 GATING_MIN_SAMPLES。
    gating_by_strategy = {}
    for strategy_id in sorted({r.get("strategy_id", "default") for r in records}):
        s_closed = [
            r for r in final_closed
            if r.get("strategy_id", "default") == strategy_id
        ]
        net_rets = _net_returns(s_closed)
        gating_by_strategy[strategy_id] = {
            "closed": len(net_rets),
            "closed_gross": len(s_closed),
            "cost_basis": "net_of_estimated_cost",
            **_expectancy(net_rets),
            "expectancy_gross": _expectancy(
                [r["t1_close_ret"] for r in s_closed]
            )["expectancy"],
        }
    by_attribution_strategy = _attribution_stats(closed)
    gating_by_attribution_strategy = _attribution_stats(
        final_closed,
        final_only=True,
        net_of_cost=True,
    )

    return {
        "metric": "打板口径(T+1隔日)",
        "total_signals": len(records),
        "closed": len(closed),
        "pending": eligible - terminal_count,
        "legacy_excluded": legacy,
        "settlement_coverage": settlement_coverage,
        "win_rate": round(len(wins) / len(closed) * 100, 1),
        "promote_rate": round(len(promoted) / len(closed) * 100, 1),
        "avg_t1_open_premium": round(
            sum(r["t1_open_premium"] for r in closed
                if r.get("t1_open_premium") is not None) / len(closed), 2),
        "avg_t1_close_ret": round(sum(rets) / len(rets), 2) if rets else 0,
        "avg_alpha_t1": round(sum(alphas) / len(alphas), 2) if alphas else None,
        **_expectancy(rets),
        **_net_metrics(closed),
        "cost_model": _cost_model(closed),
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
                             min_samples: int = GATING_MIN_SAMPLES,
                             *, coverage_sufficient: bool = True,
                             coverage_reason: str = "coverage_insufficient") -> List[Dict]:
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
        if not coverage_sufficient:
            action, reason = "skip", coverage_reason
        elif closed < min_samples:
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
        coverage = stats.get("settlement_coverage") or {}
        lines.append(
            f"结算覆盖率: {float(coverage.get('ratio') or 0):.1%} "
            f"({coverage.get('status') or 'unknown'})"
        )
        push_report = format_push_report(stats.get("push_report"))
        if push_report:
            lines.extend(["", push_report])
        return "\n".join(lines)

    alpha = stats.get("avg_alpha_t1")
    alpha_str = f"{alpha:+.2f}%" if alpha is not None else "N/A"
    lines.append(f"📊 总信号: {stats['total_signals']} | 已结算: {stats['closed']} | "
                 f"待结算: {stats['pending']}")
    coverage = stats.get("settlement_coverage") or {}
    lines.append(
        f"🧾 结算覆盖率: {float(coverage.get('ratio') or 0):.1%} | "
        f"未决终态: {coverage.get('terminal_unresolved', 0)} | "
        f"状态: {coverage.get('status') or 'unknown'}"
    )
    lines.append(f"🎯 胜率: **{stats['win_rate']}%** | 连板晋级率: **{stats['promote_rate']}%**")
    lines.append(f"💰 隔日溢价(均): {stats['avg_t1_open_premium']:+.2f}% | "
                 f"隔日收益(均): {stats['avg_t1_close_ret']:+.2f}%")
    lines.append(f"📐 期望值: **{stats['expectancy']:+.2f}%** | 盈亏比: {stats['payoff_ratio']} | "
                 f"超额(α vs 沪深300): **{alpha_str}**")
    net_expectancy = stats.get("expectancy_net")
    cost_model = stats.get("cost_model") or {}
    if net_expectancy is None:
        lines.append("🧮 税后期望: 样本不可定价（成本口径无法计算）")
    else:
        lines.append(
            f"🧮 税后期望(门控口径): **{net_expectancy:+.2f}%** | "
            f"税后胜率: {stats.get('win_rate_net')}% | "
            f"名义本金假设: {cost_model.get('assumed_notional')}"
        )
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
                stats.get("gating_by_strategy", {}),
                coverage_sufficient=(stats.get("settlement_coverage") or {}).get("gating_status") == "sufficient",
                coverage_reason=(stats.get("settlement_coverage") or {}).get("gating_reason") or "coverage_insufficient",
            )
            evidence_decisions = evaluate_strategy_gating(
                stats.get("gating_by_attribution_strategy", {}),
                coverage_sufficient=(stats.get("settlement_coverage") or {}).get("gating_status") == "sufficient",
                coverage_reason=(stats.get("settlement_coverage") or {}).get("gating_reason") or "coverage_insufficient",
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
