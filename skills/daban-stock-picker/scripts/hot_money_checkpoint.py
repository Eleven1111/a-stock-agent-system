#!/usr/bin/env python3
"""Bounded 09:50/13:15 research checkpoints for mainline leaders.

The checkpoint reuses the 09:35 surface, fetches at most 20 Tencent quotes,
and records research observations only. It never places trades or recommends a
same-day exit for a position opened today.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from typing import Any, Mapping, Sequence


SCRIPT_DIR = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(SCRIPT_DIR, "..", "..", "common"))

from a_share_rules import add_trading_days  # noqa: E402
from a_stock_http import DataSourceError  # noqa: E402
import candidate_fsm  # noqa: E402
import candidate_lifecycle  # noqa: E402
import candidate_pipeline  # noqa: E402
import hot_money_selection  # noqa: E402
from market_adapters import fetch_tencent_snapshot  # noqa: E402
from market_snapshot import compact_ref, materialize_input_snapshot, write_snapshot  # noqa: E402
from paths import data_file  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402
from tradeability import assess_tradeability  # noqa: E402


MAX_CANDIDATES = 20
PROFILE_RULES = {
    "morning_confirm": {
        "window": "09:50",
        "stage": "morning_reconfirmed",
        "min_change_pct": 3.0,
        "min_open_return_pct": -1.0,
    },
    "afternoon_reflow": {
        "window": "13:15",
        "stage": "afternoon_reflow",
        "min_change_pct": 2.0,
        "min_open_return_pct": 0.0,
    },
}


def open_confirmation_path(asof: str) -> str:
    return data_file("daban-stock-picker", f"open_confirmation_{asof}.json")


def latest_output_path(profile: str) -> str:
    return data_file("daban-stock-picker", f"hot_money_checkpoint_{profile}_latest.json")


def load_open_confirmation(asof: str) -> dict[str, Any]:
    result = read_json(open_confirmation_path(asof), {})
    if not isinstance(result, dict) or result.get("status") != "ready":
        raise DataSourceError("open_confirmation", f"{asof} 开盘确认结果缺失或不可用")
    if str(result.get("asof") or "") != asof:
        raise DataSourceError(
            "open_confirmation",
            f"开盘确认日期不一致: source={result.get('asof')}, event={asof}",
        )
    if not isinstance(result.get("signals"), list):
        raise DataSourceError("open_confirmation", "开盘确认缺少 signals")
    return result


def fetch_quotes(codes: Sequence[str]) -> dict[str, dict[str, Any]]:
    unique = list(dict.fromkeys(code for code in codes if code))[:MAX_CANDIDATES]
    return dict(fetch_tencent_snapshot(unique)) if unique else {}


def _quote_map(quotes: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        candidate_pipeline.naked_code(code): dict(value)
        for code, value in quotes.items()
        if isinstance(value, Mapping)
    }


def _number(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", "-"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def evaluate_checkpoint(
    candidates: Sequence[Mapping[str, Any]],
    quotes: Mapping[str, Mapping[str, Any]],
    *,
    profile: str,
    asof: str,
) -> list[dict[str, Any]]:
    if profile not in PROFILE_RULES:
        raise ValueError(f"unsupported checkpoint profile: {profile}")
    rules = PROFILE_RULES[profile]
    normalized_quotes = _quote_map(quotes)
    observations: list[dict[str, Any]] = []
    for raw in candidates[:MAX_CANDIDATES]:
        item = dict(raw)
        code = candidate_pipeline.naked_code(item.get("code"))
        quote = normalized_quotes.get(code)
        reasons: list[str] = []
        tradeability: dict[str, Any]
        if not quote:
            tradeability = {"tradeable": False, "status": "missing_quote"}
            reasons.append("当前行情缺失，研究确认失败")
        else:
            tradeability = assess_tradeability(
                quote,
                code,
                str(item.get("name") or code),
            )
            if tradeability.get("tradeable") is False:
                reasons.append(str(tradeability.get("reason") or "当前不可成交"))
        strategy_id = str(item.get("strategy_id") or "")
        if strategy_id.startswith("daban") and not item.get("hot_money_qualified"):
            reasons.append("未通过主线板块与板块龙头门禁")

        price = _number((quote or {}).get("price"))
        open_price = _number((quote or {}).get("open"))
        change_pct = _number((quote or {}).get("change_pct"))
        open_return = (
            round((price / open_price - 1.0) * 100, 4)
            if price > 0 and open_price > 0
            else None
        )
        research_state = "invalidated"
        if not reasons:
            if (
                change_pct >= float(rules["min_change_pct"])
                and open_return is not None
                and open_return >= float(rules["min_open_return_pct"])
            ):
                research_state = "confirmed"
                reasons.append("涨幅与开盘承接满足研究确认阈值")
            else:
                research_state = "watch"
                reasons.append("承接强度不足，继续观察但不生成交易动作")

        earliest_sell = add_trading_days(asof, 1)
        item.update({
            "code": candidate_pipeline.market_code(code),
            "price": price or None,
            "change_pct": change_pct if quote else None,
            "return_from_open_pct": open_return,
            "tradeability": tradeability,
            "research_state": research_state,
            "reasons": reasons,
            "execution_action": "none",
            "same_day_sell_allowed": False,
            "earliest_sell_date": (
                earliest_sell.isoformat()
                if hasattr(earliest_sell, "isoformat")
                else str(earliest_sell)
            ),
        })
        observations.append(item)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in observations:
        sector = str(item.get("sector") or "").strip()
        grouped.setdefault(sector, []).append(item)
    for members in grouped.values():
        ordered = sorted(
            members,
            key=lambda item: (
                -_number(item.get("change_pct"), -100.0),
                -_number(item.get("return_from_open_pct"), -100.0),
                str(item.get("code")),
            ),
        )
        for rank, item in enumerate(ordered, 1):
            item["checkpoint_sector_rank"] = rank
            if item["research_state"] == "confirmed" and rank > 2:
                item["research_state"] = "watch"
                item["reasons"].append("板块内强度未进入前2，降级为观察")
            item["selection_context"] = hot_money_selection.advance_selection_context(
                item,
                window=str(rules["window"]),
            )
            item["selection_context"]["checkpoint"] = {
                "profile": profile,
                "sector_rank": rank,
                "research_state": item["research_state"],
                "change_pct": item.get("change_pct"),
                "return_from_open_pct": item.get("return_from_open_pct"),
            }
    return observations


def run_checkpoint(profile: str, asof: str) -> dict[str, Any]:
    if profile not in PROFILE_RULES:
        raise ValueError(f"unsupported checkpoint profile: {profile}")
    source = load_open_confirmation(asof)
    candidates = list(source.get("signals") or [])[:MAX_CANDIDATES]
    codes = [
        candidate_pipeline.market_code(item.get("code"))
        for item in candidates
        if item.get("code")
    ]
    raw_quotes = fetch_quotes(codes)
    if candidates and not raw_quotes:
        raise DataSourceError("tencent", "检查点候选行情全部缺失")
    batch_id = os.environ.get("A_STOCK_BATCH_ID") or f"a-share-{asof.replace('-', '')}"
    input_snapshot = materialize_input_snapshot(
        f"hot-money-{profile}-input",
        {
            "schema": "hot_money_checkpoint_inputs_v1",
            "profile": profile,
            "source_confirmation": {
                "asof": source.get("asof"),
                "generated_at": source.get("generated_at"),
                "input_snapshot": source.get("input_snapshot"),
            },
            "candidates": candidates,
            "quotes": raw_quotes,
        },
        trading_date=asof,
        batch_id=batch_id,
        producer=f"hot-money-{profile}",
        producer_version="hot-money-checkpoint-v1",
        source_versions={"tencent": "tencent-adapter-v2"},
    )
    payload = input_snapshot["payload"]
    observations = evaluate_checkpoint(
        list(payload.get("candidates") or []),
        dict(payload.get("quotes") or {}),
        profile=profile,
        asof=asof,
    )
    confirmed = [item for item in observations if item["research_state"] == "confirmed"]
    result = {
        "schema": "hot_money_checkpoint_v1",
        "status": "ready",
        "profile": profile,
        "window": PROFILE_RULES[profile]["window"],
        "asof": asof,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_asof": source.get("source_asof"),
        "research_only": True,
        "input_snapshot": compact_ref(input_snapshot),
        "observation_count": len(observations),
        "confirmed_count": len(confirmed),
        "observations": observations,
    }
    output_snapshot = write_snapshot(
        f"hot-money-{profile}-output",
        result,
        trading_date=asof,
        batch_id=batch_id,
        producer=f"hot-money-{profile}",
        producer_version="hot-money-checkpoint-v1",
        source_versions=input_snapshot.get("source_versions") or {},
    )
    result["output_snapshot"] = compact_ref(output_snapshot)
    atomic_write_json(latest_output_path(profile), result)

    source_asof = str(source.get("source_asof") or "")
    if source_asof:
        selected_codes = [item["code"] for item in confirmed]
        rejection_reasons = {
            item["code"]: item["reasons"]
            for item in observations
            if item["research_state"] != "confirmed"
        }
        candidate_lifecycle.transition(
            source_asof,
            str(PROFILE_RULES[profile]["stage"]),
            selected_codes,
            rejection_reasons=rejection_reasons,
            event_asof=asof,
            details_by_code={
                item["code"]: {
                    "profile": profile,
                    "research_state": item["research_state"],
                    "checkpoint_sector_rank": item.get("checkpoint_sector_rank"),
                    "change_pct": item.get("change_pct"),
                    "return_from_open_pct": item.get("return_from_open_pct"),
                    "same_day_sell_allowed": False,
                }
                for item in observations
            },
        )
        _advance_fsm_to_confirmed(asof, confirmed)
    return result


def _advance_fsm_to_confirmed(asof: str, confirmed: Sequence[Mapping[str, Any]]) -> None:
    """Route checkpoint-confirmed candidates through the FSM: candidate ->
    confirmed. This is the single trigger point for the review-gate guard
    (research_committee candidate_deep_dive verdict), per advisory/enforce
    config in candidate_selection.json. Idempotent: codes not currently at
    `candidate` are left alone (already confirmed, or never promoted) so the
    morning and afternoon checkpoints don't spam rejected events on repeat.
    Best-effort: never blocks the checkpoint output, which is the
    authoritative research-only surface."""
    config = candidate_fsm.load_fsm_config()
    for item in confirmed:
        code = str(item.get("code") or "")
        if not code:
            continue
        try:
            state = candidate_fsm.current_state(code)
            if state is not None and state.get("to_state") != "candidate":
                continue
            candidate_fsm.transition(
                code, "confirmed", "score_above_threshold", asof=asof, config=config,
            )
        except Exception:  # noqa: BLE001
            continue


def json_report(result: Mapping[str, Any]) -> dict[str, Any]:
    confirmed = [
        {
            "code": item.get("code"),
            "name": item.get("name"),
            "sector": item.get("sector"),
            "sector_rank": item.get("checkpoint_sector_rank"),
            "change_pct": item.get("change_pct"),
            "research_state": item.get("research_state"),
        }
        for item in result.get("observations") or []
        if item.get("research_state") == "confirmed"
    ]
    profile = str(result.get("profile") or "")
    window = str(result.get("window") or "")
    if confirmed:
        names = "、".join(str(item.get("name") or item.get("code")) for item in confirmed[:5])
        message = f"{window}主线龙头承接确认：{len(confirmed)}只研究确认，{names}"
    elif profile == "morning_confirm":
        message = "今日早盘无主线龙头承接信号。"
    else:
        message = "今日午后无主线龙头回流信号。"
    return {
        "status": result.get("status"),
        "profile": result.get("profile"),
        "window": result.get("window"),
        "asof": result.get("asof"),
        "message": message,
        "research_only": True,
        "observation_count": result.get("observation_count"),
        "confirmed_count": result.get("confirmed_count"),
        "confirmed": confirmed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="游资主线龙头盘中研究确认")
    parser.add_argument("--profile", choices=sorted(PROFILE_RULES), required=True)
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = run_checkpoint(args.profile, args.asof)
    except DataSourceError as exc:
        failure = {
            "schema": "hot_money_checkpoint_v1",
            "status": "insufficient_data",
            "profile": args.profile,
            "asof": args.asof,
            "error": str(exc),
            "research_only": True,
            "confirmed_count": 0,
        }
        print(json.dumps(failure, ensure_ascii=False) if args.json else failure)
        return 1
    print(json.dumps(json_report(result), ensure_ascii=False) if args.json else result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
