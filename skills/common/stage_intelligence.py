"""Compact research intelligence shared by pre-open, auction, and open stages.

The summaries are deliberately descriptive. They never add candidates to the
execution shortlist or bypass strategy, quality, tradeability, or risk gates.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _code(value: Any) -> str:
    return str(value or "").strip()


def _compact(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in (
            "code", "name", "sector", "daban_rank", "daban_score",
            "trend_rank", "trend_score", "auction_rank", "auction_score",
            "auction_daban_score", "auction_trend_score", "auction_gap_pct",
            "open_rank", "open_score", "open_daban_score", "open_trend_score",
            "action", "decision", "board_status", "hot_money_qualified",
        )
        if item.get(key) is not None
    }


def _top(
    rows: Sequence[Mapping[str, Any]],
    score_key: str,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    ordered = sorted(
        (row for row in rows if row.get("code")),
        key=lambda row: (-_num(row.get(score_key)), _code(row.get("code"))),
    )
    return [_compact(item) for item in ordered[: max(0, int(limit))]]


def _decision_row(item: Mapping[str, Any]) -> dict[str, Any]:
    """决策建议行：决策 + 策略闸门原因，供简报渲染买卖建议。"""
    policy = dict(item.get("policy_decision") or {})
    return {
        "code": item.get("code"),
        "name": item.get("name"),
        "decision": item.get("decision") or policy.get("decision"),
        "reasons": list(policy.get("reasons") or []),
        "quality_status": (item.get("quality_report") or {}).get("status"),
        "requested_action": policy.get("requested_action"),
    }


def preopen_digest(result: Mapping[str, Any], *, limit: int = 10) -> dict[str, Any]:
    candidates = list(result.get("candidates") or [])
    return {
        "schema": "preopen_intelligence_v1",
        "research_only": True,
        "scanned_count": int(result.get("scanned_count") or 0),
        "eligible_count": int(result.get("eligible_count") or 0),
        "candidate_count": int(result.get("candidate_count") or len(candidates)),
        "auction_scan_count": int(result.get("auction_scan_count") or 0),
        "top_daban": _top(candidates, "daban_score", limit=limit),
        "top_trend": _top(candidates, "trend_score", limit=limit),
    }


def auction_digest(result: Mapping[str, Any], *, limit: int = 5) -> dict[str, Any]:
    factors = [
        row for row in (result.get("factors") or [])
        if row.get("code") and not row.get("error")
    ]
    shortlist_codes = {_code(row.get("code")) for row in result.get("shortlist") or []}
    decisions = list(result.get("preopen_decisions") or [])

    def mover(item: Mapping[str, Any]) -> dict[str, Any]:
        compact = _compact(item)
        compact.update({
            "research_only": True,
            "in_execution_shortlist": _code(item.get("code")) in shortlist_codes,
        })
        return compact

    gainers = sorted(
        factors,
        key=lambda row: (-_num(row.get("auction_gap_pct")), _code(row.get("code"))),
    )[:limit]
    decliners = sorted(
        factors,
        key=lambda row: (_num(row.get("auction_gap_pct")), _code(row.get("code"))),
    )[:limit]
    decision_by_code = {_code(row.get("code")): row for row in decisions}
    high_daban = [
        {**row, **decision_by_code.get(_code(row.get("code")), {})}
        for row in (result.get("shortlist") or [])
        if _num(row.get("daban_score")) >= 90
    ]
    return {
        "schema": "auction_intelligence_v1",
        "research_only": True,
        "score_semantics": result.get(
            "score_semantics", "heuristic_rank_score_not_probability"
        ),
        "score_label": result.get(
            "score_label", "竞价启发式排序分（0-100，非涨停概率/收益概率）"
        ),
        "score_is_probability": False,
        "full_market_factor_count": len(factors),
        "market_gainers": [mover(row) for row in gainers],
        "market_decliners": [mover(row) for row in decliners],
        "high_daban_candidates": _top(high_daban, "daban_score", limit=10),
        "decisions": [_decision_row(row) for row in decisions[:10]],
    }


def open_digest(result: Mapping[str, Any], *, limit: int = 10) -> dict[str, Any]:
    signals = list(result.get("signals") or [])
    selected_codes = {_code(row.get("code")) for row in signals}
    evaluated = list(result.get("evaluated_confirmations") or result.get("confirmations") or [])
    filtered: list[dict[str, Any]] = []
    for item in evaluated:
        if _code(item.get("code")) in selected_codes:
            continue
        high_score = max(
            _num(item.get("daban_score")),
            _num(item.get("auction_daban_score")),
            _num(item.get("open_daban_score")),
            _num(item.get("open_score")),
        )
        if high_score < 80 and item.get("action") not in {"not_buyable", "avoid"}:
            continue
        row = _compact(item)
        tradeability = item.get("tradeability") or {}
        reasons = list(item.get("rejection_reasons") or item.get("reasons") or [])
        if tradeability.get("tradeable") is False and not reasons:
            reasons = [str(tradeability.get("reason") or "不可成交")]
        row.update({
            "filter_stage": "open_confirmation",
            "filter_reasons": reasons or ["开盘综合排名或策略门禁未通过"],
            "tradeability": dict(tradeability),
            "research_only": True,
        })
        filtered.append(row)
    filtered.sort(
        key=lambda row: (
            -max(
                _num(row.get("open_score")),
                _num(row.get("auction_daban_score")),
                _num(row.get("daban_score")),
            ),
            _code(row.get("code")),
        )
    )
    return {
        "schema": "open_intelligence_v1",
        "research_only": True,
        "signals": [_compact(item) for item in signals[:limit]],
        "filtered_highlights": filtered[:limit],
    }
