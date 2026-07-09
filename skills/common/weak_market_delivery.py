"""Weak-market delivery gate shared by D0, auction, and open confirmation.

The gate decides whether a candidate is safe to publish as a deliverable watch
target. It deliberately keeps research-only candidates out of recommendation
surfaces without discarding their analytical evidence from upstream artifacts.

顶层 ``qualified`` 是显式的通用质量 kill-switch（当前无生产写入方，仅留作未来
扩展）；游资 ``hot_money_qualified`` 是 daban lane 专用的择时/龙头门禁语义，二者
不再混用。daban lane 的游资门禁由 candidate_pipeline 的 lane 成员判定执行，不属于
本 gate 的通用质量语义——因此 daban 关闭时不会误伤 trend lane 与 balanced fill。
"""

from __future__ import annotations

from typing import Any, Mapping

from sector_taxonomy import is_broad_sector_label

SCHEMA = "weak_market_delivery_quality_v1"
REGIME_SCHEMA = "weak_market_regime_v1"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", "-"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, "", "-"):
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _nested(mapping: Mapping[str, Any], *keys: str) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def naked_code(code: Any) -> str:
    text = str(code or "").strip().lower()
    return text[2:] if text.startswith(("sh", "sz")) else text.zfill(6)


def is_main_board_10cm(code: Any, name: str = "") -> bool:
    bare = naked_code(code)
    return bare.startswith(("000", "001", "002", "003", "600", "601", "603", "605"))


def derive_weak_market_regime(market_timing: Mapping[str, Any] | None) -> dict[str, Any]:
    """Classify weak-market delivery conditions from already captured timing data."""
    market = dict(market_timing or {})
    breadth = dict(market.get("breadth") or {})
    advancers = _num(breadth.get("advancers"))
    decliners = _num(breadth.get("decliners"))
    flat = _num(breadth.get("flat"))
    total = advancers + decliners + flat
    limitups = _num(breadth.get("limitup_count"))
    limitdowns = _num(breadth.get("limitdown_count"))
    up_ratio = round(advancers / total, 4) if total > 0 else None
    lu_ld_ratio = round(limitups / limitdowns, 4) if limitdowns > 0 else None
    premium = market.get("previous_ladder_premium")
    temperature = dict(market.get("temperature") or {})
    status = str(market.get("status") or "")
    data_stale = (
        status not in {"", "ready"}
        or temperature.get("context_fresh") is False
    )

    reasons: list[str] = []
    if up_ratio is not None and up_ratio < 0.35:
        reasons.append(f"上涨家数占比过低: {up_ratio:.1%}")
    if lu_ld_ratio is not None and lu_ld_ratio < 2.0:
        reasons.append(f"涨跌停比不足: {lu_ld_ratio:.2f}")
    if isinstance(premium, (int, float)) and float(premium) <= -1.0:
        reasons.append(f"昨日涨停溢价不足: {float(premium):.2f}%")
    if data_stale and reasons:
        reasons.append("弱市择时数据缺失或过期，交付口径收缩")

    weak_regime = bool(reasons)
    extreme_weak = bool(
        weak_regime
        and (
            (up_ratio is not None and up_ratio < 0.20)
            or (limitdowns >= 50 and (lu_ld_ratio is None or lu_ld_ratio <= 1.5))
            or (limitups <= 20 and (lu_ld_ratio is None or lu_ld_ratio < 2.0))
        )
    )
    rebound_window = bool(market.get("rebound_window"))

    return {
        "schema": REGIME_SCHEMA,
        "available": total > 0 or bool(breadth),
        "weak_regime": weak_regime,
        "extreme_weak": extreme_weak,
        "rebound_window": rebound_window,
        "data_stale": bool(data_stale),
        "up_ratio": up_ratio,
        "limitup_limitdown_ratio": lu_ld_ratio,
        "limitup_count": int(limitups),
        "limitdown_count": int(limitdowns),
        "previous_ladder_premium": premium,
        "status": (
            "normal"
            if not weak_regime
            else "weak_data_stale"
            if data_stale
            else "extreme_weak"
            if extreme_weak
            else "weak"
        ),
        "reasons": reasons,
    }


def _market_timing_for(
    item: Mapping[str, Any],
    selection_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    state = dict(selection_state or {})
    if isinstance(state.get("market_timing"), Mapping):
        return dict(state["market_timing"])
    context = item.get("selection_context") or {}
    if isinstance(context, Mapping) and isinstance(context.get("market_timing"), Mapping):
        return dict(context["market_timing"])
    return {}


def _extract_candidate_fields(item: Mapping[str, Any]) -> dict[str, Any]:
    context = item.get("selection_context") or {}
    sector_context = _nested(context, "sector") if isinstance(context, Mapping) else None
    leader_context = _nested(context, "leader") if isinstance(context, Mapping) else None
    sector = (
        item.get("sector")
        or (sector_context or {}).get("name")
        or (context or {}).get("sector")
    )
    leader_rank = _int_or_none(
        item.get("leader_rank")
        or (leader_context or {}).get("rank")
        or (context or {}).get("leader_rank")
    )
    sector_rank = _int_or_none(
        item.get("sector_rank")
        or (sector_context or {}).get("rank")
        or (context or {}).get("sector_rank")
    )
    evidence_count = _int_or_none(
        item.get("sector_evidence_count")
        or (sector_context or {}).get("evidence_count")
        or (context or {}).get("sector_evidence_count")
    )
    evidence_types = (
        item.get("sector_evidence_types")
        or (sector_context or {}).get("evidence_types")
        or (context or {}).get("sector_evidence_types")
        or []
    )
    theme_confirmed = (
        item.get("sector_theme_confirmed")
        if item.get("sector_theme_confirmed") is not None
        else (sector_context or {}).get("theme_confirmed")
    )
    if theme_confirmed is None and isinstance(context, Mapping):
        theme_confirmed = context.get("sector_theme_confirmed")
    # 通用质量门禁只认顶层显式字段，不再从 leader/context 回填游资语义。
    qualified = item.get("qualified")
    # 游资门禁（tri-state，None=未知）：顶层缺 key 时读 leader_context，兼容旧
    # artifact 里 leader.qualified 承载游资语义的历史结构。
    if "hot_money_qualified" in item:
        hot_money_qualified = item.get("hot_money_qualified")
    elif isinstance(leader_context, Mapping) and "hot_money_qualified" in leader_context:
        hot_money_qualified = leader_context.get("hot_money_qualified")
    elif isinstance(leader_context, Mapping) and "qualified" in leader_context:
        hot_money_qualified = leader_context.get("qualified")
    else:
        hot_money_qualified = None
    return {
        "sector": sector,
        "sector_rank": sector_rank,
        "leader_rank": leader_rank,
        "qualified": qualified,
        "hot_money_qualified": hot_money_qualified,
        "sector_evidence_count": evidence_count,
        "sector_evidence_types": list(evidence_types)
        if isinstance(evidence_types, list)
        else [],
        "sector_theme_confirmed": bool(theme_confirmed),
    }


def _has_capacity_core_evidence(
    item: Mapping[str, Any],
    fields: Mapping[str, Any],
) -> bool:
    if item.get("capacity_core") or item.get("structural_core"):
        return True
    sector_rank = fields.get("sector_rank")
    evidence_count = fields.get("sector_evidence_count")
    leader_rank = fields.get("leader_rank")
    if (
        sector_rank is not None
        and leader_rank is not None
        and sector_rank <= 2
        and leader_rank <= 2
        and not is_broad_sector_label(fields.get("sector"))
    ):
        return True
    intelligence = _nested(item, "research_evidence", "market_intelligence") or {}
    if isinstance(intelligence, Mapping) and intelligence.get("directional_ready"):
        return True
    return False


def daban_regime_gate(
    item: Mapping[str, Any],
    *,
    lane: str,
    market_timing: Mapping[str, Any],
) -> dict[str, Any]:
    """§7b 环境门禁（config-gated，默认关闭），仅作用于打板车道。

    温度取自择时上下文，主题阶段取自候选上由主题体系盖的 ``theme_stage``；
    门禁启用时温度缺失按 fail-closed 阻断交付。
    """
    if lane != "daban":
        return {"enabled": False, "blocked": False, "reasons": []}
    try:
        from daban_adjustments import regime_gate_assessment
    except ImportError:  # pragma: no cover - flat sys.path imports
        return {"enabled": False, "blocked": False, "reasons": []}
    temperature = _nested(market_timing, "temperature", "score")
    score = float(temperature) if isinstance(temperature, (int, float)) else None
    stage_value = item.get("theme_stage")
    return regime_gate_assessment(
        temperature_score=score,
        theme_stage=str(stage_value) if stage_value else None,
    )


def assess_delivery_quality(
    item: Mapping[str, Any],
    *,
    lane: str,
    stage: str,
    selection_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return deliverable_watch, research_only, or reject for a candidate lane."""
    fields = _extract_candidate_fields(item)
    market_timing = _market_timing_for(item, selection_state)
    regime = dict(market_timing.get("weak_market") or {})
    if not regime:
        regime = derive_weak_market_regime(market_timing)

    reasons: list[str] = []
    status = "deliverable_watch"
    inherited = item.get("delivery_quality") or {}
    if isinstance(inherited, Mapping):
        inherited_status = str(inherited.get("status") or "")
        if inherited_status in {"research_only", "reject"}:
            status = inherited_status
            reasons.extend(str(reason) for reason in inherited.get("reasons") or [])
            inherited_regime = inherited.get("market_regime")
            if not regime.get("available") and isinstance(inherited_regime, Mapping):
                regime = dict(inherited_regime)
    qualified = fields.get("qualified")
    sector = fields.get("sector")
    leader_rank = fields.get("leader_rank")
    sector_rank = fields.get("sector_rank")
    evidence_count = fields.get("sector_evidence_count")

    if qualified is False:
        # 仅对显式顶层 qualified 生效——通用质量 kill-switch。daban lane 的游资门禁
        # 走 candidate_pipeline lane 成员判定，不在此处收缩 trend/balanced 车道。
        status = "reject"
        reasons.append("候选质量门槛 qualified=False")
    if leader_rank is not None and leader_rank > 150:
        status = "reject"
        reasons.append(f"板块内排名过后: leader_rank={leader_rank}")
    if sector is None and leader_rank is not None and leader_rank > 100:
        status = "reject"
        reasons.append("缺少板块且板块内排名过后")

    weak_regime = bool(regime.get("weak_regime"))
    broad_sector = is_broad_sector_label(sector)
    capacity_core = _has_capacity_core_evidence(item, fields)
    main_board = is_main_board_10cm(item.get("code"), str(item.get("name") or ""))
    hot_money_qualified = fields.get("hot_money_qualified") is True

    if status != "reject" and weak_regime:
        reasons.extend(str(reason) for reason in regime.get("reasons") or [])
        if regime.get("data_stale"):
            status = "research_only"
            reasons.append("弱市且择时/梯队证据不新鲜，不交付观察票")
        if broad_sector:
            status = "research_only"
            reasons.append(f"弱市下宽行业标签不能当主线: {sector}")
        if not main_board and not capacity_core:
            status = "research_only"
            reasons.append("弱市下 20cm/非主板候选仅保留研究观察")
        if sector and evidence_count is not None and evidence_count < 2:
            status = "research_only"
            reasons.append("弱市主题证据不足：需要至少两类共振")

        if lane == "daban":
            leader_ok = (
                hot_money_qualified
                and sector_rank is not None
                and sector_rank <= 2
                and leader_rank is not None
                and leader_rank <= 2
                and not broad_sector
                and (evidence_count is None or evidence_count >= 2)
            )
            if not leader_ok:
                status = "research_only"
                reasons.append("弱市打板仅交付窄主题前二龙头")
        else:
            if not capacity_core:
                status = "research_only"
                reasons.append("弱市趋势票缺少容量核心/结构核心证据")

        auction_score = _num(item.get("auction_score"), -1.0)
        open_score = _num(item.get("open_score"), -1.0)
        if open_score >= 0 and auction_score >= 0 and open_score <= auction_score - 10.0:
            status = "research_only"
            reasons.append("开盘确认分较竞价显著衰减")

    social = item.get("social_attention") or {}
    if (
        status == "deliverable_watch"
        and weak_regime
        and isinstance(social, Mapping)
        and str(social.get("crowding_risk") or "").lower() in {"high", "extreme"}
        and not capacity_core
    ):
        status = "research_only"
        reasons.append("弱市下社媒拥挤但缺少结构核心证据")

    gate = daban_regime_gate(item, lane=lane, market_timing=market_timing)
    if status == "deliverable_watch" and gate.get("blocked"):
        status = "research_only"
        reasons.extend(str(reason) for reason in gate.get("reasons") or [])

    return {
        "schema": SCHEMA,
        "stage": stage,
        "lane": lane,
        "status": status,
        "market_regime": regime,
        "reasons": list(dict.fromkeys(reasons)),
    }
