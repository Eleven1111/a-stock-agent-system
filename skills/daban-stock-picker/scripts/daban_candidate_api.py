#!/usr/bin/env python3
"""
打板候选 JSON API — 主板 10cm 首板回封 / 二板弱转强
===================================================
接收结构化行情、板块、候选池特征，输出可供 stock-triage 消费的候选 JSON。

Usage:
  python skills/daban-stock-picker/scripts/daban_candidate_api.py --input fixtures.json --json
  python skills/daban-stock-picker/scripts/daban_candidate_api.py --example --json
"""

import argparse
import json
import sys
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path
from tradeability import assess_tradeability, limit_pct
from daban_adjustments import entry_mode_multiplier
import daban_config as _cfg

# 阈值走单一事实源 config/daban_thresholds.yaml（回退默认与历史硬编码一致），
# 与回测引擎 daban_bt_engine 共读，确保"实盘用的窗口==回测验证过的窗口"。
_UNI = _cfg.section("universe")
_MKT = _cfg.section("market_gate")
_FBR = _cfg.section("first_board_reseal")
_SBW = _cfg.section("second_board_weak_to_strong")

PATTERN_STRATEGY_IDS = {
    "first_board_reseal": "daban:first_board_reseal_v2",
    "second_board_weak_to_strong": "daban:second_board_w2s_v2",
}
EVENT_SCHEMA = "daban_event_v2"


ENGINE = {
    "name": "daban-stock-picker",
    "version": "1.0.0",
    "strategy_scope": "A-share main-board 10cm limit-up",
    "patterns": ["first_board_reseal", "second_board_weak_to_strong"],
    "upstream_reference": "Eleven1111/chanlun-backtest@f25b36a",
}


def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "是"}
    return bool(value)


def parse_time_minutes(value: Any) -> Optional[int]:
    """Parse HH:MM into minutes since midnight."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if ":" not in text:
        return None
    hour, minute = text.split(":", 1)
    try:
        return int(hour) * 60 + int(minute)
    except ValueError:
        return None


def _first_present(mapping: Dict[str, Any], *keys: str) -> Any:
    """Return the first supplied value, retaining explicit False/zero values."""
    for key in keys:
        if key in mapping and mapping.get(key) not in (None, ""):
            return mapping.get(key)
    return None


_OBSERVABLE_PROXY_NAMES = (
    "up_down_volume_ratio",
    "vwap_hold_ratio",
    "obv_slope",
    "mfi",
    "consecutive_large_order_net",
    "lhb_institution_net_buy",
    "industry_fund_flow",
)


def _proxy_observation(value: Any = None, *, source: str = "unavailable",
                       asof: Any = None, asof_lag_days: Any = None) -> Dict[str, Any]:
    if value is not None and (source == "unavailable" or asof is None):
        value = None
    result = {"value": value, "source": source, "asof": asof}
    if asof_lag_days is not None:
        result["asof_lag_days"] = asof_lag_days
    return result


def _observable_proxies(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize candidate-supplied proxies; missing evidence stays missing."""
    candidate = candidate if isinstance(candidate, dict) else {}
    observations = {}
    aliases = {
        "consecutive_large_order_net": ("large_order_net", "large_order_net_yi"),
        "lhb_institution_net_buy": ("institution_lhb_net_buy", "institution_lhb_net_wan"),
        "industry_fund_flow": ("sector_fund_flow", "industry_net_flow"),
    }
    for name in _OBSERVABLE_PROXY_NAMES:
        raw = next((candidate[key] for key in (name, *aliases.get(name, ())) if key in candidate), None)
        if isinstance(raw, dict):
            value = raw.get("value")
            source = raw.get("source") or candidate.get("source") or "candidate_input"
            asof = raw.get("asof") or candidate.get("asof") or candidate.get("date")
            lag = raw.get("asof_lag_days")
        else:
            value = raw
            source = candidate.get("source") or "candidate_input"
            asof = candidate.get("asof") or candidate.get("date")
            lag = candidate.get(f"{name}_asof_lag_days")
        if value is None:
            observations[name] = _proxy_observation()
        else:
            if name == "lhb_institution_net_buy" and lag is None:
                lag = candidate.get("lhb_asof_lag_days", 1)
            observations[name] = _proxy_observation(
                value, source=source, asof=asof, asof_lag_days=lag,
            )
    return {
        **{name: obs.get("value") for name, obs in observations.items()},
        "observable_proxies": observations,
        "proxy_provenance": {
            name: {key: value for key, value in obs.items() if key != "value"}
            for name, obs in observations.items()
        },
    }


def _pattern(candidate: Dict[str, Any]) -> Optional[str]:
    return candidate.get("pattern") or candidate.get("signal_type")


def _first_seal(candidate: Dict[str, Any]) -> Any:
    # ``first_seal`` is the shared candidate-pipeline evidence name.  The
    # older API accepted ``first_limitup_time``; keep accepting it at input.
    return _first_present(
        candidate, "first_seal", "first_seal_time", "first_limitup_time"
    )


def _seal_quality(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize reseal evidence into the shared event vocabulary.

    Missing observations remain ``None``.  In particular, a missing final
    seal observation must not be silently treated as a surviving seal.
    """
    first_seal = _first_seal(candidate)
    final_seal = _first_present(
        candidate,
        "final_seal",
        "final_seal_time",
        "last_seal",
        "last_seal_time",
        "final_limitup_time",
    )
    open_count = _first_present(candidate, "open_board_count", "open_count")
    total_open = _first_present(
        candidate,
        "total_open_minutes",
        "cumulative_open_minutes",
        "cumulative_open_duration_minutes",
        "open_board_minutes",
    )
    max_open = _first_present(
        candidate,
        "max_open_minutes",
        "longest_open_minutes",
        "max_open_duration_minutes",
    )
    reseal_volume = _first_present(
        candidate, "reseal_volume", "reseal_trade_volume", "reseal_volume_amount"
    )
    final_survival = _first_present(
        candidate,
        "final_seal_survival",
        "final_seal_order_survival",
        "seal_survival",
        "sealed_at_close",
    )
    explicit_tail = _first_present(candidate, "tail_seal_after_14_30")
    if explicit_tail is not None:
        tail_seal = _bool(explicit_tail)
    else:
        final_minutes = parse_time_minutes(final_seal)
        tail_seal = final_minutes is not None and final_minutes > parse_time_minutes("14:30")

    return {
        # Names used by candidate_pipeline's ladder evidence.
        "first_seal": first_seal,
        "final_seal": final_seal,
        "open_board_count": int(_num(open_count)) if _num(open_count) is not None else None,
        "total_open_minutes": _num(total_open),
        "max_open_minutes": _num(max_open),
        "reseal_volume": _num(reseal_volume),
        "final_seal_survival": final_survival,
        "tail_seal_after_14_30": tail_seal,
        # Explicit time/duration aliases make the event self-describing for
        # consumers that do not use the ladder adapter.
        "first_seal_time": first_seal,
        "final_seal_time": final_seal,
        "last_seal": final_seal,
        "last_seal_time": final_seal,
        "cumulative_open_minutes": _num(total_open),
        "total_open_duration": _num(total_open),
        "longest_open_minutes": _num(max_open),
        "max_open_duration": _num(max_open),
        "reseal_trade_volume": _num(reseal_volume),
        "final_seal_order_survival": final_survival,
    }


def _strategy_id(pattern: Optional[str]) -> Optional[str]:
    return PATTERN_STRATEGY_IDS.get(pattern)


def _sector_count(candidate: Dict[str, Any], market: Dict[str, Any]) -> int:
    if candidate.get("sector_limitup_count") is not None:
        return int(_num(candidate.get("sector_limitup_count"), 0) or 0)
    sector = candidate.get("sector")
    sectors = market.get("sectors", [])
    if isinstance(sectors, dict):
        info = sectors.get(sector, {})
        return int(_num(info.get("limitup_count"), 0) or 0)
    for item in sectors:
        if item.get("name") == sector:
            return int(_num(item.get("limitup_count"), 0) or 0)
    return 0


def _quote_from_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    quote = candidate.get("quote")
    if isinstance(quote, dict):
        return {
            "price": _num(quote.get("price")),
            "prev_close": _num(quote.get("prev_close") or quote.get("close_prev")),
            "open": _num(quote.get("open")),
            "high": _num(quote.get("high")),
            "low": _num(quote.get("low")),
            "volume": _num(quote.get("volume")),
        }
    return {
        "price": _num(candidate.get("price")),
        "prev_close": _num(candidate.get("prev_close") or candidate.get("close_prev")),
        "open": _num(candidate.get("open")),
        "high": _num(candidate.get("high")),
        "low": _num(candidate.get("low")),
        "volume": _num(candidate.get("volume")),
    }


def _hard_pool_rejections(candidate: Dict[str, Any]) -> List[str]:
    code = str(candidate.get("code", ""))
    name = str(candidate.get("name", ""))
    rejections = []

    if limit_pct(code, name) != 10.0 or not code.startswith(("00", "60")):
        rejections.append("仅支持A股主板10cm打板，排除创业板/科创板/北交所/ST")
    if _bool(candidate.get("is_st"), False):
        rejections.append("ST/*ST股票一票否决")
    if _num(candidate.get("listed_days"), 9999) < _UNI["listed_days_min"]:
        rejections.append("上市未满60天")

    float_cap = _num(candidate.get("float_market_cap") or candidate.get("float_mktcap"))
    if float_cap is not None and not (_UNI["float_mktcap_min"] <= float_cap <= _UNI["float_mktcap_max"]):
        rejections.append("流通市值不在15亿-120亿")

    avg_turnover = _num(candidate.get("avg_turnover_amount_20d") or candidate.get("avg_turnover_20d"))
    if avg_turnover is not None and avg_turnover < _UNI["avg_turnover_20d_min"]:
        rejections.append("20日平均成交额低于2亿元")

    close_prev = _num(candidate.get("close_prev") or candidate.get("prev_close"))
    if close_prev is not None and not (_UNI["close_prev_min"] <= close_prev <= _UNI["close_prev_max"]):
        rejections.append("前收盘价不在4-35元打板价格带")

    return rejections


def _market_rejections(market: Dict[str, Any], portfolio: Dict[str, Any]) -> List[str]:
    rejections = []
    if _num(market.get("yday_limitup_index_open"), 0.0) < _MKT["yday_limitup_index_open_min"]:
        rejections.append("昨日涨停指数开盘低于-2%，强股反馈差")
    if _num(market.get("broken_rate_first20m"), 0.0) > _MKT["broken_rate_first20m_max"]:
        rejections.append("早盘20分钟炸板率高于35%")
    if _bool(portfolio.get("has_positions_to_dispose"), False):
        rejections.append("T+1处置优先：仍有持仓待处理，先卖后买")
    if _num(portfolio.get("week_trades"), 0.0) >= _MKT["week_trades_max"]:
        rejections.append("本周新开仓已达3笔上限")
    if _num(portfolio.get("day_loss_pct"), 0.0) <= _MKT["day_loss_pct_stop"]:
        rejections.append("日度亏损达到-2%停手线")
    if _num(portfolio.get("week_loss_pct"), 0.0) <= _MKT["week_loss_pct_freeze"]:
        rejections.append("周度亏损达到-5%冻结线")
    if _num(portfolio.get("consecutive_losses"), 0.0) >= _MKT["consecutive_losses_max"]:
        rejections.append("连续错单3次，冻结交易")
    return rejections


def _pattern_rejections(candidate: Dict[str, Any], sector_limitups: int) -> List[str]:
    pattern = _pattern(candidate)
    first_time = parse_time_minutes(_first_seal(candidate))
    rejections = []

    if pattern == "first_board_reseal":
        if first_time is None or first_time > parse_time_minutes(_FBR["first_limitup_latest"]):
            rejections.append("首板首次上板晚于10:30")
        if _num(candidate.get("open_board_count"), 99) > _FBR["open_board_max"]:
            rejections.append("炸板次数超过2次")
        if _num(candidate.get("reseal_minutes") or candidate.get("reseal_time"), 99) > _FBR["reseal_minutes_max"]:
            rejections.append("回封耗时超过15分钟")
        seal_amount = _num(candidate.get("seal_amount") or candidate.get("seal_amt"), 0.0) or 0.0
        float_cap = _num(candidate.get("float_market_cap") or candidate.get("float_mktcap"), 0.0) or 0.0
        if float_cap <= 0 or seal_amount / float_cap < _FBR["seal_amount_ratio_min"]:
            rejections.append("封单额/流通市值低于0.3%")
        if _num(candidate.get("active_buy_ratio"), 0.0) < _FBR["active_buy_ratio_min"]:
            rejections.append("主动买入占比低于60%")
        inflow_ratio = _num(candidate.get("big_order_net_inflow_ratio"))
        if inflow_ratio is None:
            turnover = _num(candidate.get("turnover"), 0.0) or 0.0
            inflow = _num(candidate.get("big_order_net_inflow"), 0.0) or 0.0
            inflow_ratio = inflow / turnover if turnover > 0 else 0.0
        if inflow_ratio < _FBR["big_order_inflow_ratio_min"]:
            rejections.append("大单净流入/成交额低于8%")
        if sector_limitups < _FBR["sector_limitup_min"]:
            rejections.append("板块涨停少于3只，缺少集群共振")
    elif pattern == "second_board_weak_to_strong":
        if not _bool(candidate.get("prev_day_limitup_close"), False):
            rejections.append("二板弱转强要求前一日涨停收盘")
        auction_gap = _num(candidate.get("auction_gap_pct"), 999.0)
        if auction_gap is None or not (_SBW["auction_gap_low"] <= auction_gap <= _SBW["auction_gap_high"]):
            rejections.append("竞价涨幅不在-1%到+3%弱转强窗口")
        if first_time is None or first_time > parse_time_minutes(_SBW["first_limitup_latest"]):
            rejections.append("二板首次上板晚于09:45")
        companion = int(_num(candidate.get("sector_companion_count"), sector_limitups) or 0)
        if companion < _SBW["sector_companion_min"]:
            rejections.append("同板块跟随涨停/冲板少于2只")
    else:
        rejections.append("未知打板模式，必须是first_board_reseal或second_board_weak_to_strong")

    return rejections


def _six_questions(candidate: Dict[str, Any], market: Dict[str, Any], sector_limitups: int) -> List[Dict[str, Any]]:
    first_time = parse_time_minutes(_first_seal(candidate))
    pattern = _pattern(candidate)
    if pattern == "first_board_reseal":
        timing_passed = (
            first_time is not None
            and parse_time_minutes("09:35") <= first_time <= parse_time_minutes("10:30")
        )
        timing_question = "它是不是09:35-10:30早盘强回封？"
    elif pattern == "second_board_weak_to_strong":
        timing_passed = first_time is not None and first_time <= parse_time_minutes("09:45")
        timing_question = "二板弱转强首次上板是否不晚于09:45？"
    else:
        timing_passed = False
        timing_question = "是否符合已注册的打板形态时间窗？"
    questions = [
        {
            "id": "sentiment_score",
            "question": "今天短线情绪评分是否 >= 7？",
            "passed": _num(market.get("sentiment_score"), 0.0) >= 7.0,
            "reason": f"sentiment_score={_num(market.get('sentiment_score'), 0.0)}",
        },
        {
            "id": "main_theme",
            "question": "有没有明确主线板块？",
            "passed": bool(market.get("main_theme") or candidate.get("sector")),
            "reason": f"main_theme={market.get('main_theme') or candidate.get('sector') or 'N/A'}",
        },
        {
            "id": "sector_cluster",
            "question": "板块内涨停是否 >= 3只？",
            "passed": sector_limitups >= _FBR["sector_limitup_min"],
            "reason": f"sector_limitup_count={sector_limitups}",
        },
        {
            "id": "leader_or_front",
            "question": "目标股是不是龙头或前排？",
            "passed": _bool(candidate.get("is_leader"), False) or _bool(candidate.get("is_front_runner"), False),
            "reason": "leader/front-runner"
            if (_bool(candidate.get("is_leader"), False) or _bool(candidate.get("is_front_runner"), False))
            else "not leader/front-runner",
        },
        {
            "id": "morning_reseal",
            "question": timing_question,
            "passed": timing_passed,
            "reason": f"first_seal={_first_seal(candidate) or 'N/A'}",
        },
        {
            "id": "mechanical_exit",
            "question": "次日低开是否愿意机械卖出？",
            "passed": _bool(candidate.get("accepts_mechanical_exit"), False),
            "reason": "confirmed" if _bool(candidate.get("accepts_mechanical_exit"), False) else "not confirmed",
        },
    ]
    return questions


def _score_candidate(
    hard_rejections: List[str],
    pattern_rejections: List[str],
    market_rejections: List[str],
    veto_no_count: int,
    trade_status: Dict[str, Any],
    candidate: Dict[str, Any],
) -> Tuple[float, str]:
    score = 100.0
    score -= 12.0 * len(hard_rejections)
    score -= 8.0 * len(pattern_rejections)
    score -= 10.0 * len(market_rejections)
    score -= 8.0 * veto_no_count
    if trade_status.get("tradeable") is False:
        score -= 30.0
    elif trade_status.get("tradeable") == "risky":
        score -= 8.0
    if _bool(candidate.get("is_leader"), False):
        score += 5.0
    if _bool(candidate.get("is_front_runner"), False):
        score += 3.0
    score *= entry_mode_multiplier(
        candidate.get("pattern") or candidate.get("signal_type")
    )
    score = max(0.0, min(100.0, score))
    if score >= 85:
        grade = "S"
    elif score >= 70:
        grade = "A"
    elif score >= 55:
        grade = "B"
    else:
        grade = "C"
    return round(score, 1), grade


def t1_scenario(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """T+1 竞价证伪场景决策树（游资风控决策树，纯函数）。

    按当日封板质量分三档，预案在次日 9:15-9:25 竞价阶段执行（T+1 制度下
    竞价出局优于开盘后挨"核按钮"）：
    - A 高位强封（零炸板）：竞价≥+3% 持有、跌破5日线减半；竞价<0% 开盘3分钟减半
    - B 烂板回封（有炸板尾市封回）：竞价弱只观察承接，不默认补仓；平开/微红冲高全清
    - C 收盘未封回：无论竞价，开盘3分钟无条件全清
    """
    sealed_at_close = _bool(candidate.get("sealed_at_close"), True)
    open_boards = int(_num(candidate.get("open_board_count"), 0) or 0)
    if not sealed_at_close:
        return {
            "scenario": "C",
            "seal_quality": "收盘前开板未封回，封板失败",
            "auction_plan": "无论竞价表现，开盘3分钟内无条件斩仓全清",
            "allowed_actions": ["exit"],
        }
    if open_boards >= 1:
        return {
            "scenario": "B",
            "seal_quality": f"炸板{open_boards}次尾市封回，多空分歧大",
            "auction_plan": "竞价≤-4%仅观察大单承接；"
                            "平开或微红即丧失向上动能，开盘冲高过程全清",
            "allowed_actions": ["reduce", "exit", "observe"],
        }
    return {
        "scenario": "A",
        "seal_quality": "高位强封无炸板，筹码锁定良好",
        "auction_plan": "竞价≥+3%持有(跌破5日线减半)；竞价<0%严重弱于预期，开盘3分钟内减仓50%",
        "allowed_actions": ["reduce", "exit", "observe"],
    }


def _event_payload(
    candidate: Dict[str, Any],
    *,
    code: str,
    name: str,
    price: Any,
    score: float,
    grade: str,
    quality: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the stable event/evidence shape consumed by downstream ranking.

    ``candidate_pipeline`` calls these fields strategy identity/state evidence;
    keeping the same names here avoids a second, pattern-specific vocabulary.
    """
    pattern = _pattern(candidate)
    strategy_id = _strategy_id(pattern)
    confidence = (
        "high" if score >= 80.0 else "medium" if score >= 55.0 else "low"
    )
    net_expectancy = _num(candidate.get("daban_net_expectancy"), score / 100.0)
    observable_proxies = _observable_proxies(candidate)
    evidence = {
        "schema": "daban_evidence_v2",
        "strategy_identity": strategy_id,
        "primary_strategy_id": strategy_id,
        "primary_score": score,
        "primary_net_expectancy": round(net_expectancy, 6),
        "primary_confidence": confidence,
        "strategy_live_score": score,
        "strategy_state": {
            "primary_strategy_id": strategy_id,
            "primary_score": score,
            "daban_net_expectancy": net_expectancy,
            "daban_confidence": confidence,
        },
        "seal_quality": quality,
        **observable_proxies,
    }
    return {
        "schema": EVENT_SCHEMA,
        "event_type": "daban.candidate",
        "code": code,
        "name": name,
        "pattern": pattern,
        "strategy_id": strategy_id,
        "strategy_identity": strategy_id,
        "primary_strategy_id": strategy_id,
        "primary_score": score,
        "primary_net_expectancy": evidence["primary_net_expectancy"],
        "primary_confidence": evidence["primary_confidence"],
        "strategy_live_score": score,
        "strategy_state": evidence["strategy_state"],
        "strategy_state_event": {
            "event": "strategy_identity_selected",
            "from": None,
            "to": strategy_id,
        },
        "signal_price": price,
        "signal_date": candidate.get("signal_date") or candidate.get("date"),
        "evidence_time": candidate.get("evidence_time") or candidate.get("asof"),
        "evidence": evidence,
        "seal_quality": quality,
        **observable_proxies,
    }


def _t1_exit_plan(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "priority": "先处理已有持仓，再考虑新开仓",
        **t1_scenario(candidate),
        "high_open_exit": "T+1高开6%-9%，09:30-09:45卖出1/2，余仓看封板/5日线",
        "broken_board_exit": "首次放量断板且15分钟内不能回封，退出剩余仓位",
    }


def _identity_fields(
    event: Dict[str, Any], *, strategy_id: str, score: float
) -> Dict[str, Any]:
    """Strategy identity and score aliases carried by every candidate payload."""
    return {
        "strategy_id": strategy_id,
        "strategy_identity": strategy_id,
        "primary_strategy_id": strategy_id,
        "primary_score": score,
        "primary_net_expectancy": event["evidence"]["primary_net_expectancy"],
        "primary_confidence": event["evidence"]["primary_confidence"],
        "strategy_live_score": score,
        "strategy_state": event["evidence"]["strategy_state"],
        "strategy_state_event": event["strategy_state_event"],
    }


def _blocked_reasons(
    rejections: List[str], *, trade: Dict[str, Any], veto_no_count: int
) -> List[str]:
    reasons = list(rejections)
    if trade.get("tradeable") is False:
        reasons.append(trade.get("reason", "不可成交"))
    if veto_no_count >= 2:
        reasons.append(f"六问否决：{veto_no_count}项为否")
    return reasons


def _entry_plan(pattern: str, *, blocked: bool) -> Dict[str, Any]:
    return {
        "window": (
            "09:35-10:30"
            if pattern == "first_board_reseal"
            else "首次上板不晚于09:45"
            if pattern == "second_board_weak_to_strong"
            else None
        ),
        "initial_position_pct": 0 if blocked else 20,
        "max_single_ticket_pct": 50,
        "max_total_exposure_pct": 60,
        "buy_condition": "只做早盘强回封/二板弱转强；一字封死或停牌不买",
    }


def _record_payload(
    candidate: Dict[str, Any],
    event: Dict[str, Any],
    quality: Dict[str, Any],
    observable_proxies: Dict[str, Any],
    *,
    code: str,
    name: str,
    grade: str,
    score: float,
    price: Any,
    strategy_id: str,
) -> Dict[str, Any]:
    """The ledger row for an executable candidate; callers gate on blocked/price."""
    return {
        "schema": EVENT_SCHEMA,
        "event_type": "daban.candidate",
        "code": code,
        "name": name,
        "grade": grade,
        "score": round(score / 10.0, 1),
        "price": price,
        "signal_price": price,
        "signal_date": candidate.get("signal_date") or candidate.get("date"),
        "strategy_id": strategy_id,
        "strategy_identity": strategy_id,
        "primary_strategy_id": strategy_id,
        "primary_score": score,
        "primary_net_expectancy": event["evidence"]["primary_net_expectancy"],
        "primary_confidence": event["evidence"]["primary_confidence"],
        "strategy_live_score": score,
        "strategy_state": event["strategy_state"],
        "strategy_state_event": event["strategy_state_event"],
        "evidence": event["evidence"],
        **observable_proxies,
        "seal_quality": quality,
        "action": "buy",
        "source": "daban_candidate_api",
    }


def evaluate_candidate(candidate: Dict[str, Any], market: Dict[str, Any], portfolio: Dict[str, Any]) -> Dict[str, Any]:
    code = str(candidate.get("code", "")).zfill(6)
    name = candidate.get("name", code)
    sector_limitups = _sector_count(candidate, market)
    quote = _quote_from_candidate(candidate)
    trade = assess_tradeability(quote, code, name) if quote.get("price") is not None else {
        "tradeable": False,
        "status": "missing_quote",
        "reason": "缺少实时/候选价格，不能判断是否可成交",
    }

    hard_rejections = _hard_pool_rejections({**candidate, "code": code})
    market_rejections = _market_rejections(market, portfolio)
    pattern_rejections = _pattern_rejections(candidate, sector_limitups)
    pattern = _pattern(candidate)
    quality = _seal_quality(candidate)
    if quality["tail_seal_after_14_30"]:
        pattern_rejections.append("尾盘14:30后封板，仅研究观察，不进入可执行推荐")
    questions = _six_questions(candidate, market, sector_limitups)
    veto_no_count = sum(1 for q in questions if not q["passed"])

    blocked_reasons = _blocked_reasons(
        hard_rejections + market_rejections + pattern_rejections,
        trade=trade,
        veto_no_count=veto_no_count,
    )
    score, grade = _score_candidate(
        hard_rejections,
        pattern_rejections,
        market_rejections,
        veto_no_count,
        trade,
        candidate,
    )
    price = quote.get("price") or candidate.get("entry_price")
    strategy_id = _strategy_id(pattern)
    event = _event_payload(
        candidate,
        code=code,
        name=name,
        price=price,
        score=score,
        grade=grade,
        quality=quality,
    )
    observable_proxies = _observable_proxies(candidate)
    research_only = bool(quality["tail_seal_after_14_30"])

    return {
        "code": code,
        "name": name,
        "sector": candidate.get("sector", "Unknown"),
        "pattern": pattern,
        **_identity_fields(event, strategy_id=strategy_id, score=score),
        "score": score,
        "grade": grade,
        "blocked": bool(blocked_reasons),
        "research_only": research_only,
        "execution_status": "research_only" if research_only else (
            "blocked" if blocked_reasons else "executable"
        ),
        "seal_quality": quality,
        **quality,
        "event": event,
        "block_reasons": blocked_reasons,
        "tradeability": trade,
        **observable_proxies,
        "six_question_veto": {
            "no_count": veto_no_count,
            "blocked": veto_no_count >= 2,
            "questions": questions,
        },
        "entry_plan": _entry_plan(pattern, blocked=bool(blocked_reasons)),
        "t1_exit_plan": _t1_exit_plan(candidate),
        "record_payload": _record_payload(
            candidate, event, quality, observable_proxies,
            code=code, name=name, grade=grade, score=score, price=price,
            strategy_id=strategy_id,
        ) if not blocked_reasons and price is not None and not research_only else None,
    }


def evaluate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    market = payload.get("market", {})
    portfolio = payload.get("portfolio", {})
    candidates = payload.get("candidates", [])
    evaluated = [evaluate_candidate(c, market, portfolio) for c in candidates]
    evaluated.sort(key=lambda c: (c["blocked"], -c["score"], c["code"]))
    top = [c for c in evaluated if not c["blocked"]][:3]
    blocked = not top

    return {
        "schema": "daban_candidate_v1",
        "generated_at": datetime.now().isoformat(),
        "asof": payload.get("asof") or date.today().isoformat(),
        "engine": ENGINE,
        "market_gate": {
            "sentiment_score": _num(market.get("sentiment_score"), 0.0),
            "main_theme": market.get("main_theme"),
            "yday_limitup_index_open": _num(market.get("yday_limitup_index_open"), 0.0),
            "broken_rate_first20m": _num(market.get("broken_rate_first20m"), 0.0),
        },
        "portfolio_gate": {
            "has_positions_to_dispose": _bool(portfolio.get("has_positions_to_dispose"), False),
            "week_trades": int(_num(portfolio.get("week_trades"), 0.0) or 0),
            "day_loss_pct": _num(portfolio.get("day_loss_pct"), 0.0),
            "week_loss_pct": _num(portfolio.get("week_loss_pct"), 0.0),
            "consecutive_losses": int(_num(portfolio.get("consecutive_losses"), 0.0) or 0),
        },
        "blocked": blocked,
        "summary": "无可执行打板候选，保持现金/观察" if blocked else f"{len(top)}只候选通过打板闸门",
        "top_candidates": top,
        "candidates": evaluated,
    }


def format_report(result: Dict[str, Any]) -> str:
    lines = [
        f"## 打板候选池 | {result['asof']}",
        f"结论：{result['summary']}",
        "",
    ]
    if result["blocked"]:
        lines.append("当前无可执行候选。")
    else:
        lines.append("| 代码 | 名称 | 板块 | 模式 | 等级 | 分数 | 仓位 |")
        lines.append("|------|------|------|------|------|------|------|")
        for c in result["top_candidates"]:
            lines.append(
                f"| {c['code']} | {c['name']} | {c['sector']} | {c['pattern']} | "
                f"{c['grade']} | {c['score']} | {c['entry_plan']['initial_position_pct']}% |"
            )
    blocked = [c for c in result["candidates"] if c["blocked"]]
    if blocked:
        lines.append("")
        lines.append("### 被否决候选")
        for c in blocked[:5]:
            lines.append(f"- {c['name']}({c['code']}): {'; '.join(c['block_reasons'][:3])}")
    return "\n".join(lines)


def example_payload() -> Dict[str, Any]:
    return {
        "asof": "2026-06-03",
        "market": {
            "sentiment_score": 8.0,
            "main_theme": "半导体",
            "yday_limitup_index_open": 0.6,
            "broken_rate_first20m": 18.0,
            "sectors": [{"name": "半导体", "limitup_count": 5}],
        },
        "portfolio": {
            "has_positions_to_dispose": False,
            "week_trades": 1,
            "day_loss_pct": 0.0,
            "week_loss_pct": 0.5,
            "consecutive_losses": 0,
        },
        "candidates": [
            {
                "code": "600001",
                "name": "示例股份",
                "sector": "半导体",
                "pattern": "first_board_reseal",
                "price": 11.0,
                "prev_close": 10.0,
                "open": 10.2,
                "high": 11.0,
                "low": 10.1,
                "volume": 500000,
                "listed_days": 3000,
                "float_market_cap": 8.0e9,
                "avg_turnover_amount_20d": 4.0e8,
                "close_prev": 10.0,
                "first_limitup_time": "09:52",
                "open_board_count": 1,
                "reseal_minutes": 8,
                "seal_amount": 4.0e7,
                "active_buy_ratio": 0.68,
                "big_order_net_inflow_ratio": 0.10,
                "is_leader": True,
                "accepts_mechanical_exit": True,
            }
        ],
    }


def load_payload(path: Optional[str], use_example: bool) -> Dict[str, Any]:
    if use_example:
        return example_payload()
    if path:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    if not sys.stdin.isatty():
        return json.load(sys.stdin)
    return {"asof": date.today().isoformat(), "market": {}, "portfolio": {}, "candidates": []}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="A股主板10cm打板候选池 API")
    parser.add_argument("--input", help="JSON input file. If omitted, reads stdin when piped.")
    parser.add_argument("--example", action="store_true", help="Run with built-in example payload.")
    parser.add_argument("--json", action="store_true", help="Output JSON.")
    args = parser.parse_args()

    payload = load_payload(args.input, args.example)
    output = evaluate_payload(payload)
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(format_report(output))
