"""Pure portfolio concentration checks used by the central decision policy."""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Iterable, Mapping

from a_share_rules import CalendarCoverageError, previous_trading_day
from data_access_config import risk_settings


def _position_value(position: Mapping[str, Any]) -> float:
    value = position.get("market_value")
    if value is not None:
        return max(0.0, float(value))
    price = position.get("current_price", position.get("price", position.get("cost", 0)))
    shares = position.get("shares", 0)
    try:
        return max(0.0, float(price or 0) * float(shares or 0))
    except (TypeError, ValueError):
        return 0.0


def portfolio_value(portfolio: Mapping[str, Any]) -> float:
    """Return current runtime account value; static config is never consulted."""
    positions = [
        position
        for position in (portfolio.get("positions") or [])
        if isinstance(position, Mapping)
    ]
    try:
        cash = max(0.0, float(portfolio.get("cash") or 0))
    except (TypeError, ValueError):
        cash = 0.0
    return round(cash + sum(_position_value(position) for position in positions), 2)


def _position_sector(position: Mapping[str, Any]) -> str:
    return str(position.get("sector") or position.get("industry") or "").strip()


def _concentration_reasons(
    *,
    normalized_sector: str,
    unknown_sector_codes: list[str],
    stock_pct: float,
    sector_pct: float | None,
    worst_case_sector_pct: float | None,
    max_single_position_pct: float,
    max_sector_exposure_pct: float,
) -> list[str]:
    """Holdings without a sector only make the sector limit unverifiable.

    Such holdings could all belong to the candidate's sector, so the check is
    the worst case under full overlap. Blocking every candidate instead would
    reject names that cannot breach the limit even if the overlap were total.
    """
    reasons: list[str] = []
    if not normalized_sector:
        reasons.append("unknown_sector")
    if unknown_sector_codes and (
        worst_case_sector_pct is None
        or worst_case_sector_pct > float(max_sector_exposure_pct)
    ):
        reasons.append("existing_position_sector_unknown")
    if stock_pct > float(max_single_position_pct):
        reasons.append("single_position_limit")
    if sector_pct is not None and sector_pct > float(max_sector_exposure_pct):
        reasons.append("sector_exposure_limit")
    return reasons


def _projected_exposures(
    values: list[tuple[Mapping[str, Any], float]],
    *,
    total_assets: float,
    normalized_code: str,
    normalized_sector: str,
    proposed_pct: float,
) -> tuple[float, float | None, float]:
    """Return single-name, sector and unverifiable exposure after the proposal."""
    current_stock = sum(
        value
        for position, value in values
        if str(position.get("code") or "").zfill(6) == normalized_code
    )
    current_sector = sum(
        value
        for position, value in values
        if normalized_sector and _position_sector(position) == normalized_sector
    )
    unknown_sector_value = sum(
        value for position, value in values if not _position_sector(position)
    )
    return (
        current_stock / total_assets * 100 + proposed_pct,
        (
            current_sector / total_assets * 100 + proposed_pct
            if normalized_sector
            else None
        ),
        unknown_sector_value / total_assets * 100,
    )


def evaluate_new_position(
    portfolio: Mapping[str, Any],
    *,
    code: str,
    sector: str,
    proposed_position_pct: float,
    max_single_position_pct: float,
    max_sector_exposure_pct: float,
) -> dict[str, Any]:
    positions = [
        position
        for position in (portfolio.get("positions") or [])
        if isinstance(position, Mapping)
    ]
    values = [(position, _position_value(position)) for position in positions]
    total_assets = portfolio_value(portfolio)
    if total_assets <= 0:
        return {
            "schema": "portfolio_policy_v1",
            "allowed": False,
            "reasons": ["portfolio_value_unavailable"],
        }

    normalized_code = str(code).zfill(6)
    normalized_sector = str(sector or "").strip()
    unknown_sector_codes = sorted({
        str(position.get("code") or "").zfill(6)
        for position in positions
        if not _position_sector(position)
    })
    stock_pct, sector_pct, unknown_sector_pct = _projected_exposures(
        values,
        total_assets=total_assets,
        normalized_code=normalized_code,
        normalized_sector=normalized_sector,
        proposed_pct=max(0.0, float(proposed_position_pct or 0)),
    )
    worst_case_sector_pct = (
        sector_pct + unknown_sector_pct if sector_pct is not None else None
    )
    reasons = _concentration_reasons(
        normalized_sector=normalized_sector,
        unknown_sector_codes=unknown_sector_codes,
        stock_pct=stock_pct,
        sector_pct=sector_pct,
        worst_case_sector_pct=worst_case_sector_pct,
        max_single_position_pct=max_single_position_pct,
        max_sector_exposure_pct=max_sector_exposure_pct,
    )
    result = {
        "schema": "portfolio_policy_v1",
        "allowed": not reasons,
        "reasons": reasons,
        "total_assets": round(total_assets, 2),
        "projected_single_position_pct": round(stock_pct, 2),
        "projected_sector_exposure_pct": (
            round(sector_pct, 2) if sector_pct is not None else None
        ),
        "worst_case_sector_exposure_pct": (
            round(worst_case_sector_pct, 2)
            if worst_case_sector_pct is not None
            else None
        ),
        "unknown_sector_codes": unknown_sector_codes,
        "unknown_sector_exposure_pct": round(unknown_sector_pct, 2),
        "limits": {
            "max_single_position_pct": float(max_single_position_pct),
            "max_sector_exposure_pct": float(max_sector_exposure_pct),
        },
    }
    if "unknown_sector" in reasons:
        result["code"] = "UNKNOWN_SECTOR"
    return result


def evaluate_candidate(
    portfolio: Mapping[str, Any],
    candidate: Mapping[str, Any],
    proposed_position_pct: float,
) -> dict[str, Any]:
    settings = risk_settings()
    return evaluate_new_position(
        portfolio,
        code=str(candidate.get("code") or ""),
        sector=str(candidate.get("sector") or candidate.get("industry") or ""),
        proposed_position_pct=proposed_position_pct,
        max_single_position_pct=float(settings["max_single_position_pct"]),
        max_sector_exposure_pct=float(settings["max_sector_exposure_pct"]),
    )


_FACTOR_LIMITS = {
    "correlation": ("max_correlation", "correlation_limit"),
    "beta": ("max_beta", "beta_limit"),
    "style_exposure_pct": ("max_style_exposure_pct", "style_limit"),
    "adv_participation_pct": ("max_adv_participation_pct", "adv_limit"),
    "portfolio_volatility_pct": (
        "max_portfolio_volatility_pct",
        "volatility_limit",
    ),
}

_FACTOR_EVIDENCE_REASONS = {
    "risk_evidence_schema_invalid",
    "risk_evidence_source_missing",
    "risk_evidence_coverage_insufficient",
    "risk_evidence_future",
    "risk_evidence_stale",
    "risk_evidence_asof_invalid",
    "portfolio_value_unavailable",
    "risk_evidence_data_cutoff_invalid",
    "risk_evidence_data_cutoff_future",
    "risk_evidence_data_stale",
}


def _coverage_evidence(
    evidence: Mapping[str, Any], limits: Mapping[str, Any]
) -> tuple[float, list[str]]:
    reasons: list[str] = []
    try:
        coverage = float(evidence.get("coverage"))
    except (TypeError, ValueError):
        coverage = -1.0
    if not math.isfinite(coverage):
        coverage = -1.0
    try:
        min_coverage = float(limits.get("min_coverage", 1.0))
    except (TypeError, ValueError):
        min_coverage = float("nan")
    if not math.isfinite(min_coverage):
        reasons.append("min_coverage_invalid")
        min_coverage = 1.0
    if coverage < min_coverage:
        reasons.append("risk_evidence_coverage_insufficient")
    return coverage, reasons


def _freshness_evidence(
    evidence: Mapping[str, Any], limits: Mapping[str, Any], decision_asof: str
) -> tuple[int | None, Any, list[str]]:
    reasons: list[str] = []
    try:
        evidence_day = date.fromisoformat(str(evidence.get("asof"))[:10])
        decision_day = date.fromisoformat(str(decision_asof)[:10])
        age_days = (decision_day - evidence_day).days
        max_age_days = float(limits.get("max_age_days", 0))
        if not math.isfinite(max_age_days) or max_age_days < 0:
            reasons.append("max_age_days_invalid")
        elif age_days < 0:
            reasons.append("risk_evidence_future")
        elif age_days > max_age_days:
            reasons.append("risk_evidence_stale")
    except (TypeError, ValueError):
        age_days = None
        reasons.append("risk_evidence_asof_invalid")
    data_cutoff = evidence.get("data_cutoff")
    try:
        cutoff_day = date.fromisoformat(str(data_cutoff)[:10])
        decision_day = date.fromisoformat(str(decision_asof)[:10])
        required_cutoff = previous_trading_day(decision_day)
        if cutoff_day >= decision_day:
            reasons.append("risk_evidence_data_cutoff_future")
        elif cutoff_day < required_cutoff:
            reasons.append("risk_evidence_data_stale")
    except (TypeError, ValueError, CalendarCoverageError):
        reasons.append("risk_evidence_data_cutoff_invalid")
    return age_days, data_cutoff, reasons


def _factor_measurements(
    evidence: Mapping[str, Any], limits: Mapping[str, Any]
) -> tuple[dict[str, float | None], list[str]]:
    measured: dict[str, float | None] = {}
    reasons: list[str] = []
    for field, (limit_field, reason) in _FACTOR_LIMITS.items():
        try:
            value = float(evidence[field])
        except (KeyError, TypeError, ValueError):
            measured[field] = None
            reasons.append(f"{field.removesuffix('_pct')}_missing")
            continue
        if not math.isfinite(value):
            measured[field] = None
            reasons.append(f"{field.removesuffix('_pct')}_invalid")
            continue
        measured[field] = value
        try:
            limit = float(limits[limit_field])
        except (KeyError, TypeError, ValueError):
            reasons.append(f"{limit_field}_missing")
            continue
        if not math.isfinite(limit):
            reasons.append(f"{limit_field}_invalid")
        elif value > limit:
            reasons.append(reason)
    return measured, reasons


def evaluate_factor_liquidity_risk(
    evidence: Mapping[str, Any],
    *,
    limits: Mapping[str, Any],
    decision_asof: str,
) -> dict[str, Any]:
    """Fail closed on missing/stale portfolio factor and liquidity evidence."""
    reasons: list[str] = []
    if evidence.get("schema") != "portfolio_risk_evidence_v2":
        reasons.append("risk_evidence_schema_invalid")
    if not str(evidence.get("source") or "").strip():
        reasons.append("risk_evidence_source_missing")
    coverage, coverage_reasons = _coverage_evidence(evidence, limits)
    age_days, data_cutoff, freshness_reasons = _freshness_evidence(
        evidence, limits, decision_asof
    )
    measured, measurement_reasons = _factor_measurements(evidence, limits)
    reasons.extend(coverage_reasons)
    reasons.extend(freshness_reasons)
    reasons.extend(measurement_reasons)
    unique_reasons = list(dict.fromkeys(reasons))
    evidence_blocked = any(
        reason in _FACTOR_EVIDENCE_REASONS
        or reason.endswith("_missing")
        or reason.endswith("_invalid")
        for reason in unique_reasons
    )
    status = "blocked" if evidence_blocked else "rejected" if unique_reasons else "passed"
    return {
        "schema": "portfolio_factor_policy_v1",
        "status": status,
        "allowed": not unique_reasons,
        "reasons": unique_reasons,
        "asof": evidence.get("asof"),
        "source": evidence.get("source"),
        "coverage": coverage if coverage >= 0 else None,
        "age_days": age_days,
        "data_cutoff": data_cutoff,
        "measured": measured,
        "limits": dict(limits),
    }


def evaluate_complete_admission(
    portfolio: Mapping[str, Any],
    candidate: Mapping[str, Any],
    proposed_position_pct: float,
    *,
    factor_evidence: Mapping[str, Any] | None,
    decision_asof: str,
) -> dict[str, Any]:
    """Apply concentration and factor/liquidity controls as one live gate."""
    try:
        requested_position_pct = float(proposed_position_pct)
    except (TypeError, ValueError):
        requested_position_pct = float("nan")
    requested_position_valid = (
        math.isfinite(requested_position_pct) and requested_position_pct >= 0
    )
    concentration = evaluate_candidate(
        portfolio,
        candidate,
        requested_position_pct if requested_position_valid else 0.0,
    )
    settings = risk_settings()
    factor = evaluate_factor_liquidity_risk(
        factor_evidence or {},
        limits={
            "max_correlation": settings["max_correlation"],
            "max_beta": settings["max_beta"],
            "max_style_exposure_pct": settings["max_style_exposure_pct"],
            "max_adv_participation_pct": settings["max_adv_participation_pct"],
            "max_portfolio_volatility_pct": settings["max_portfolio_volatility_pct"],
            "min_coverage": settings["factor_min_coverage"],
            "max_age_days": settings["factor_max_age_days"],
        },
        decision_asof=decision_asof,
    )
    position_evidence_reasons = (
        [] if requested_position_valid else ["requested_position_invalid"]
    )
    if not factor_evidence or factor_evidence.get("proposed_position_pct") is None:
        position_evidence_reasons.append("risk_evidence_position_missing")
    else:
        try:
            evidenced_position_pct = float(factor_evidence["proposed_position_pct"])
            if not math.isfinite(evidenced_position_pct) or evidenced_position_pct < 0:
                position_evidence_reasons.append("risk_evidence_position_invalid")
            elif requested_position_valid and evidenced_position_pct < requested_position_pct:
                position_evidence_reasons.append("risk_evidence_position_understated")
        except (TypeError, ValueError):
            position_evidence_reasons.append("risk_evidence_position_invalid")
    reasons = list(dict.fromkeys([
        *(str(item) for item in concentration.get("reasons") or []),
        *(str(item) for item in factor.get("reasons") or []),
        *position_evidence_reasons,
    ]))
    concentration_reasons = [str(item) for item in concentration.get("reasons") or []]
    measured_rejection = (
        factor.get("status") == "rejected"
        or any(reason in {"single_position_limit", "sector_exposure_limit"}
               for reason in concentration_reasons)
    )
    evidence_blocked = (
        factor.get("status") == "blocked"
        or bool(position_evidence_reasons)
        or any(reason in {"unknown_sector", "existing_position_sector_unknown"}
               for reason in concentration_reasons)
    )
    status = (
        "rejected" if measured_rejection
        else "blocked" if evidence_blocked
        else "passed"
    )
    return {
        "schema": "portfolio_admission_v2",
        "status": status,
        "allowed": not reasons,
        "reasons": reasons,
        "concentration": concentration,
        "factor_liquidity": factor,
        "position_evidence_reasons": position_evidence_reasons,
    }


def _research_rejection(
    signal: Mapping[str, Any],
    reason: str,
) -> dict[str, Any]:
    return {
        "code": _research_code(signal.get("code")),
        "strategy_id": str(signal.get("strategy_id") or "").strip(),
        "signal_id": signal.get("signal_id"),
        "reason": reason,
    }


def _research_code(value: Any) -> str:
    code = str(value or "").strip().lower()
    if code.startswith(("sh", "sz")):
        code = code[2:]
    return code.zfill(6)


def _valid_research_code(code: str) -> bool:
    return len(code) == 6 and code.isdigit() and code != "000000"


def _normalize_research_signal(
    signal: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(signal, Mapping):
        return None, {
            "code": "000000",
            "strategy_id": "",
            "signal_id": None,
            "reason": "signal_invalid",
        }
    code = _research_code(signal.get("code"))
    if not _valid_research_code(code):
        return None, _research_rejection(signal, "code_missing")
    strategy_id = str(signal.get("strategy_id") or "").strip()
    if not strategy_id:
        return None, _research_rejection(signal, "strategy_id_missing")
    sector = str(
        signal.get("sector")
        or signal.get("industry")
        or signal.get("sector_id")
        or ""
    ).strip()
    if not sector:
        return None, _research_rejection(signal, "unknown_sector")
    try:
        priority = float(signal["priority"])
    except (KeyError, TypeError, ValueError):
        return None, _research_rejection(signal, "priority_invalid")
    if not math.isfinite(priority):
        return None, _research_rejection(signal, "priority_invalid")
    requested = signal.get(
        "proposed_position_pct",
        signal.get("position_pct", signal.get("allocation_pct")),
    )
    try:
        proposed_position_pct = float(requested)
    except (TypeError, ValueError):
        return None, _research_rejection(signal, "position_pct_invalid")
    if (
        not math.isfinite(proposed_position_pct)
        or proposed_position_pct <= 0
    ):
        return None, _research_rejection(signal, "position_pct_invalid")
    requested = signal.get(
        "requested_capacity",
        signal.get("requested_notional"),
    )
    try:
        requested_capacity = float(requested)
    except (TypeError, ValueError):
        return None, _research_rejection(signal, "requested_capacity_invalid")
    if not math.isfinite(requested_capacity) or requested_capacity <= 0:
        return None, _research_rejection(signal, "requested_capacity_invalid")
    return {
        **dict(signal),
        "code": code,
        "strategy_id": strategy_id,
        "sector": sector,
        "priority": priority,
        "proposed_position_pct": proposed_position_pct,
        "requested_capacity": requested_capacity,
        "signal_id": str(
            signal.get("signal_id") or f"{strategy_id}:{code}"
        ),
    }, None


def _research_priority_key(signal: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        -float(signal["priority"]),
        str(signal["strategy_id"]),
        str(signal["code"]),
        str(signal["signal_id"]),
        -float(signal["requested_capacity"]),
        -float(signal["proposed_position_pct"]),
        str(signal["sector"]),
    )


def _allocate_research_signal(
    signal: Mapping[str, Any],
    *,
    remaining_capacity: dict[str, float],
    sector_allocated: dict[str, float],
    max_single_pct: float,
    max_sector_pct: float,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    code = str(signal["code"])
    available = float(remaining_capacity.get(code, 0.0))
    requested = float(signal["requested_capacity"])
    sector = str(signal["sector"])
    remaining_sector_pct = max(
        0.0,
        max_sector_pct - sector_allocated.get(sector, 0.0),
    )
    allowed_position_pct = min(max_single_pct, remaining_sector_pct)
    if allowed_position_pct <= 0:
        return None, _research_rejection(signal, "sector_exposure_limit")
    concentration_capacity = (
        requested
        * allowed_position_pct
        / float(signal["proposed_position_pct"])
    )
    allocated_capacity = min(requested, available, concentration_capacity)
    if allocated_capacity <= 0:
        return None, _research_rejection(signal, "capacity_exhausted")
    proposed_position_pct = float(signal["proposed_position_pct"])
    allocated_pct = proposed_position_pct * allocated_capacity / requested
    sector_allocated[sector] = sector_allocated.get(sector, 0.0) + allocated_pct
    remaining_capacity[code] = available - allocated_capacity
    return {
        **signal,
        "allocated_capacity": allocated_capacity,
        "allocated_position_pct": allocated_pct,
        "limited_by": sorted(
            reason
            for reason, limited in {
                "security_capacity": allocated_capacity < requested
                and available < requested,
                "single_position_limit": allocated_capacity < requested
                and max_single_pct < proposed_position_pct,
                "sector_exposure_limit": allocated_capacity < requested
                and remaining_sector_pct < proposed_position_pct,
            }.items()
            if limited
        ),
    }, None


def _select_research_allocations(
    signals: Iterable[Mapping[str, Any]],
    *,
    security_capacity: Mapping[str, float],
    max_single_pct: float,
    max_sector_pct: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = sorted((dict(signal) for signal in signals), key=_research_priority_key)
    winners: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    for signal in ordered:
        if signal["code"] in seen_codes:
            rejections.append(_research_rejection(signal, "duplicate_security"))
            continue
        seen_codes.add(signal["code"])
        winners.append(signal)

    allocations: list[dict[str, Any]] = []
    sector_allocated: dict[str, float] = {}
    remaining_capacity = dict(security_capacity)
    for signal in winners:
        allocation, rejection = _allocate_research_signal(
            signal,
            remaining_capacity=remaining_capacity,
            sector_allocated=sector_allocated,
            max_single_pct=max_single_pct,
            max_sector_pct=max_sector_pct,
        )
        if allocation is not None:
            allocations.append(allocation)
        if rejection is not None:
            rejections.append(rejection)
    return allocations, rejections


def _normalize_security_capacity(
    security_capacity: Mapping[str, Any],
) -> dict[str, float]:
    if not isinstance(security_capacity, Mapping):
        raise ValueError("security_capacity must be a code-to-capacity mapping")
    normalized: dict[str, float] = {}
    for raw_code, raw_capacity in security_capacity.items():
        code = _research_code(raw_code)
        if not _valid_research_code(code):
            raise ValueError("security_capacity contains an invalid code")
        if code in normalized:
            raise ValueError("security_capacity contains duplicate normalized code")
        try:
            capacity = float(raw_capacity)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"security_capacity for {code} must be numeric"
            ) from exc
        if not math.isfinite(capacity) or capacity < 0:
            raise ValueError(
                f"security_capacity for {code} must be non-negative and finite"
            )
        normalized[code] = capacity
    return normalized


def _normalize_research_limits(
    max_single_pct: float,
    max_sector_pct: float,
) -> tuple[float, float]:
    try:
        single_limit = float(max_single_pct)
        sector_limit = float(max_sector_pct)
    except (TypeError, ValueError) as exc:
        raise ValueError("portfolio research limits must be numeric") from exc
    if not math.isfinite(single_limit) or not math.isfinite(sector_limit) or single_limit <= 0 or sector_limit <= 0:
        raise ValueError("portfolio research limits must be positive and finite")
    return single_limit, sector_limit


def _normalize_research_signals(
    signals: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    normalized: list[dict[str, Any]] = []
    input_rejections: list[dict[str, Any]] = []
    for signal in signals:
        item, rejection = _normalize_research_signal(signal)
        if item is not None:
            normalized.append(item)
        if rejection is not None:
            input_rejections.append(rejection)
    normalized.sort(key=_research_priority_key)
    return normalized, input_rejections


def _standalone_research_codes(
    signals: Iterable[Mapping[str, Any]],
    *,
    security_capacity: Mapping[str, float],
    max_single_pct: float,
    max_sector_pct: float,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, set[str]]]:
    by_strategy: dict[str, list[dict[str, Any]]] = {}
    for signal in signals:
        by_strategy.setdefault(signal["strategy_id"], []).append(dict(signal))
    standalone_codes: dict[str, set[str]] = {}
    for strategy_id in sorted(by_strategy):
        selected, _ = _select_research_allocations(
            by_strategy[strategy_id],
            security_capacity=security_capacity,
            max_single_pct=max_single_pct,
            max_sector_pct=max_sector_pct,
        )
        standalone_codes[strategy_id] = {str(signal["code"]) for signal in selected}
    return by_strategy, standalone_codes


def _format_research_allocations(
    selected: Iterable[Mapping[str, Any]],
    normalized: Iterable[Mapping[str, Any]],
    standalone_codes: Mapping[str, set[str]],
) -> list[dict[str, Any]]:
    strategies_by_code: dict[str, set[str]] = {}
    for signal in normalized:
        strategies_by_code.setdefault(signal["code"], set()).add(signal["strategy_id"])
    allocations: list[dict[str, Any]] = []
    for signal in selected:
        code = str(signal["code"])
        contributing = sorted(strategies_by_code[code])
        standalone = [strategy_id for strategy_id in contributing if code in standalone_codes.get(strategy_id, set())]
        allocations.append(
            {
                "code": code,
                "sector": signal["sector"],
                "strategy_id": signal["strategy_id"],
                "signal_id": signal["signal_id"],
                "priority": signal["priority"],
                "requested_capacity": signal["requested_capacity"],
                "allocated_capacity": signal["allocated_capacity"],
                "proposed_position_pct": signal["proposed_position_pct"],
                "allocated_position_pct": signal["allocated_position_pct"],
                "limited_by": signal["limited_by"],
                "attribution": {
                    "primary_strategy_id": signal["strategy_id"],
                    "contributing_strategy_ids": contributing,
                    "standalone_strategy_ids": standalone,
                    "incremental_strategy_id": signal["strategy_id"],
                },
            }
        )
    return allocations


def _research_shared_capacity(
    allocations: Iterable[Mapping[str, Any]],
    capacity_by_code: Mapping[str, float],
    *,
    single_limit: float,
    sector_limit: float,
) -> dict[str, Any]:
    allocation_list = list(allocations)
    sector_allocated: dict[str, float] = {}
    allocated_by_code = {code: 0.0 for code in capacity_by_code}
    for allocation in allocation_list:
        sector = str(allocation["sector"])
        sector_allocated[sector] = sector_allocated.get(sector, 0.0) + float(allocation["allocated_position_pct"])
        allocated_by_code[allocation["code"]] = float(allocation["allocated_capacity"])
    remaining_by_code = {code: capacity_by_code[code] - allocated_by_code[code] for code in capacity_by_code}
    return {
        "initial_by_code": {code: capacity_by_code[code] for code in sorted(capacity_by_code)},
        "allocated_by_code": {code: allocated_by_code[code] for code in sorted(allocated_by_code)},
        "remaining_by_code": {code: remaining_by_code[code] for code in sorted(remaining_by_code)},
        "allocated_position_pct": round(
            sum(float(allocation["allocated_position_pct"]) for allocation in allocation_list),
            4,
        ),
        "sector_allocated_pct": {sector: round(sector_allocated[sector], 4) for sector in sorted(sector_allocated)},
        "max_single_pct": single_limit,
        "max_sector_pct": sector_limit,
    }


def coordinate_research_allocations(
    signals: Iterable[Mapping[str, Any]],
    *,
    security_capacity: Mapping[str, float],
    max_single_pct: float,
    max_sector_pct: float,
) -> dict[str, Any]:
    """Coordinate research signals without changing any live admission rule.

    Signals first compete inside their own strategy (the standalone view), then
    compete for per-security shared capacity (the incremental view). A security
    is allocated once while every proposing strategy remains attributable.
    """
    capacity_by_code = _normalize_security_capacity(security_capacity)
    single_limit, sector_limit = _normalize_research_limits(
        max_single_pct,
        max_sector_pct,
    )
    normalized, input_rejections = _normalize_research_signals(signals)
    by_strategy, standalone_codes = _standalone_research_codes(
        normalized,
        security_capacity=capacity_by_code,
        max_single_pct=single_limit,
        max_sector_pct=sector_limit,
    )
    selected, coordination_rejections = _select_research_allocations(
        normalized,
        security_capacity=capacity_by_code,
        max_single_pct=single_limit,
        max_sector_pct=sector_limit,
    )
    allocations = _format_research_allocations(
        selected,
        normalized,
        standalone_codes,
    )
    rejections = sorted(
        [*input_rejections, *coordination_rejections],
        key=lambda item: (
            str(item.get("reason") or ""),
            str(item.get("strategy_id") or ""),
            str(item.get("code") or ""),
            str(item.get("signal_id") or ""),
        ),
    )
    standalone_by_strategy = {
        strategy_id: len(standalone_codes[strategy_id]) for strategy_id in sorted(standalone_codes)
    }
    incremental_by_strategy = {
        strategy_id: sum(allocation["strategy_id"] == strategy_id for allocation in allocations)
        for strategy_id in sorted(by_strategy)
    }
    return {
        "schema": "portfolio_research_coordination_v1",
        "allocations": allocations,
        "rejections": rejections,
        "shared_capacity": _research_shared_capacity(
            allocations,
            capacity_by_code,
            single_limit=single_limit,
            sector_limit=sector_limit,
        ),
        "standalone_count": sum(standalone_by_strategy.values()),
        "incremental_count": len(allocations),
        "standalone_by_strategy": standalone_by_strategy,
        "incremental_by_strategy": incremental_by_strategy,
    }
