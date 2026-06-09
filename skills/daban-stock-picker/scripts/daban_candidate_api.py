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
import os
import sys
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "common"))
from tradeability import assess_tradeability, limit_pct
import daban_config as _cfg

# 阈值走单一事实源 config/daban_thresholds.yaml（回退默认与历史硬编码一致），
# 与回测引擎 daban_bt_engine 共读，确保"实盘用的窗口==回测验证过的窗口"。
_UNI = _cfg.section("universe")
_MKT = _cfg.section("market_gate")
_FBR = _cfg.section("first_board_reseal")
_SBW = _cfg.section("second_board_weak_to_strong")


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
    pattern = candidate.get("pattern") or candidate.get("signal_type")
    first_time = parse_time_minutes(candidate.get("first_limitup_time"))
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
    first_time = parse_time_minutes(candidate.get("first_limitup_time"))
    pattern = candidate.get("pattern") or candidate.get("signal_type")
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
            "question": "它是不是09:35-10:30早盘强回封？",
            "passed": (
                pattern in {"first_board_reseal", "second_board_weak_to_strong"}
                and first_time is not None
                and parse_time_minutes("09:35") <= first_time <= parse_time_minutes("10:30")
            ),
            "reason": f"first_limitup_time={candidate.get('first_limitup_time', 'N/A')}",
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
    questions = _six_questions(candidate, market, sector_limitups)
    veto_no_count = sum(1 for q in questions if not q["passed"])

    blocked_reasons = hard_rejections + market_rejections + pattern_rejections
    if trade.get("tradeable") is False:
        blocked_reasons.append(trade.get("reason", "不可成交"))
    if veto_no_count >= 2:
        blocked_reasons.append(f"六问否决：{veto_no_count}项为否")

    score, grade = _score_candidate(
        hard_rejections,
        pattern_rejections,
        market_rejections,
        veto_no_count,
        trade,
        candidate,
    )
    price = quote.get("price") or candidate.get("entry_price")

    return {
        "code": code,
        "name": name,
        "sector": candidate.get("sector", "Unknown"),
        "pattern": candidate.get("pattern") or candidate.get("signal_type"),
        "score": score,
        "grade": grade,
        "blocked": bool(blocked_reasons),
        "block_reasons": blocked_reasons,
        "tradeability": trade,
        "six_question_veto": {
            "no_count": veto_no_count,
            "blocked": veto_no_count >= 2,
            "questions": questions,
        },
        "entry_plan": {
            "window": "09:35-10:30",
            "initial_position_pct": 20 if not blocked_reasons else 0,
            "max_single_ticket_pct": 50,
            "max_total_exposure_pct": 60,
            "buy_condition": "只做早盘强回封/二板弱转强；一字封死或停牌不买",
        },
        "t1_exit_plan": {
            "priority": "先处理已有持仓，再考虑新开仓",
            "low_open_exit": "T+1低开<-3%且主线走弱，集合竞价/开盘快速机械卖出",
            "high_open_exit": "T+1高开6%-9%，09:30-09:45卖出1/2，余仓看封板/5日线",
            "broken_board_exit": "首次放量断板且15分钟内不能回封，退出剩余仓位",
        },
        "record_payload": {
            "code": code,
            "name": name,
            "grade": grade,
            "score": round(score / 10.0, 1),
            "price": price,
            "strategy_id": f"daban:{candidate.get('pattern') or candidate.get('signal_type')}",
        } if not blocked_reasons and price is not None else None,
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
                "code": "002156",
                "name": "通富微电",
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
