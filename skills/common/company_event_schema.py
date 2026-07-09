"""Schema helpers for company event opportunity scans."""

from __future__ import annotations

from typing import Any, Mapping


SCHEMA = "company_event_opportunities_v1"

EVENT_TYPES = {
    "mna_restructuring": {
        "label": "并购重组",
        "keywords": ("并购", "重组", "吸收合并", "借壳", "重大资产重组"),
    },
    "asset_injection": {
        "label": "资产注入",
        "keywords": ("资产注入", "整体上市", "国企改革", "混改", "员工持股"),
    },
    "buyback_increase": {
        "label": "回购增持",
        "keywords": ("回购", "增持", "股份回购", "高管增持", "控股股东增持"),
    },
    "spin_off_listing": {
        "label": "分拆上市",
        "keywords": ("分拆上市", "子公司上市", "分拆至"),
    },
    "index_adjustment": {
        "label": "指数调整",
        "keywords": ("指数纳入", "纳入指数", "指数剔除", "调入", "调出"),
    },
    "unlock_reduction": {
        "label": "解禁减持",
        "keywords": ("解禁", "减持", "限售股上市", "股东减持"),
    },
    "management_change": {
        "label": "管理层变更",
        "keywords": ("董事长变更", "总经理变更", "高管变更", "辞职", "聘任"),
    },
}

ALLOWED_SUGGESTIONS = {"watch", "review", "avoid"}
DEFAULT_DOWNSIDE_BY_TYPE = {
    "mna_restructuring": "event_failure_downside_unavailable",
    "asset_injection": "event_failure_downside_unavailable",
    "buyback_increase": "buyback_support_failure_unavailable",
    "spin_off_listing": "spin_off_failure_downside_unavailable",
    "index_adjustment": "passive_flow_reversal_unavailable",
    "unlock_reduction": "supply_pressure_downside_unavailable",
    "management_change": "governance_uncertainty_downside_unavailable",
}


def event_label(event_type: str) -> str:
    return EVENT_TYPES.get(event_type, {}).get("label", event_type)


def classify_event(text: str) -> str | None:
    compact = str(text or "")
    for event_type, spec in EVENT_TYPES.items():
        if any(keyword in compact for keyword in spec["keywords"]):
            return event_type
    return None


def normalize_code(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw.startswith(("sh", "sz", "bj")):
        raw = raw[2:]
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def make_opportunity(
    *,
    code: str,
    name: str,
    event_type: str,
    evidence: list[Mapping[str, Any]],
    event_status: str = "active",
    suggestion: str = "watch",
    downside_pct: float | None = None,
    upside_pct: float | None = None,
    success_probability: float | None = None,
    risk_flags: list[str] | None = None,
    milestones: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one normalized opportunity without fabricating missing evidence."""
    normalized_suggestion = suggestion if suggestion in ALLOWED_SUGGESTIONS else "watch"
    expected_value_pct = None
    if (
        isinstance(upside_pct, (int, float))
        and isinstance(downside_pct, (int, float))
        and isinstance(success_probability, (int, float))
    ):
        expected_value_pct = round(
            float(success_probability) * float(upside_pct)
            + (1.0 - float(success_probability)) * float(downside_pct),
            4,
        )

    flags = list(risk_flags or [])
    if downside_pct is None:
        flags.append(DEFAULT_DOWNSIDE_BY_TYPE.get(event_type, "event_failure_downside_unavailable"))
    if upside_pct is None:
        flags.append("upside_evidence_unavailable")
    if success_probability is None:
        flags.append("success_probability_evidence_unavailable")

    return {
        "code": normalize_code(code),
        "name": str(name or normalize_code(code)),
        "event_type": event_type,
        "event_label": event_label(event_type),
        "event_status": event_status,
        "source_rank": "S4",
        "evidence": [dict(item) for item in evidence],
        "announced_at": _first_value(evidence, "published_at"),
        "milestones": [dict(item) for item in (milestones or [])],
        "upside_pct": upside_pct,
        "downside_pct": downside_pct,
        "success_probability": success_probability,
        "expected_value_pct": expected_value_pct,
        "time_horizon_days": None,
        "annualized_return_if_success_pct": None,
        "risk_level": "medium" if normalized_suggestion != "avoid" else "high",
        "risk_flags": sorted(set(flags)),
        "suggestion": normalized_suggestion,
        "directional_ready": False,
    }


def _first_value(items: list[Mapping[str, Any]], key: str) -> Any:
    for item in items:
        value = item.get(key)
        if value:
            return value
    return None
