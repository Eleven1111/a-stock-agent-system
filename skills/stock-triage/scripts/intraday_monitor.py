#!/usr/bin/env python3
"""
盘中高频异动监控 — 5分钟阈值触发
=================================
监测：放量突破 / 北向异动 / 涨跌停板 / 板块异动
只在触发阈值时输出，无触发完全静默。

Usage:
  python3 intraday_monitor.py
  python3 intraday_monitor.py --json
"""

import json
import sys
from datetime import datetime, time as dtime
from typing import Any, Dict, Mapping

import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path
from paths import data_file
from state_store import atomic_write_json, read_json
from data_access_config import intraday_settings, risk_settings
from data_provider import fetch_tencent_quote, fetch_tencent_quotes
from http_client import DataSourceError
from exit_signals import evaluate_all_exit_signals
from a_share_rules import CalendarCoverageError, t1_constraint
from signal_context import read_signal_context
from catalyst_context import read_catalyst_events
import monitor_registry
import runtime_targets

# 兼容测试/显式注入；生产默认观察集由持仓和 monitor_registry 动态生成。
TRACKED_CODES = []
TRACKED_NAMES = {}

ALERT_CACHE = data_file("stock-triage", "intraday_alerts.json")
PORTFOLIO_FILE = data_file("stock-triage", "portfolio.json")
SHORTLIST_FILE = data_file("daban-stock-picker", "auction_shortlist_latest.json")

_MONITOR_CONFIG = intraday_settings()
LIMIT_MOVE_PCT = float(_MONITOR_CONFIG["limit_move_pct"])
HIGH_TURNOVER_PCT = float(_MONITOR_CONFIG["high_turnover_pct"])
SURGE_PCT = float(_MONITOR_CONFIG["surge_pct"])
DIRECTIONAL_MOVE_PCT = float(_MONITOR_CONFIG["directional_move_pct"])
INTRADAY_QUOTE_BATCH_SIZE = int(_MONITOR_CONFIG["quote_batch_size"])
SECTOR_MIN_MEMBERS = int(_MONITOR_CONFIG["sector_min_members"])
SECTOR_MIN_POSITIVE_RATIO = float(_MONITOR_CONFIG["sector_min_positive_ratio"])
SECTOR_MIN_AVERAGE_PCT = float(_MONITOR_CONFIG["sector_min_average_pct"])
SECTOR_MIN_ACCELERATION_PCT = float(_MONITOR_CONFIG["sector_min_acceleration_pct"])
STOP_LOSS_PCT = float(risk_settings()["stop_loss_pct"])


def in_trading_session(now: datetime = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    current = now.time()
    return (
        dtime(9, 30) <= current <= dtime(11, 30)
        or dtime(13, 0) <= current <= dtime(15, 0)
    )


def fetch_realtime(code: str) -> Dict:
    try:
        quote = fetch_tencent_quote(code)
        return {
            key: quote.get(key)
            for key in (
                "price",
                "change_pct",
                "high",
                "low",
                "volume",
                "amount",
                "turnover",
                "prev_close",
                "fetched_at",
            )
        }
    except DataSourceError:
        return {}


def fetch_realtime_many(codes: list[str]) -> Dict[str, Dict[str, Any]]:
    """Fetch the intraday universe in one Tencent request."""
    unique = list(dict.fromkeys(
        runtime_targets.normalize_stock_code(code)
        for code in codes
        if runtime_targets.normalize_stock_code(code)
    ))
    if not unique:
        return {}
    output: Dict[str, Dict[str, Any]] = {}
    for index in range(0, len(unique), INTRADAY_QUOTE_BATCH_SIZE):
        batch = unique[index:index + INTRADAY_QUOTE_BATCH_SIZE]
        try:
            result = fetch_tencent_quotes(batch)
        except DataSourceError:
            continue
        for raw_code, quote in result.data.items():
            code = runtime_targets.normalize_stock_code(raw_code)
            if not code:
                continue
            output[code] = {
                key: quote.get(key)
                for key in (
                    "price",
                    "change_pct",
                    "high",
                    "low",
                    "volume",
                    "amount",
                    "turnover",
                    "prev_close",
                    "fetched_at",
                )
            }
    return output


def load_alert_cache() -> Dict:
    return read_json(ALERT_CACHE, {})


def save_alert_cache(cache: Dict):
    atomic_write_json(ALERT_CACHE, cache)


def _apply_t1_exit_guard(signal: Dict, position: Dict, now: datetime) -> Dict:
    guarded = dict(signal)
    if guarded.get("action") not in {"sell", "reduce"}:
        return guarded
    entry_date = (
        position.get("entry_date")
        or position.get("acquired_on")
        or position.get("buy_date")
    )
    if not entry_date:
        guarded["original_action"] = guarded.get("action")
        guarded["action"] = "hold_locked"
        guarded["severity"] = "warning"
        guarded["reason"] = f"{guarded.get('reason', '')}；A股T+1锁定状态未知，禁止盘中卖出建议"
        return guarded
    try:
        constraint = t1_constraint(entry_date, now.date())
    except (CalendarCoverageError, ValueError):
        guarded["original_action"] = guarded.get("action")
        guarded["action"] = "hold_locked"
        guarded["severity"] = "warning"
        guarded["reason"] = f"{guarded.get('reason', '')}；A股交易日历不足，退出建议失败关闭"
        return guarded
    if constraint.get("sell_allowed") is False:
        guarded["original_action"] = guarded.get("action")
        guarded["action"] = "hold_locked"
        guarded["severity"] = "warning"
        guarded["t1_constraint"] = constraint
        guarded["reason"] = (
            f"{guarded.get('reason', '')}；A股T+1锁定，"
            f"最早{constraint.get('earliest_sell_date')}可卖"
        )
    return guarded


def tracked_universe() -> Dict[str, str]:
    portfolio = read_json(PORTFOLIO_FILE, {})
    positions = portfolio.get("positions", []) if isinstance(portfolio, dict) else []
    monitor_registry.sync_positions(positions)
    registry = monitor_registry.load_registry()
    tracked = {
        target["code"]: target["name"]
        for target in runtime_targets.build_stock_targets(
            portfolio=portfolio,
            registry=registry,
        )
    }
    cancelled = runtime_targets.cancelled_stock_codes(registry)
    for raw_code in TRACKED_CODES:
        code = runtime_targets.normalize_stock_code(raw_code)
        if code and code not in cancelled:
            tracked[code] = TRACKED_NAMES.get(str(raw_code), code)
    return tracked


def _same_day_shortlist(asof: str) -> Dict[str, Any]:
    payload = read_json(SHORTLIST_FILE, {})
    if not isinstance(payload, dict) or str(payload.get("asof") or "")[:10] != asof:
        return {}
    return payload


def sector_watchlist_degradation(asof: str) -> str:
    """竞价短名单降级原因；未降级返回空串。

    降级时刻意不猜测板块成员（回退前一日短名单会用过期池子产生假信号，
    改用持仓板块则改变了监控语义）。板块告警确实停发，但必须显式告知——
    否则运维看不出盘中板块监控今天根本没在工作（issue #115）。
    """
    payload = _same_day_shortlist(asof)
    if str(payload.get("status")) != "degraded":
        return ""
    reasons = "；".join(str(item) for item in payload.get("degraded_reasons") or [])
    collection = payload.get("collection_status") or "unknown"
    return f"竞价短名单降级（collection_status={collection}）：{reasons or '原因未记录'}"


def _sector_degradation_alerts(
    reason: str,
    cache: Dict[str, Any],
    now_str: str,
) -> list[Dict[str, str]]:
    """降级提示；每日只发一次（盘中每几分钟一 tick，不能刷屏）。"""
    if not reason or "sector_degraded" in cache:
        return []
    cache["sector_degraded"] = now_str
    return [{
        "level": "⚠️",
        "type": "板块监控降级",
        "msg": f"板块加速告警今日停用｜{reason}",
    }]


def load_sector_watchlist(asof: str) -> Dict[str, Dict[str, str]]:
    """issue #260 §4.D.1：同时读取同日 shortlist 与局部主题观察/条件候选。

    只用 artifact 里已固化的成员（同一份同日快照），不猜测、不回退历史日。
    手工取消的 tombstone codes 一律排除——local_theme 路径拉入更宽的成员集合，
    不能因此让已被人工停用的自动监控重新复活。
    """
    payload = _same_day_shortlist(asof)
    cancelled = runtime_targets.cancelled_stock_codes(monitor_registry.load_registry())
    members: Dict[str, Dict[str, str]] = {}
    for source, key in (
        ("execution", "shortlist"),
        ("local_theme", "local_theme_candidates"),
        ("local_theme", "conditional_candidates"),
    ):
        for item in payload.get(key) or []:
            code = runtime_targets.normalize_stock_code(item.get("code"))
            sector = str(item.get("sector") or "").strip()
            if not code or not sector or code in cancelled:
                continue
            existing = members.get(code)
            if existing and existing.get("source") == "execution":
                continue  # execution 成员身份优先，不被 local_theme 覆盖
            members[code] = {
                "name": str(item.get("name") or code),
                "sector": sector,
                "source": source,
            }
    return members


def detect_sector_acceleration(
    quotes: Mapping[str, Mapping[str, Any]],
    members: Mapping[str, Mapping[str, str]],
    *,
    previous: Mapping[str, Mapping[str, Any]],
    min_members: int,
    min_positive_ratio: float,
    min_average_pct: float,
    min_acceleration_pct: float,
) -> tuple[list[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Detect broad sector momentum without converting it into a buy signal.

    issue #260 §4.D.2：成员可能来自 execution shortlist 或 local_theme 观察/
    条件候选(见 load_sector_watchlist)。一个板块的成员若全部来自 local_theme
    路径，输出 participation_scope=local_theme_only 供简报/审计区分；只要还有
    execution 成员，就仍是旧的全局观察语义。无论哪种来源，action 恒为
    watch——盘中告警本身永不升级为买入信号。
    """
    grouped: Dict[str, list[tuple[str, str, float, str]]] = {}
    for code, metadata in members.items():
        sector = str(metadata.get("sector") or "").strip()
        quote = quotes.get(code) or {}
        if not sector or quote.get("change_pct") is None:
            continue
        grouped.setdefault(sector, []).append((
            code,
            str(metadata.get("name") or code),
            float(quote.get("change_pct") or 0.0),
            str(metadata.get("source") or "execution"),
        ))

    alerts: list[Dict[str, Any]] = []
    state: Dict[str, Dict[str, Any]] = {}
    for sector, rows in grouped.items():
        average = sum(row[2] for row in rows) / len(rows)
        positive = sum(row[2] > 0 for row in rows)
        positive_ratio = positive / len(rows)
        participation_scope = (
            "local_theme_only"
            if all(row[3] == "local_theme" for row in rows)
            else None
        )
        prior = previous.get(sector) or {}
        prior_average = float(prior.get("average_pct") or 0.0)
        qualifies = (
            len(rows) >= min_members
            and positive_ratio >= min_positive_ratio
            and average >= min_average_pct
        )
        should_alert = qualifies and (
            not prior.get("alerted")
            or average - prior_average >= min_acceleration_pct
        )
        state[sector] = {
            "average_pct": round(average, 2),
            "positive_ratio": round(positive_ratio, 4),
            "member_count": len(rows),
            "alerted": qualifies,
            "participation_scope": participation_scope,
        }
        if not should_alert:
            continue
        leaders = sorted(rows, key=lambda row: (-row[2], row[0]))[:3]
        leader_text = "、".join(f"{name}{pct:+.1f}%" for _, name, pct, _source in leaders)
        alerts.append({
            "level": "🟡",
            "type": "板块加速",
            "sector": sector,
            "action": "watch",
            "participation_scope": participation_scope,
            "msg": (
                f"{sector}候选集体走强：{positive}/{len(rows)}上涨，"
                f"均涨{average:.1f}%；领先 {leader_text}。仅升级关注，不构成买入信号"
                + ("（局部主题观察，非执行池）" if participation_scope == "local_theme_only" else "")
            ),
        })
    return alerts, state


def check_intraday() -> Dict:
    """检测盘中异动（阈值触发）"""
    alerts = []
    cache = load_alert_cache()
    now = datetime.now()
    now_str = now.strftime("%H:%M")
    today = now.strftime("%Y%m%d")
    if cache.get("_date", "") != today:
        cache = {"_date": today}

    universe = tracked_universe()
    today_iso = now.date().isoformat()
    sector_members = load_sector_watchlist(today_iso)
    sector_degradation = sector_watchlist_degradation(today_iso)
    alerts.extend(_sector_degradation_alerts(sector_degradation, cache, now_str))
    quote_by_code = fetch_realtime_many(list(universe) + list(sector_members))
    for code, name in universe.items():
        data = quote_by_code.get(code) or {}
        if not data.get("price"):
            continue

        price = data["price"]
        pct = data.get("change_pct", 0)
        turnover = data.get("turnover", 0)

        # 1. 涨跌停检测
        if pct and pct >= LIMIT_MOVE_PCT:
            key = f"zt_{code}"
            if key not in cache:
                alerts.append({"level": "🔴", "type": "涨停",
                               "msg": f"{name}({code}) 涨停！现价{price} (+{pct}%)"})
                cache[key] = now_str

        elif pct and pct <= -LIMIT_MOVE_PCT:
            key = f"dt_{code}"
            if key not in cache:
                alerts.append({"level": "🔴", "type": "跌停",
                               "msg": f"{name}({code}) 跌停！现价{price} ({pct}%)"})
                cache[key] = now_str

        # 2. 放量检测（换手率>10%且之前未报）
        if turnover > HIGH_TURNOVER_PCT:
            key = f"vol_{code}"
            if key not in cache:
                direction = (
                    "拉升"
                    if pct > DIRECTIONAL_MOVE_PCT
                    else ("砸盘" if pct < -DIRECTIONAL_MOVE_PCT else "异动")
                )
                alerts.append({"level": "🟡", "type": "放量",
                               "msg": f"{name} 换手{turnover:.1f}%{direction}，成交{data.get('amount',0)/1e8:.1f}亿"})
                cache[key] = now_str

        # 3. 急涨急跌（5%以上）
        if abs(pct) >= SURGE_PCT:
            key = f"surge_{code}"
            if key not in cache:
                direction = "急涨" if pct > 0 else "急跌"
                alerts.append({"level": "🟡", "type": direction,
                               "msg": f"{name} {direction}{abs(pct):.1f}%，现价{price}"})
                cache[key] = now_str

    sector_alerts, sector_state = detect_sector_acceleration(
        quote_by_code,
        sector_members,
        previous=cache.get("_sector_state") or {},
        min_members=SECTOR_MIN_MEMBERS,
        min_positive_ratio=SECTOR_MIN_POSITIVE_RATIO,
        min_average_pct=SECTOR_MIN_AVERAGE_PCT,
        min_acceleration_pct=SECTOR_MIN_ACCELERATION_PCT,
    )
    alerts.extend(sector_alerts)
    cache["_sector_state"] = sector_state

    # 持仓退出信号检测
    portfolio = read_json(PORTFOLIO_FILE, {})
    positions = portfolio.get("positions", []) if isinstance(portfolio, dict) else []
    signal_ctx = read_signal_context()
    exit_alerts = []
    for pos in positions:
        pos_code = str(pos.get("code", ""))
        if not pos_code:
            continue
        normalized_pos_code = runtime_targets.normalize_stock_code(pos_code)
        pos_data = quote_by_code.get(normalized_pos_code) or {}
        if not pos_data.get("price"):
            continue
        pos_price = pos_data["price"]
        # portfolio_manager 落盘字段是 cost/buy_date；entry_price/entry_date 仅
        # 兼容旧测试注入。此前缺 cost/buy_date 回退，真实持仓的止损/时间止损
        # 在盘中从不触发（issue #88 翔鹭钨业止损未闭环的坑之一）。
        entry_price = float(
            pos.get("entry_price") or pos.get("avg_cost") or pos.get("cost") or 0
        )
        pnl_pct = ((pos_price / entry_price - 1) * 100) if entry_price > 0 else None
        peak = float(pos.get("peak_price") or pos.get("high_since_entry") or pos_price)
        if pos_price > peak:
            peak = pos_price

        stop_price = float(pos.get("stop_price") or 0)
        if stop_price <= 0 and entry_price > 0:
            stop_price = round(entry_price * (1 + STOP_LOSS_PCT / 100), 2)

        nb_yi = signal_ctx.get("northbound_net_yi") if signal_ctx else None
        stock_flow = (signal_ctx.get("stock_flows") or {}).get(pos_code) if signal_ctx else None
        stock_main = stock_flow.get("main_net_yi") if isinstance(stock_flow, dict) else None
        lhb_profile = (signal_ctx.get("lhb_profiles") or {}).get(
            runtime_targets.normalize_stock_code(pos_code) or pos_code
        ) if signal_ctx else None
        deep_score = None
        try:
            from deep_research_cache import read_deep_research
            deep_record = read_deep_research(pos_code)
            if deep_record:
                deep_score = deep_record.get("deep_score")
        except Exception:  # noqa: BLE001 — 深研缓存缺失不阻塞盘中监控
            pass

        exit_result = evaluate_all_exit_signals(
            current_price=pos_price,
            stop_price=stop_price,
            target_price=float(pos.get("target_price") or 0),
            target_price_2=float(pos.get("target_price_2") or 0) or None,
            peak_price=peak,
            trailing_pct=5.0,
            entry_date=pos.get("entry_date") or pos.get("buy_date"),
            horizon_days=int(pos.get("horizon_days") or 3),
            current_pnl_pct=pnl_pct,
            temperature_tier=signal_ctx.get("temperature_tier") if signal_ctx else None,
            northbound_net_yi=nb_yi,
            stock_main_net_yi=stock_main,
            catalyst_events=read_catalyst_events(pos_code),
            lhb_profile=lhb_profile if isinstance(lhb_profile, dict) else None,
            deep_score=deep_score,
        )
        if exit_result["triggered_count"] > 0:
            top = _apply_t1_exit_guard(exit_result["top_signal"], pos, now)
            key = f"exit_{pos_code}_{top['signal_type']}"
            # critical 且可执行的退出信号每小时重报，直到执行——
            # 止损建议只发一次然后沉默，是 -5% 拖成 -25% 的直接原因。
            if top.get("severity") == "critical" and top.get("action") in {"sell", "reduce"}:
                key = f"{key}_{now.strftime('%H')}"
            if key not in cache:
                severity_icon = {"critical": "🔴", "warning": "🟡"}.get(top.get("severity", ""), "⚪")
                exit_alerts.append({
                    "level": severity_icon,
                    "type": f"退出信号({top['signal_type']})",
                    "msg": f"{pos.get('name', pos_code)}({pos_code}) {top.get('reason', '')} → 建议{top['action']}",
                    "action": top["action"],
                    "signal": top,
                })
                cache[key] = now_str

    alerts.extend(exit_alerts)
    save_alert_cache(cache)

    return {
        "timestamp": now.isoformat(),
        "time": now_str,
        "tracked_count": len(universe),
        "tracked_stocks": universe,
        "sector_member_count": len(sector_members),
        "sector_monitor_status": "degraded" if sector_degradation else "ok",
        "sector_alerts": sector_alerts,
        "alerts": alerts,
        "exit_signals": exit_alerts,
        "has_alerts": len(alerts) > 0,
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true")
    p.add_argument("--force", action="store_true")
    args = p.parse_args()

    if not args.force and not in_trading_session():
        sys.exit(0)

    data = check_intraday()

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif data["has_alerts"]:
        print(f"⚡ 盘中异动 | {data['time']}")
        for a in data["alerts"]:
            print(f"  {a['level']} {a['msg']}")
    # 无触发则静默
