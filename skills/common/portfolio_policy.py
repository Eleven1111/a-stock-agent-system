"""Pure portfolio concentration checks used by the central decision policy."""

from __future__ import annotations

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
    total_assets = max(
        0.0,
        float(portfolio.get("cash") or 0)
        + sum(value for _position, value in values),
    )
    if total_assets <= 0:
        return {
            "schema": "portfolio_policy_v1",
            "allowed": False,
            "reasons": ["portfolio_value_unavailable"],
        }

    normalized_code = str(code).zfill(6)
    current_stock = sum(
        value
        for position, value in values
        if str(position.get("code") or "").zfill(6) == normalized_code
    )
    current_sector = sum(
        value
        for position, value in values
        if sector and str(position.get("sector") or position.get("industry") or "") == sector
    )
    proposed_pct = max(0.0, float(proposed_position_pct or 0))
    stock_pct = current_stock / total_assets * 100 + proposed_pct
    sector_pct = current_sector / total_assets * 100 + proposed_pct if sector else proposed_pct
    reasons: list[str] = []
    if stock_pct > float(max_single_position_pct):
        reasons.append("single_position_limit")
    if sector and sector_pct > float(max_sector_exposure_pct):
        reasons.append("sector_exposure_limit")
    return {
        "schema": "portfolio_policy_v1",
        "allowed": not reasons,
        "reasons": reasons,
        "total_assets": round(total_assets, 2),
        "projected_single_position_pct": round(stock_pct, 2),
        "projected_sector_exposure_pct": round(sector_pct, 2) if sector else None,
        "limits": {
            "max_single_position_pct": float(max_single_position_pct),
            "max_sector_exposure_pct": float(max_sector_exposure_pct),
        },
    }


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
