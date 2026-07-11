"""Pure portfolio concentration checks used by the central decision policy."""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

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
        if not str(position.get("sector") or position.get("industry") or "").strip()
    })
    current_stock = sum(
        value
        for position, value in values
        if str(position.get("code") or "").zfill(6) == normalized_code
    )
    current_sector = sum(
        value
        for position, value in values
        if normalized_sector
        and str(position.get("sector") or position.get("industry") or "").strip()
        == normalized_sector
    )
    proposed_pct = max(0.0, float(proposed_position_pct or 0))
    stock_pct = current_stock / total_assets * 100 + proposed_pct
    sector_pct = (
        current_sector / total_assets * 100 + proposed_pct
        if normalized_sector
        else None
    )
    reasons: list[str] = []
    if not normalized_sector:
        reasons.append("unknown_sector")
    if unknown_sector_codes:
        reasons.append("existing_position_sector_unknown")
    if stock_pct > float(max_single_position_pct):
        reasons.append("single_position_limit")
    if sector_pct is not None and sector_pct > float(max_sector_exposure_pct):
        reasons.append("sector_exposure_limit")
    result = {
        "schema": "portfolio_policy_v1",
        "allowed": not reasons,
        "reasons": reasons,
        "total_assets": round(total_assets, 2),
        "projected_single_position_pct": round(stock_pct, 2),
        "projected_sector_exposure_pct": (
            round(sector_pct, 2) if sector_pct is not None else None
        ),
        "unknown_sector_codes": unknown_sector_codes,
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


def evaluate_factor_liquidity_risk(
    evidence: Mapping[str, Any],
    *,
    limits: Mapping[str, Any],
    decision_asof: str,
) -> dict[str, Any]:
    """Fail closed on missing/stale portfolio factor and liquidity evidence."""
    reasons: list[str] = []
    if evidence.get("schema") != "portfolio_risk_evidence_v1":
        reasons.append("risk_evidence_schema_invalid")
    if not str(evidence.get("source") or "").strip():
        reasons.append("risk_evidence_source_missing")
    try:
        coverage = float(evidence.get("coverage"))
    except (TypeError, ValueError):
        coverage = -1.0
    if coverage < float(limits.get("min_coverage", 1.0)):
        reasons.append("risk_evidence_coverage_insufficient")
    try:
        evidence_day = date.fromisoformat(str(evidence.get("asof"))[:10])
        decision_day = date.fromisoformat(str(decision_asof)[:10])
        age_days = (decision_day - evidence_day).days
        if age_days < 0:
            reasons.append("risk_evidence_future")
        elif age_days > int(limits.get("max_age_days", 0)):
            reasons.append("risk_evidence_stale")
    except ValueError:
        age_days = None
        reasons.append("risk_evidence_asof_invalid")

    measured: dict[str, float | None] = {}
    for field, (limit_field, reason) in _FACTOR_LIMITS.items():
        try:
            value = float(evidence[field])
        except (KeyError, TypeError, ValueError):
            measured[field] = None
            reasons.append(f"{field.removesuffix('_pct')}_missing")
            continue
        measured[field] = value
        try:
            limit = float(limits[limit_field])
        except (KeyError, TypeError, ValueError):
            reasons.append(f"{limit_field}_missing")
            continue
        if value > limit:
            reasons.append(reason)
    return {
        "schema": "portfolio_factor_policy_v1",
        "allowed": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "asof": evidence.get("asof"),
        "source": evidence.get("source"),
        "coverage": coverage if coverage >= 0 else None,
        "age_days": age_days,
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
    concentration = evaluate_candidate(portfolio, candidate, proposed_position_pct)
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
    reasons = list(dict.fromkeys([
        *(str(item) for item in concentration.get("reasons") or []),
        *(str(item) for item in factor.get("reasons") or []),
    ]))
    return {
        "schema": "portfolio_admission_v2",
        "allowed": not reasons,
        "reasons": reasons,
        "concentration": concentration,
        "factor_liquidity": factor,
    }
