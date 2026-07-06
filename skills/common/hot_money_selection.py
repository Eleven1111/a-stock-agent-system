"""Research-only hot-money timing, mainline-sector, and leader selection.

The module is deliberately pure: callers provide already captured D0/D1 data,
so selection cannot add network dependencies or bypass snapshot lineage.
"""

from __future__ import annotations

from statistics import mean
from typing import Any, Mapping, Sequence

from crowding_fragility import build_market_crowding_fragility, sector_crowding_fragility
from market_temperature import classify_market_state, temperature_from_context
from sector_taxonomy import resolve_sector
from social_attention import theme_attention_evidence
from tradeability import limit_pct
from weak_market_delivery import derive_weak_market_regime


SCHEMA = "hot_money_selection_state_v1"
DABAN_STRATEGY_ID = "daban:mainline_leader_confirm"
TREND_STRATEGY_ID = "trend_pullback"

DEFAULT_CONFIG: dict[str, Any] = {
    "research_only": False,
    "min_quote_count": 500,
    "min_sector_coverage": 0.20,
    "mainline_top_n": 2,
    "leader_top_n": 2,
    "min_sector_limitups": 3,
    "min_sector_evidence_types_weak": 2,
    "sector_flow_confirm_yi": 5.0,
    "sector_weights": {
        "limitup_count": 0.45,
        "amount": 0.20,
        "top10_change": 0.25,
        "attention": 0.10,
    },
}


def _config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_CONFIG)
    supplied = dict(config or {})
    merged.update({key: value for key, value in supplied.items() if key != "sector_weights"})
    merged["sector_weights"] = {
        **DEFAULT_CONFIG["sector_weights"],
        **dict(supplied.get("sector_weights") or {}),
    }
    return merged


def _code(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith(("sh", "sz")):
        text = text[2:]
    return text.zfill(6)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", "-"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _percentiles(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, float]:
    ordered = sorted(
        ((str(row.get("sector") or ""), _num(row.get(key))) for row in rows),
        key=lambda pair: pair[1],
    )
    if not ordered:
        return {}
    denominator = max(1, len(ordered) - 1)
    output: dict[str, float] = {}
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        percentile = ((index + end - 1) / 2) / denominator
        for sector, _value in ordered[index:end]:
            output[sector] = percentile
        index = end
    return output


def _stock_sector(
    quote: Mapping[str, Any],
    signal_context: Mapping[str, Any] | None,
) -> str:
    code = _code(quote.get("code"))
    context = signal_context or {}
    ladder = (context.get("lianban_ladder") or {}).get(code) or {}
    social = context.get("social_attention") or {}
    attention = (
        (social.get("stocks") or social.get("records") or {}).get(code) or {}
    )
    sector, _source = resolve_sector(quote, ladder=ladder, social=attention)
    return sector


def _sector_flow_yi(sector: str, context: Mapping[str, Any]) -> float | None:
    flows = context.get("sector_flows") or {}
    if not isinstance(flows, Mapping) or sector not in flows:
        return None
    value = flows.get(sector)
    if isinstance(value, Mapping):
        value = value.get("main_net_yi")
    return _num(value)


def build_market_timing(
    quotes: Sequence[Mapping[str, Any]],
    signal_context: Mapping[str, Any] | None,
    *,
    event_asof: str,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive D0 breadth and validate the hot-money timing clock."""
    cfg = _config(config)
    context = dict(signal_context or {})
    observed = [
        row for row in quotes
        if _num(row.get("price")) > 0 and row.get("change_pct") not in (None, "", "-")
    ]
    advancers = sum(_num(row.get("change_pct")) > 0 for row in observed)
    decliners = sum(_num(row.get("change_pct")) < 0 for row in observed)
    limitups = 0
    limitdowns = 0
    for row in observed:
        threshold = limit_pct(_code(row.get("code")), str(row.get("name") or ""))
        change = _num(row.get("change_pct"))
        if change >= threshold - 0.2:
            limitups += 1
        if change <= -threshold + 0.2:
            limitdowns += 1

    previous_codes = {
        _code(code)
        for code, entry in (context.get("prev_lianban_ladder") or {}).items()
        if isinstance(entry, Mapping)
    }
    premium_values = [
        _num(row.get("change_pct"))
        for row in observed
        if _code(row.get("code")) in previous_codes
    ]
    temperature = temperature_from_context(context, event_asof=event_asof, max_age_days=0)
    reasons: list[str] = []
    context_asof = str(context.get("ladder_asof") or "")
    if not temperature.get("context_fresh"):
        reasons.extend(str(note) for note in temperature.get("notes") or [])
    if context_asof and context_asof != event_asof:
        reasons.append(f"梯队日期与事件日不一致或已过期: {context_asof} != {event_asof}")
    if len(observed) < int(cfg["min_quote_count"]):
        reasons.append(
            f"全市场有效行情不足: {len(observed)} < {int(cfg['min_quote_count'])}"
        )
    if not temperature.get("allow_new_daban"):
        reasons.append(f"市场温度{temperature.get('tier')}不允许新开打板仓")

    ready = not reasons
    result = {
        "status": "ready" if ready else "insufficient_data",
        "event_asof": event_asof,
        "context_asof": context_asof or None,
        "daban_ready": ready,
        "quote_count": len(observed),
        "breadth": {
            "advancers": advancers,
            "decliners": decliners,
            "flat": len(observed) - advancers - decliners,
            "limitup_count": limitups,
            "limitdown_count": limitdowns,
        },
        "previous_ladder_premium": (
            round(mean(premium_values), 4) if premium_values else None
        ),
        "temperature": temperature,
        "reasons": reasons,
    }
    result["weak_market"] = derive_weak_market_regime(result)
    return result


def build_sector_leadership(
    quotes: Sequence[Mapping[str, Any]],
    signal_context: Mapping[str, Any] | None,
    market_timing: Mapping[str, Any],
    *,
    previous_snapshot: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Rank sectors cross-sectionally and expose persistence as research evidence."""
    cfg = _config(config)
    context = dict(signal_context or {})
    valid = [row for row in quotes if _num(row.get("price")) > 0]
    stock_sectors = {
        _code(row.get("code")): sector
        for row in valid
        if (sector := _stock_sector(row, context))
    }
    sector_rows: dict[str, list[Mapping[str, Any]]] = {}
    for row in valid:
        sector = stock_sectors.get(_code(row.get("code")))
        if sector:
            sector_rows.setdefault(sector, []).append(row)

    declared_limitups = dict(context.get("sector_limitups") or {})
    social = context.get("social_attention") or {}
    attention_records = social.get("stocks") or social.get("records") or {}
    attention_themes = social.get("themes") or {}
    rows: list[dict[str, Any]] = []
    for sector, members in sector_rows.items():
        calculated_limitups = 0
        for member in members:
            threshold = limit_pct(_code(member.get("code")), str(member.get("name") or ""))
            if _num(member.get("change_pct")) >= threshold - 0.2:
                calculated_limitups += 1
        top_changes = sorted(
            (_num(member.get("change_pct")) for member in members), reverse=True
        )[:10]
        theme_evidence = theme_attention_evidence(sector, context)
        theme_attention = attention_themes.get(sector) or {}
        attention = _num(theme_evidence.get("attention_score"))
        if attention <= 0:
            attention = _num(theme_attention.get("attention_score"))
        if attention <= 0:
            attention = sum(
                _num(record.get("attention_score") or record.get("score"))
                for code, record in attention_records.items()
                if isinstance(record, Mapping)
                and (
                    str(record.get("sector") or record.get("industry") or "").strip() == sector
                    or stock_sectors.get(_code(code)) == sector
                )
            )
        limitup_count = max(
            calculated_limitups,
            int(declared_limitups.get(sector) or 0),
        )
        flow_yi = _sector_flow_yi(sector, context)
        evidence_types: list[str] = []
        if limitup_count >= int(cfg["min_sector_limitups"]):
            evidence_types.append("limitup_cluster")
        if theme_evidence.get("confirmed"):
            evidence_types.append("social_theme")
        if flow_yi is not None and flow_yi >= float(cfg["sector_flow_confirm_yi"]):
            evidence_types.append("sector_flow")
        sector_cf = sector_crowding_fragility(members)
        rows.append({
            "sector": sector,
            "stock_count": len(members),
            "limitup_count": limitup_count,
            "amount": round(sum(_num(member.get("amount")) for member in members), 2),
            "top10_change": round(mean(top_changes), 4) if top_changes else 0.0,
            "attention": round(attention, 4),
            "theme_confirmed": bool(theme_evidence.get("confirmed")),
            "theme_confirmed_stock_count": int(
                theme_evidence.get("confirmed_stock_count") or 0
            ),
            "theme_stock_count": int(theme_evidence.get("stock_count") or 0),
            "theme_attention_score": theme_evidence.get("attention_score"),
            "sector_flow_yi": flow_yi,
            "evidence_types": evidence_types,
            "evidence_count": len(evidence_types),
            "crowding_score": sector_cf.get("crowding_score"),
            "fragility_score": sector_cf.get("fragility_score"),
        })

    percentiles = {
        key: _percentiles(rows, key)
        for key in ("limitup_count", "amount", "top10_change", "attention")
    }
    weights = cfg["sector_weights"]
    for row in rows:
        sector = row["sector"]
        row["score"] = round(100 * sum(
            _num(weights.get(key)) * percentiles[key].get(sector, 0.0)
            for key in percentiles
        ), 2)
    rows.sort(key=lambda row: (-_num(row.get("score")), row["sector"]))

    previous_top = {
        str(row.get("sector") or "")
        for row in (previous_snapshot or {}).get("sectors") or []
        if int(row.get("rank") or 10_000) <= int(cfg["mainline_top_n"])
        and row.get("qualified_for_daban")
    }
    timing_ready = bool(market_timing.get("daban_ready"))
    coverage = round(len(stock_sectors) / len(valid), 4) if valid else 0.0
    coverage_ready = coverage >= float(cfg["min_sector_coverage"])
    weak_market = bool((market_timing.get("weak_market") or {}).get("weak_regime"))
    min_evidence_count = (
        int(cfg["min_sector_evidence_types_weak"]) if weak_market else 1
    )
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
        in_current_top = rank <= int(cfg["mainline_top_n"])
        was_top = row["sector"] in previous_top
        row["state"] = (
            "confirmed" if in_current_top and was_top
            else "emerging" if in_current_top
            else "weakening" if was_top
            else "neutral"
        )
        row["qualified_for_daban"] = bool(
            timing_ready
            and coverage_ready
            and in_current_top
            and row["limitup_count"] >= int(cfg["min_sector_limitups"])
            and row["evidence_count"] >= min_evidence_count
        )

    qualified = any(row["qualified_for_daban"] for row in rows)
    reasons: list[str] = []
    if not timing_ready:
        reasons.append("市场择时证据未通过")
    if not coverage_ready:
        reasons.append(
            f"板块映射覆盖不足: {coverage:.1%} < {float(cfg['min_sector_coverage']):.1%}"
        )
    if timing_ready and coverage_ready and not qualified:
        if weak_market:
            reasons.append("弱市没有板块同时满足主线排名、涨停集群和多源共振门槛")
        else:
            reasons.append("没有板块同时满足主线排名和涨停集群门槛")

    crowding = build_market_crowding_fragility(
        quotes, context, market_timing,
        event_asof=str(market_timing.get("event_asof") or ""),
    )
    state_count = len(rows) or 1
    market_state = classify_market_state(
        dict(market_timing.get("temperature") or {}),
        breadth=market_timing.get("breadth"),
        crowding_score=crowding.get("crowding_score"),
        fragility_score=crowding.get("fragility_score"),
        sector_rotation={
            "weakening_ratio": round(sum(r.get("state") == "weakening" for r in rows) / state_count, 4),
            "emerging_ratio": round(sum(r.get("state") == "emerging" for r in rows) / state_count, 4),
        },
        previous_state=((previous_snapshot or {}).get("market_state") or {}).get("dominant_state"),
    )
    return {
        "schema": SCHEMA,
        "status": "ready" if timing_ready and coverage_ready and qualified else "insufficient_data",
        "research_only": bool(cfg.get("research_only", True)),
        "daban_ready": bool(timing_ready and coverage_ready and qualified),
        "market_timing": dict(market_timing),
        "sector_coverage": coverage,
        "stock_sectors": stock_sectors,
        "crowding_fragility": crowding,
        "market_state": market_state,
        "sectors": rows,
        "reasons": reasons,
        "config": {
            key: cfg[key]
            for key in (
                "min_quote_count",
                "min_sector_coverage",
                "mainline_top_n",
                "leader_top_n",
                "min_sector_limitups",
                "min_sector_evidence_types_weak",
                "sector_flow_confirm_yi",
                "sector_weights",
            )
        },
    }


def _is_limit_up(quote: Mapping[str, Any]) -> bool:
    threshold = limit_pct(_code(quote.get("code")), str(quote.get("name") or ""))
    return _num(quote.get("change_pct")) >= threshold - 0.2


def _leader_ablation(leader: Mapping[str, Any], sector_state: Mapping[str, Any]) -> dict[str, Any]:
    """日线宽度近似的龙头消融检验（报告 4.3，非时序因果，无逐笔数据）。

    真龙头能带动板块扩散——移除后板块仍有涨停集群=结构性带动；移除后板块塌（无其他
    涨停）或龙头独占成交=孤立单核（脆弱），不是结构性龙头。
    """
    sector_amount = _num(sector_state.get("amount"))
    leader_amount = _num(leader.get("amount"))
    amount_share = round(leader_amount / sector_amount, 4) if sector_amount > 0 else None
    sector_limitups = int(_num(sector_state.get("limitup_count")))
    breadth_without_leader = max(0, sector_limitups - (1 if _is_limit_up(leader) else 0))
    structural = breadth_without_leader >= 1 and (amount_share is None or amount_share < 0.6)
    return {
        "method": "daily_breadth_proxy",
        "leader_amount_share": amount_share,
        "breadth_without_leader": breadth_without_leader,
        "structural_leader": structural,
        "note": "移除龙头后板块仍有涨停集群=结构性带动；否则孤立单核(脆弱)。日线近似, 非时序因果。",
    }


def apply_leader_identity(
    candidates: Sequence[Mapping[str, Any]],
    selection_state: Mapping[str, Any] | None,
    signal_context: Mapping[str, Any] | None,
    *,
    config: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Rank candidates within sectors and mark research-qualified leaders."""
    cfg = _config(config or (selection_state or {}).get("config"))
    state = dict(selection_state or {})
    sector_by_code = dict(state.get("stock_sectors") or {})
    sector_states = {
        str(row.get("sector")): dict(row)
        for row in state.get("sectors") or []
        if row.get("sector")
    }
    context = dict(signal_context or {})
    ladder = context.get("lianban_ladder") or {}
    output = [dict(item) for item in candidates]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in output:
        code = _code(item.get("code"))
        sector = str(item.get("sector") or sector_by_code.get(code) or "").strip()
        item["sector"] = sector or None
        grouped.setdefault(sector, []).append(item)

    for sector, members in grouped.items():
        ordered = sorted(
            members,
            key=lambda item: (
                -int((ladder.get(_code(item.get("code"))) or {}).get("lianban") or 0),
                -_num(item.get("hot_money_bonus")),
                -_num(item.get("change_pct")),
                -_num(item.get("amount")),
                _code(item.get("code")),
            ),
        )
        for rank, item in enumerate(ordered, 1):
            sector_state = sector_states.get(sector, {})
            item["sector_rank"] = sector_state.get("rank")
            item["sector_state"] = sector_state.get("state")
            item["sector_evidence_types"] = list(
                sector_state.get("evidence_types") or []
            )
            item["sector_evidence_count"] = int(
                sector_state.get("evidence_count") or 0
            )
            item["sector_theme_confirmed"] = bool(
                sector_state.get("theme_confirmed")
            )
            item["sector_theme_attention_score"] = sector_state.get(
                "theme_attention_score"
            )
            item["sector_flow_yi"] = sector_state.get("sector_flow_yi")
            item["leader_rank"] = rank
            item["leader_role"] = (
                "sector_leader" if rank == 1
                else "sector_core" if rank <= int(cfg["leader_top_n"])
                else "sector_follower"
            )
            item["leader_score"] = round(max(0.0, 100.0 - (rank - 1) * 15.0), 2)
            if rank == 1:
                item["ablation"] = _leader_ablation(item, sector_state)
            item["hot_money_qualified"] = bool(
                state.get("daban_ready")
                and sector_state.get("qualified_for_daban")
                and rank <= int(cfg["leader_top_n"])
                and item.get("daban_eligible", True)
            )
            item["hot_money_gate_reasons"] = [] if item["hot_money_qualified"] else [
                reason
                for reason, failed in (
                    ("游资选股状态不可用", not state.get("daban_ready")),
                    ("未进入主线板块", not sector_state.get("qualified_for_daban")),
                    ("未进入板块龙头前列", rank > int(cfg["leader_top_n"])),
                    ("不满足打板交易板块约束", not item.get("daban_eligible", True)),
                )
                if failed
            ]
    return output


def selection_strategy_id(candidate: Mapping[str, Any], lane: str) -> str:
    """Return attribution without pretending a generic candidate is a reseal setup."""
    if lane == "daban" and candidate.get("hot_money_qualified"):
        return DABAN_STRATEGY_ID
    return TREND_STRATEGY_ID


def selection_context_for(
    candidate: Mapping[str, Any],
    selection_state: Mapping[str, Any] | None,
    *,
    window: str,
) -> dict[str, Any]:
    """Build the bounded attribution surface stored in reports and the ledger."""
    state = dict(selection_state or {})
    market = dict(state.get("market_timing") or {})
    crowding = dict(state.get("crowding_fragility") or {})
    market_state = dict(state.get("market_state") or {})
    temperature = dict(market.get("temperature") or {})
    sector_name = str(candidate.get("sector") or "")
    sector = next(
        (
            dict(row) for row in state.get("sectors") or []
            if str(row.get("sector") or "") == sector_name
        ),
        {},
    )
    return {
        "window": window,
        "selection_status": state.get("status") or "insufficient_data",
        "research_only": bool(state.get("research_only", True)),
        "market_timing": {
            "status": market.get("status"),
            "tier": temperature.get("tier"),
            "daban_ready": market.get("daban_ready", False),
            "breadth": dict(market.get("breadth") or {}),
            "previous_ladder_premium": market.get("previous_ladder_premium"),
            "crowding_score": crowding.get("crowding_score"),
            "fragility_score": crowding.get("fragility_score"),
            "crowding_signals": crowding.get("signals") or [],
            "dominant_state": market_state.get("dominant_state"),
            "market_state_label": market_state.get("dominant_label"),
            "state_risk_off": bool(market_state.get("risk_off")),
            "weak_market": dict(market.get("weak_market") or {}),
        },
        "sector": {
            "name": sector_name or None,
            "rank": sector.get("rank") or candidate.get("sector_rank"),
            "state": sector.get("state") or candidate.get("sector_state"),
            "source": candidate.get("sector_source"),
            "score": sector.get("score"),
            "qualified": bool(sector.get("qualified_for_daban")),
            "qualified_for_daban": bool(sector.get("qualified_for_daban")),
            "evidence_types": list(
                sector.get("evidence_types")
                or candidate.get("sector_evidence_types")
                or []
            ),
            "evidence_count": sector.get("evidence_count")
            if sector.get("evidence_count") is not None
            else candidate.get("sector_evidence_count"),
            "theme_confirmed": bool(
                sector.get("theme_confirmed")
                or candidate.get("sector_theme_confirmed")
            ),
            "theme_attention_score": sector.get("theme_attention_score")
            if sector.get("theme_attention_score") is not None
            else candidate.get("sector_theme_attention_score"),
            "theme_confirmed_stock_count": sector.get("theme_confirmed_stock_count"),
            "sector_flow_yi": sector.get("sector_flow_yi")
            if sector.get("sector_flow_yi") is not None
            else candidate.get("sector_flow_yi"),
            "crowding_score": sector.get("crowding_score"),
            "fragility_score": sector.get("fragility_score"),
        },
        "industry": {
            "name": candidate.get("industry"),
            "source": candidate.get("industry_source"),
        },
        "leader": {
            "rank": candidate.get("leader_rank"),
            "role": candidate.get("leader_role"),
            "score": candidate.get("leader_score"),
            "qualified": bool(candidate.get("hot_money_qualified")),
            "hot_money_qualified": bool(candidate.get("hot_money_qualified")),
            "ablation": candidate.get("ablation"),
        },
        "selection_snapshot": dict(state.get("snapshot") or {}),
    }


def advance_selection_context(
    candidate: Mapping[str, Any],
    *,
    window: str,
) -> dict[str, Any]:
    """Advance inherited D0 attribution without mutating its source evidence."""
    context = dict(candidate.get("selection_context") or {})
    context["window"] = window
    sector = dict(context.get("sector") or {})
    sector.update({
        key: value for key, value in {
            "name": candidate.get("sector"),
            "rank": candidate.get("sector_rank"),
            "state": candidate.get("sector_state"),
            "evidence_types": candidate.get("sector_evidence_types"),
            "evidence_count": candidate.get("sector_evidence_count"),
            "theme_confirmed": candidate.get("sector_theme_confirmed"),
            "theme_attention_score": candidate.get("sector_theme_attention_score"),
            "sector_flow_yi": candidate.get("sector_flow_yi"),
        }.items() if value is not None
    })
    context["sector"] = sector
    industry = dict(context.get("industry") or {})
    industry.update({
        key: value for key, value in {
            "name": candidate.get("industry"),
            "source": candidate.get("industry_source"),
        }.items() if value is not None
    })
    context["industry"] = industry
    leader = dict(context.get("leader") or {})
    leader.update({
        key: value for key, value in {
            "rank": candidate.get("leader_rank"),
            "role": candidate.get("leader_role"),
            "score": candidate.get("leader_score"),
            "qualified": candidate.get("hot_money_qualified"),
            "hot_money_qualified": candidate.get("hot_money_qualified"),
            "auction_sector_rank": candidate.get("auction_sector_rank"),
            "auction_sector_delta": candidate.get("auction_sector_delta"),
            "open_sector_rank": candidate.get("open_sector_rank"),
            "open_sector_delta": candidate.get("open_sector_delta"),
        }.items() if value is not None
    })
    context["leader"] = leader
    return context


def compact_selection_context(context: Mapping[str, Any] | None) -> dict[str, Any]:
    """Trim local lineage for bounded cron artifacts without losing attribution."""
    value = dict(context or {})
    market = value.get("market_timing") or {}
    sector = value.get("sector") or {}
    industry = value.get("industry") or {}
    leader = value.get("leader") or {}
    snapshot = value.get("selection_snapshot") or {}
    return {
        "window": value.get("window"),
        "status": value.get("selection_status"),
        "tier": market.get("tier"),
        "daban_ready": bool(market.get("daban_ready")),
        "crowding_score": market.get("crowding_score"),
        "fragility_score": market.get("fragility_score"),
        "dominant_state": market.get("dominant_state"),
        "weak_market_status": (market.get("weak_market") or {}).get("status"),
        "sector": sector.get("name"),
        "sector_source": sector.get("source"),
        "sector_rank": sector.get("rank"),
        "sector_state": sector.get("state"),
        "sector_evidence_count": sector.get("evidence_count"),
        "sector_evidence_types": list(sector.get("evidence_types") or []),
        "sector_theme_confirmed": bool(sector.get("theme_confirmed")),
        "industry": industry.get("name"),
        "industry_source": industry.get("source"),
        "leader_rank": leader.get("rank"),
        "leader_role": leader.get("role"),
        "qualified": bool(leader.get("qualified")),
        "hot_money_qualified": bool(
            leader.get("hot_money_qualified", leader.get("qualified"))
        ),
        "snapshot_id": snapshot.get("snapshot_id"),
    }
