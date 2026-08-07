"""Deterministic red-flag screen over fundamental facts (serenity P1 floor).

这不是 Serenity 深度研究的替代品，是它缺位时的地板。设计约束来自
``research_evidence._serenity_evidence`` 的消费口径：六维里只有
``financial_quality`` 与 ``risk_control`` 带一票否决权（≤2 → serenity_hard_risk），
而这两维恰好是最能被财报确定性计算的两维。

三条铁律：

1. **只出 1~3 分，永不出 4/5。** 「没筛出红旗」不等于「质量高」——确定性筛查
   没有资格给高分。红线用，绿灯不用。
2. **缺数据一律不打分**（score=None + missing 清单），绝不把缺失当合格。
3. **不产出 deep_score / rating。** 那两个字段直接进 four_dim 的排序权重，
   本模块无权触碰；产物带 ``source="deterministic_screen"`` 供上游区分三态
   （missing / screened / researched）。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

SCHEMA = "deterministic_screen_v1"
SOURCE = "deterministic_screen"

# 阈值取通行的会计红旗口径，不是回拟合出来的参数。改动需要说明依据。
THRESHOLDS = {
    "debt_ratio_severe": 0.85,      # 资不抵债边缘
    "debt_ratio_warn": 0.70,
    "receivable_to_revenue": 0.60,  # 应收占收入过高 → 回款质量存疑
    "goodwill_to_equity": 0.30,     # 商誉减值敞口
    "pledge_ratio_severe": 0.50,    # 控股股东质押比例
    "pledge_ratio_warn": 0.30,
}

# 每个维度必须齐备的字段；缺一项就不打分（fail-closed）。
REQUIRED_FIELDS = {
    "financial_quality": ("total_assets", "total_liabilities", "revenue", "net_profit"),
    "risk_control": ("equity",),
}


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _periods(facts: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = facts.get("periods")
    return [item for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []


def _series(facts: Mapping[str, Any], field: str) -> list[float]:
    """按 period 倒序取该字段的可用数值（最新在前）。"""
    ordered = sorted(_periods(facts), key=lambda item: str(item.get("period") or ""), reverse=True)
    values = [_num(item.get(field)) for item in ordered]
    return [value for value in values if value is not None]


def _latest(facts: Mapping[str, Any], field: str) -> float | None:
    values = _series(facts, field)
    return values[0] if values else None


def _missing_fields(facts: Mapping[str, Any], dimension: str) -> list[str]:
    return [
        field
        for field in REQUIRED_FIELDS[dimension]
        if _latest(facts, field) is None and _num((facts.get("metrics") or {}).get(field)) is None
    ]


def _value(facts: Mapping[str, Any], field: str) -> float | None:
    latest = _latest(facts, field)
    return latest if latest is not None else _num((facts.get("metrics") or {}).get(field))


def _score_from_flags(flags: Sequence[Mapping[str, Any]]) -> int:
    """severe 见血即 1 分，warn 累计降到 2 分，无旗封顶 3 分（永不 4/5）。"""
    if any(flag["level"] == "severe" for flag in flags):
        return 1
    return 2 if flags else 3


def screen_financial_quality(facts: Mapping[str, Any]) -> dict[str, Any]:
    missing = _missing_fields(facts, "financial_quality")
    if missing:
        return {"score": None, "flags": [], "missing": missing}
    flags: list[dict[str, Any]] = []

    assets = _value(facts, "total_assets")
    liabilities = _value(facts, "total_liabilities")
    if assets and assets > 0:
        ratio = liabilities / assets
        if ratio >= THRESHOLDS["debt_ratio_severe"]:
            flags.append({"code": "debt_ratio_severe", "level": "severe", "value": round(ratio, 4)})
        elif ratio >= THRESHOLDS["debt_ratio_warn"]:
            flags.append({"code": "debt_ratio_warn", "level": "warn", "value": round(ratio, 4)})

    profits = _series(facts, "net_profit")
    if len(profits) >= 2 and profits[0] < 0 and profits[1] < 0:
        flags.append({"code": "consecutive_net_loss", "level": "severe", "value": profits[:2]})

    cashflows = _series(facts, "operating_cash_flow")
    if len(cashflows) >= 2 and cashflows[0] < 0 and cashflows[1] < 0:
        level = "severe" if (profits and profits[0] > 0) else "warn"
        # 净利润为正而经营现金流连续为负 → 盈利质量存疑，比单纯亏损更该警惕。
        flags.append({"code": "negative_operating_cash_flow", "level": level, "value": cashflows[:2]})

    revenue = _value(facts, "revenue")
    receivable = _value(facts, "accounts_receivable")
    if receivable is not None and revenue and revenue > 0:
        ratio = receivable / revenue
        if ratio >= THRESHOLDS["receivable_to_revenue"]:
            flags.append({"code": "receivable_heavy", "level": "warn", "value": round(ratio, 4)})

    return {"score": _score_from_flags(flags), "flags": flags, "missing": []}


def screen_risk_control(
    facts: Mapping[str, Any],
    *,
    listing_flags: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    missing = _missing_fields(facts, "risk_control")
    if missing:
        return {"score": None, "flags": [], "missing": missing}
    flags: list[dict[str, Any]] = []
    listing = dict(listing_flags or {})

    if listing.get("st") is True:
        flags.append({"code": "st_flagged", "level": "severe", "value": True})
    if listing.get("delisting_risk") is True:
        flags.append({"code": "delisting_risk", "level": "severe", "value": True})

    equity = _value(facts, "equity")
    goodwill = _value(facts, "goodwill")
    if goodwill is not None and equity and equity > 0:
        ratio = goodwill / equity
        if ratio >= THRESHOLDS["goodwill_to_equity"]:
            flags.append({"code": "goodwill_heavy", "level": "warn", "value": round(ratio, 4)})

    pledge = _num(listing.get("pledge_ratio"))
    if pledge is not None:
        if pledge >= THRESHOLDS["pledge_ratio_severe"]:
            flags.append({"code": "pledge_severe", "level": "severe", "value": round(pledge, 4)})
        elif pledge >= THRESHOLDS["pledge_ratio_warn"]:
            flags.append({"code": "pledge_warn", "level": "warn", "value": round(pledge, 4)})

    inquiry = _num(listing.get("regulatory_inquiries"))
    if inquiry is not None and inquiry > 0:
        flags.append({"code": "regulatory_inquiry", "level": "warn", "value": int(inquiry)})

    return {"score": _score_from_flags(flags), "flags": flags, "missing": []}


def screen(
    facts: Mapping[str, Any],
    *,
    listing_flags: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """产出 deterministic_screen_v1。故意不含 deep_score / rating。"""
    quality = screen_financial_quality(facts)
    risk = screen_risk_control(facts, listing_flags=listing_flags)
    dimensions = {
        "financial_quality": quality["score"],
        "risk_control": risk["score"],
    }
    complete = all(value is not None for value in dimensions.values())
    return {
        "schema": SCHEMA,
        "source": SOURCE,
        "code": str(facts.get("code") or "").zfill(6),
        "name": facts.get("name"),
        "asof": facts.get("asof"),
        "facts_provider": (facts.get("source") or {}).get("provider"),
        "dimensions": dimensions,
        "complete": complete,
        "evidence": {"financial_quality": quality, "risk_control": risk},
        "hard_risk_codes": sorted(
            flag["code"]
            for item in (quality, risk)
            for flag in item["flags"]
            if flag["level"] == "severe"
        ),
    }
