#!/usr/bin/env python3
"""
09:35 open confirmation for the limit-up candidate workflow.

Reads the 09:25 auction factor state, fetches current Tencent quotes, and emits
a compact JSON decision surface for stock-triage. It does not place orders.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Sequence

SCRIPT_DIR = os.path.dirname(__file__)
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, "..", "..", "common"))

from a_stock_http import DataSourceError, fetch_tencent_snapshot  # noqa: E402
import candidate_lifecycle  # noqa: E402
import candidate_pipeline  # noqa: E402
from market_temperature import temperature_from_context  # noqa: E402
from paths import data_file  # noqa: E402
from signal_context import read_signal_context  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402
from tradeability import assess_tradeability  # noqa: E402

QUOTE_BATCH_SIZE = 80


def _naked_code(code: str) -> str:
    return code[2:] if code.startswith(("sh", "sz")) else code


def evaluate_open_confirmation(factor: Dict[str, Any], quote: Dict[str, Any]) -> Dict[str, Any]:
    code = factor.get("code", "")
    name = factor.get("name") or quote.get("name") or code
    tradeability = assess_tradeability(quote, _naked_code(code), name)
    change_pct = quote.get("change_pct")
    price = quote.get("price")

    action = "skip"
    reasons: List[str] = []
    if factor.get("error"):
        reasons.append(factor["error"])
    elif tradeability.get("tradeable") is False:
        action = "not_buyable"
        reasons.append(tradeability.get("reason", "不可成交"))
    elif factor.get("is_yiziban"):
        action = "not_buyable"
        reasons.append("09:25一字封死，高分也可能打不进")
    elif tradeability.get("status") == "limit_up":
        action = "queue_or_skip"
        reasons.append("已封涨停，仅可排队且不保证成交")
    elif change_pct is not None and 3.0 <= change_pct < 9.5:
        action = "trend_watch"
        reasons.append("符合用户偏好的3%-10%中度上涨观察窗口")
    elif factor.get("board_status") in {"high_open", "limit_up_with_ask"}:
        action = "watch"
        reasons.append("竞价强但开盘未形成明确可执行信号")
    else:
        reasons.append("开盘确认不足")

    return {
        "code": code,
        "name": name,
        "price": price,
        "change_pct": change_pct,
        "auction_gap_pct": factor.get("auction_gap_pct"),
        "board_status": factor.get("board_status"),
        "action": action,
        "tradeability": tradeability,
        "reasons": reasons,
    }


def _shortlist_path(asof: str) -> str:
    return data_file("daban-stock-picker", f"auction_shortlist_{asof}.json")


def _confirmation_path(asof: str) -> str:
    return data_file("daban-stock-picker", f"open_confirmation_{asof}.json")


def _confirmation_latest_path() -> str:
    return data_file("daban-stock-picker", "open_confirmation_latest.json")


def load_shortlist(asof: str) -> Dict[str, Any]:
    shortlist = read_json(_shortlist_path(asof), {})
    if not isinstance(shortlist, dict) or shortlist.get("asof") != asof:
        raise DataSourceError("auction_shortlist", f"{asof} 竞价短名单缺失")
    return shortlist


def rank_confirmations(
    shortlist: Sequence[Mapping[str, Any]],
    confirmations: Sequence[Mapping[str, Any]],
    limit: int = 5,
    temperature: Mapping[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    prior = {
        candidate_pipeline.naked_code(item.get("code")): dict(item)
        for item in shortlist
    }
    action_quality = {"trend_watch": 1.0, "watch": 0.8}
    eligible = []
    for raw in confirmations:
        item = dict(raw)
        code = candidate_pipeline.naked_code(item.get("code"))
        if item.get("action") not in action_quality:
            continue
        merged = {**prior.get(code, {}), **item}
        change_pct = float(merged.get("change_pct") or 0.0)
        open_quality = max(0.0, 1.0 - abs(change_pct - 5.5) / 6.0)
        auction_score = float(merged.get("auction_score") or 0.0)
        merged["open_daban_score"] = round(
            0.65 * float(merged.get("auction_daban_score") or auction_score)
            + 20.0 * action_quality[item["action"]]
            + 15.0 * open_quality,
            2,
        )
        merged["open_trend_score"] = round(
            0.65 * float(merged.get("auction_trend_score") or auction_score)
            + 20.0 * action_quality[item["action"]]
            + 15.0 * open_quality,
            2,
        )
        merged["open_score"] = max(
            merged["open_daban_score"],
            merged["open_trend_score"],
        )
        eligible.append(merged)

    temperature_active = bool(
        temperature
        and temperature.get("tier") != "neutral"
        and temperature.get("context_fresh", True)
    )
    allow_new_daban = bool(
        not temperature_active or temperature.get("allow_new_daban", True)
    )
    daban_quota = (limit + 1) // 2
    if temperature_active:
        top_n_limit = temperature.get("top_n_limit")
        if not allow_new_daban:
            daban_quota = 0
        elif isinstance(top_n_limit, int):
            daban_quota = min(daban_quota, max(0, top_n_limit))
    trend_quota = max(0, limit - daban_quota)

    def _lane_member(item: Mapping[str, Any], lane: str) -> bool:
        if lane == "daban" and not allow_new_daban:
            return False
        selected_by = item.get("auction_selected_by") or item.get("selected_by")
        if isinstance(selected_by, Mapping):
            return bool(selected_by.get(lane))
        if lane == "daban":
            return candidate_pipeline.is_main_board_10cm(
                item.get("code"),
                str(item.get("name") or ""),
            )
        return True

    selected: Dict[str, Dict[str, Any]] = {}

    def _add_lane(lane: str, quota: int) -> None:
        if quota <= 0:
            return
        score_key = f"open_{lane}_score"
        ordered = sorted(
            (item for item in eligible if _lane_member(item, lane)),
            key=lambda row: (-float(row.get(score_key) or 0.0), str(row.get("code"))),
        )
        added = 0
        for item in ordered:
            code = candidate_pipeline.naked_code(item.get("code"))
            if code in selected:
                selected[code]["open_selected_by"][lane] = True
                continue
            chosen = dict(item)
            chosen["open_selected_by"] = {
                "daban": lane == "daban",
                "trend": lane == "trend",
                "balanced_fill": False,
            }
            selected[code] = chosen
            added += 1
            if added >= quota:
                break

    _add_lane("daban", daban_quota)
    _add_lane("trend", trend_quota)
    if len(selected) < limit:
        for item in sorted(
            eligible,
            key=lambda row: (-float(row.get("open_score") or 0.0), str(row.get("code"))),
        ):
            code = candidate_pipeline.naked_code(item.get("code"))
            if code in selected:
                continue
            if temperature_active and not _lane_member(item, "trend"):
                continue
            chosen = dict(item)
            chosen["open_selected_by"] = {
                "daban": False,
                "trend": temperature_active,
                "balanced_fill": not temperature_active,
            }
            selected[code] = chosen
            if len(selected) >= limit:
                break

    ranked = sorted(
        selected.values(),
        key=lambda item: (-item["open_score"], str(item.get("code"))),
    )[:limit]
    for index, item in enumerate(ranked, 1):
        item["open_rank"] = index
    return ranked


def _rejection_reasons(
    confirmations: Sequence[Mapping[str, Any]],
    selected_codes: set[str],
    limit: int,
) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    for item in confirmations:
        code = candidate_pipeline.naked_code(item.get("code"))
        if code in selected_codes:
            continue
        reasons = list(item.get("reasons") or [])
        if item.get("action") in {"watch", "trend_watch"}:
            reasons = [f"开盘综合排名未进入前{limit}"]
        result[code] = reasons or ["开盘确认不足"]
    return result


def _fetch_snapshots(codes: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    unique_codes = list(dict.fromkeys(code for code in codes if code))
    quotes: Dict[str, Dict[str, Any]] = {}
    for index in range(0, len(unique_codes), QUOTE_BATCH_SIZE):
        quotes.update(
            fetch_tencent_snapshot(
                unique_codes[index:index + QUOTE_BATCH_SIZE]
            )
        )
    return quotes


def build_confirmation(codes: List[str], asof: str, limit: int = 5) -> Dict[str, Any]:
    shortlist_result = load_shortlist(asof)
    factors = list(shortlist_result.get("shortlist", []))
    if codes:
        wanted = {candidate_pipeline.naked_code(code) for code in codes}
        factors = [
            factor for factor in factors
            if candidate_pipeline.naked_code(factor.get("code")) in wanted
        ]
    quote_codes = [
        factor["code"]
        for factor in factors
        if factor.get("code") and not factor.get("error")
    ]
    signal_ctx = read_signal_context(max_age_hours=4 * 24) or {}
    temperature = temperature_from_context(
        signal_ctx,
        event_asof=asof,
        max_age_days=4,
    )
    if temperature.get("context_fresh"):
        quote_codes.extend(
            candidate_pipeline.market_code(code)
            for code in (signal_ctx.get("lianban_ladder") or {})
        )
    quotes = _fetch_snapshots(quote_codes) if quote_codes else {}
    temperature = temperature_from_context(
        signal_ctx,
        morning_quotes=quotes,
        event_asof=asof,
        max_age_days=4,
    )

    confirmations = []
    for factor in factors:
        quote = quotes.get(factor.get("code"), {})
        confirmations.append(evaluate_open_confirmation(factor, quote))

    signals = rank_confirmations(
        factors,
        confirmations,
        limit=limit,
        temperature=temperature,
    )
    result = {
        "schema": "open_confirmation_v2",
        "asof": asof,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": "ready",
        "source_asof": shortlist_result.get("source_asof"),
        "market_temperature": temperature,
        "confirmations": confirmations,
        "signals": signals,
        "signal_count": len(signals),
    }
    atomic_write_json(_confirmation_path(asof), result)
    atomic_write_json(_confirmation_latest_path(), result)

    selected_codes = {
        candidate_pipeline.naked_code(item["code"])
        for item in signals
    }
    source_asof = str(shortlist_result.get("source_asof") or "")
    if source_asof:
        candidate_lifecycle.transition(
            source_asof,
            "open_confirmed",
            selected_codes,
            rejection_reasons=_rejection_reasons(confirmations, selected_codes, limit),
            event_asof=asof,
            details_by_code={
                candidate_pipeline.naked_code(item["code"]): {
                    "open_rank": item["open_rank"],
                    "open_score": item["open_score"],
                    "action": item["action"],
                }
                for item in signals
            },
        )
    return result


def format_report(result: Dict[str, Any]) -> str:
    if not result["confirmations"]:
        return "09:35 开盘确认：无竞价候选"
    lines = [f"## 09:35 开盘确认 | {result['asof']}"]
    for item in result["confirmations"]:
        lines.append(
            f"- {item['name']}({item['code']}): {item['action']} "
            f"现价={item.get('price')} 涨幅={item.get('change_pct')}% "
            f"竞价={item.get('auction_gap_pct')}% | {'；'.join(item['reasons'])}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="A股09:35开盘确认")
    parser.add_argument("--codes", help="逗号分隔，带市场前缀，如 sh600011,sz002156")
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--limit", type=int, default=5, help="开盘确认最终保留数量")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else []
    try:
        result = build_confirmation(codes, args.asof, limit=args.limit)
    except DataSourceError as exc:
        result = {
            "schema": "open_confirmation_v1",
            "asof": args.asof,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "status": "insufficient_data",
            "error": str(exc),
            "confirmations": [],
            "signals": [],
            "signal_count": 0,
        }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_report(result))


if __name__ == "__main__":
    main()
