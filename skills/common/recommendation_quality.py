"""Deterministic recommendation quality gate for A-share decisions."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Mapping

from a_share_rules import t1_constraint


REQUIRED_BUY_FIELDS = (
    "code",
    "name",
    "entry_price",
    "price_range",
    "stop_price",
    "target_price",
    "horizon",
    "grade",
    "confidence",
    "position_pct",
)

CLARIFICATION_TERMS = (
    "澄清",
    "不属实",
    "未涉及",
    "不存在",
    "无相关业务",
    "尚未形成收入",
    "未形成收入",
    "对业绩影响较小",
    "对公司业绩无重大影响",
    "风险提示",
    "异常波动",
)

HARD_RISK_TERMS = (
    "立案调查",
    "退市风险",
    "重大诉讼",
    "资金占用",
    "违规担保",
    "财务造假",
    "监管问询",
    "减持计划",
)


def _as_date(value: date | datetime | str | None) -> date:
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def scan_announcement_risks(
    announcements: Iterable[Mapping[str, Any]],
    asof: date | datetime | str | None = None,
    max_age_days: int = 30,
) -> dict[str, Any]:
    warnings: list[str] = []
    clarification_hits: list[str] = []
    hard_risk_hits: list[str] = []
    scanned = 0
    current = _as_date(asof)
    for item in announcements:
        item_date = str(item.get("date") or "")[:10]
        try:
            if item_date and (current - date.fromisoformat(item_date)).days > max_age_days:
                continue
        except ValueError:
            pass
        scanned += 1
        title = str(item.get("title") or "")
        body = str(item.get("text") or item.get("snippet") or "")
        text = f"{title} {body}"
        clarification = [term for term in CLARIFICATION_TERMS if term in text]
        hard_risks = [term for term in HARD_RISK_TERMS if term in text]
        if clarification:
            clarification_hits.extend(clarification)
            warnings.append(f"公告澄清/风险提示：{title or text[:40]}")
        if hard_risks:
            hard_risk_hits.extend(hard_risks)
            warnings.append(f"公告硬风险：{title or text[:40]}")
    return {
        "scanned": scanned,
        "clarification_hits": sorted(set(clarification_hits)),
        "hard_risk_hits": sorted(set(hard_risk_hits)),
        "warnings": warnings,
    }


def build_quality_report(
    recommendation: Mapping[str, Any],
    announcements: Iterable[Mapping[str, Any]] | None,
    asof: date | datetime | str | None = None,
) -> dict[str, Any]:
    action = str(recommendation.get("action") or "").lower()
    blocking: list[str] = []
    warnings: list[str] = []

    if action in {"buy", "add"}:
        missing = [field for field in REQUIRED_BUY_FIELDS if recommendation.get(field) in (None, "")]
        if missing:
            blocking.append("required_fields_missing")
            warnings.append(f"缺少推荐必填字段：{', '.join(missing)}")
        warnings.append("A股T+1：当日买入不可卖出，止损仅能从下一交易日起执行")

    tradeability = recommendation.get("tradeability") or {}
    if tradeability.get("tradeable") is False:
        blocking.append("not_tradeable")
        warnings.append(str(tradeability.get("reason") or "当前不可成交"))

    announcement_report: dict[str, Any]
    if announcements is None:
        announcement_report = {
            "scanned": 0,
            "clarification_hits": [],
            "hard_risk_hits": [],
            "warnings": ["未完成公司公告扫描"],
        }
        blocking.append("announcement_scan_missing")
        warnings.append("未完成公司公告扫描，不得给出无条件买入结论")
    else:
        announcement_report = scan_announcement_risks(announcements, asof=asof)
        warnings.extend(announcement_report["warnings"])
        if announcement_report["clarification_hits"]:
            blocking.append("announcement_clarification")
        if announcement_report["hard_risk_hits"]:
            blocking.append("announcement_hard_risk")

    hard_blocks = {
        "not_tradeable",
        "announcement_clarification",
        "announcement_hard_risk",
    }
    if hard_blocks.intersection(blocking):
        status = "rejected"
    elif blocking:
        status = "conditional"
    else:
        status = "passed"

    current = _as_date(asof)
    return {
        "schema": "recommendation_quality_v1",
        "status": status,
        "eligible_for_directional_advice": status == "passed",
        "blocking_checks": blocking,
        "risk_warnings": warnings,
        "announcement_scan": announcement_report,
        "execution_constraints": t1_constraint(current, current),
    }


def merge_market_intelligence(
    quality_report: Mapping[str, Any],
    intelligence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Attach cached chip/institution evidence and keep quality status consistent."""
    result = dict(quality_report)
    evidence = dict(intelligence or {})
    result["market_intelligence"] = evidence
    blocking = list(result.get("blocking_checks") or [])
    warnings = list(result.get("risk_warnings") or [])
    hard_risks = list(evidence.get("hard_risks") or [])
    intelligence_warnings = list(evidence.get("warnings") or [])
    if not evidence.get("available"):
        if "market_intelligence_missing" not in blocking:
            blocking.append("market_intelligence_missing")
        missing = ", ".join(evidence.get("missing_datasets") or [])
        warnings.append(
            f"筹码/机构必要数据缺失：{missing or '未生成缓存'}"
        )
    elif evidence.get("directional_ready") is not True:
        if "market_intelligence_incomplete" not in blocking:
            blocking.append("market_intelligence_incomplete")
        affected = sorted(set(
            list(evidence.get("missing_datasets") or [])
            + list(evidence.get("stale_datasets") or [])
        ))
        warnings.append(
            f"筹码/机构必要数据不完整或过期：{', '.join(affected) or '状态未知'}"
        )
    if hard_risks:
        if "market_intelligence_hard_risk" not in blocking:
            blocking.append("market_intelligence_hard_risk")
        warnings.extend(f"筹码/机构硬风险：{item}" for item in hard_risks)
    warnings.extend(f"筹码/机构提示：{item}" for item in intelligence_warnings)
    if hard_risks:
        result["status"] = "rejected"
        result["eligible_for_directional_advice"] = False
    elif {
        "market_intelligence_missing",
        "market_intelligence_incomplete",
    }.intersection(blocking):
        if result.get("status") == "passed":
            result["status"] = "conditional"
        result["eligible_for_directional_advice"] = False
    result["blocking_checks"] = blocking
    result["risk_warnings"] = list(dict.fromkeys(warnings))
    return result


def build_execution_plan(
    candidate: Mapping[str, Any],
    quality_report: Mapping[str, Any],
    asof: date | datetime | str | None = None,
    stage: str = "open",
) -> dict[str, Any]:
    price = float(candidate.get("price") or candidate.get("indicative_price") or 0.0)
    prev_close = float(candidate.get("prev_close") or 0.0)
    action = str(candidate.get("action") or "")
    quality_status = quality_report.get("status")
    tradeable = (candidate.get("tradeability") or {}).get("tradeable", True)

    max_chase_price = round(prev_close * 1.07, 2) if prev_close > 0 else None
    beyond_max_chase = bool(max_chase_price and price > max_chase_price)

    if price <= 0 or not tradeable or quality_status == "rejected":
        decision = "avoid"
    elif beyond_max_chase:
        decision = "watch"
    elif stage == "auction":
        decision = "conditional_buy" if quality_status == "passed" else "watch"
    elif action == "trend_watch" and quality_status == "passed":
        decision = "buy"
    else:
        decision = "watch"

    if price > 0:
        entry_low = round(price * 0.995, 2)
        entry_high = round(price * 1.005, 2)
        stop_price = round(price * 0.95, 2)
        target_price = round(price * 1.08, 2)
        target_price_2 = round(price * 1.12, 2)
        max_chase_price = max_chase_price or entry_high
        entry_high = min(entry_high, max_chase_price)
        price_range = (
            f"{entry_low:.2f}-{entry_high:.2f}"
            if entry_low <= entry_high else None
        )
    else:
        stop_price = target_price = target_price_2 = max_chase_price = None
        price_range = None

    selected_by = candidate.get("open_selected_by") or candidate.get("auction_selected_by") or {}
    position_pct = 4.0 if selected_by.get("daban") else 6.0
    if decision not in {"buy", "conditional_buy"}:
        position_pct = 0.0

    constraints = dict(quality_report.get("execution_constraints") or {})
    return {
        "schema": "a_share_execution_plan_v1",
        "decision": decision,
        "stage": stage,
        "entry_range": price_range,
        "max_chase_price": max_chase_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "target_price_2": target_price_2,
        "position_pct": position_pct,
        "horizon": "T+1到T+3",
        "trigger": "价格位于买入区间、可成交性正常且公告质检通过",
        "beyond_max_chase": beyond_max_chase,
        "invalidation": [
            "超过最高追价线",
            "开盘后快速跌破竞价价且无法收回",
            "公告质检出现澄清、监管或硬风险",
        ],
        **constraints,
    }
