"""Deterministic recommendation quality gate for A-share decisions."""

from __future__ import annotations

import re
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

THESIS_INVALIDATION_TERMS = (
    "不属实",
    "未涉及",
    "无相关业务",
    "尚未形成收入",
    "未形成收入",
    "对业绩影响较小",
    "对公司业绩无重大影响",
)

REVIEW_TERMS = (
    "澄清",
    "风险提示",
    "监管问询",
    "问询函",
    "监管函",
    "更正",
)

WARNING_ONLY_TERMS = (
    "异常波动",
)

HARD_RISK_TERMS = (
    "立案调查",
    "退市风险",
    "重大诉讼",
    "资金占用",
    "违规担保",
    "财务造假",
    "减持计划",
)

#: 强制定期披露件的**标题形态**——主题恰恰是"不存在该风险"，却含硬风险关键词。
#:
#: 「（年度/半年度）非经营性资金占用及其他关联资金往来情况汇总表 / 专项说明」是每家
#: 上市公司随定期报告必交的披露件，正文通常正是"本期不存在非经营性资金占用"。子串
#: 匹配读不出这层否定：2026-08-27 实测 2026-08-07 候选池 237 只成分股有 86 只（36%）
#: 被判硬风险，抽样 25 只无一例外由这一条触发——定期报告季会把接近全市场判成 avoid。
#:
#: 锚点必须是**披露件类型**（"关联资金往来…汇总表/专项说明"），不能是「非经营性资金
#: 占用」这几个字：真的占用事件（整改报告、督促归还公告）标题里同样有这几个字，锚错
#: 了护栏就退化成"凡提到资金占用一律放行"，比不修更危险。
#: 同类补丁的仓内先例：``skills/announcement-radar/assets/taxonomy.json`` 的
#: ``polarity_rules.flip``（按标题反转极性）。
#:
#: **刻意不豁免**「上市公司控股股东、实际控制人及其他关联方资金占用情况表」
#: （2026-08-27 决定）：它虽同为必交附表，但真有占用时标题一字不变，豁免它等于放弃
#: 这一条唯一的标题级信号；实测占比 1/80，宁可留在保守侧。
PERIODIC_DISCLOSURE_TITLE_RE = re.compile(
    # 同一张必交件在各家的命名有「情况汇总表 / 情况表 / 情况的专项说明 / 专项审核
    # 报告」等多种写法（2026-08-27 实测样本里四种都出现过），锚点取"关联资金往来"
    # 这个披露件名 + 文体后缀，而不是逐字枚举全称。
    r"关联资金往来(情况)?(的)?"
    r"((汇总)?表|专项(审计|审核|核查|鉴证)?(说明|报告|意见)|鉴证报告|审核报告|审计报告)"
)

#: 上述披露件只豁免它天然会带的这一个词；同一条公告里的其它硬风险词照旧生效。
PERIODIC_DISCLOSURE_EXEMPT_TERMS = ("资金占用",)


def _split_periodic_disclosure_exemptions(
    title: str, hard_risks: list[str]
) -> tuple[list[str], list[str]]:
    """返回 (仍然生效的硬风险词, 因定期披露件而豁免的词)。

    只看 ``title``：正文引用这张表的名字很常见（"详见…情况汇总表"），拿正文做锚点会
    把真利空一起洗白。
    """
    if not hard_risks or not PERIODIC_DISCLOSURE_TITLE_RE.search(title):
        return hard_risks, []
    kept = [term for term in hard_risks if term not in PERIODIC_DISCLOSURE_EXEMPT_TERMS]
    exempted = [term for term in hard_risks if term in PERIODIC_DISCLOSURE_EXEMPT_TERMS]
    return kept, exempted


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
    thesis_invalidation_hits: list[str] = []
    review_hits: list[str] = []
    warning_only_hits: list[str] = []
    hard_risk_hits: list[str] = []
    periodic_disclosure_exempt_hits: list[str] = []
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
        invalidations = [term for term in THESIS_INVALIDATION_TERMS if term in text]
        reviews = [term for term in REVIEW_TERMS if term in text]
        warning_only = [term for term in WARNING_ONLY_TERMS if term in text]
        hard_risks = [term for term in HARD_RISK_TERMS if term in text]
        hard_risks, exempted = _split_periodic_disclosure_exemptions(title, hard_risks)
        periodic_disclosure_exempt_hits.extend(exempted)
        if invalidations:
            thesis_invalidation_hits.extend(invalidations)
            warnings.append(f"公告澄清/事实否定交易逻辑：{title or text[:40]}")
        if reviews:
            review_hits.extend(reviews)
        if reviews and not invalidations and not hard_risks:
            warnings.append(f"公告需人工复核：{title or text[:40]}")
        if warning_only:
            warning_only_hits.extend(warning_only)
            warnings.append(f"公告波动提示：{title or text[:40]}")
        if hard_risks:
            hard_risk_hits.extend(hard_risks)
            warnings.append(f"公告硬风险：{title or text[:40]}")
    return {
        "scanned": scanned,
        "thesis_invalidation_hits": sorted(set(thesis_invalidation_hits)),
        "review_hits": sorted(set(review_hits)),
        "warning_only_hits": sorted(set(warning_only_hits)),
        "clarification_hits": sorted(set(
            thesis_invalidation_hits + review_hits
        )),
        "hard_risk_hits": sorted(set(hard_risk_hits)),
        "periodic_disclosure_exempt_hits": sorted(set(periodic_disclosure_exempt_hits)),
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
            "thesis_invalidation_hits": [],
            "review_hits": [],
            "warning_only_hits": [],
            "clarification_hits": [],
            "hard_risk_hits": [],
            "periodic_disclosure_exempt_hits": [],
            "warnings": ["未完成公司公告扫描"],
        }
        blocking.append("announcement_scan_missing")
        warnings.append("未完成公司公告扫描，不得给出无条件买入结论")
    else:
        announcement_report = scan_announcement_risks(announcements, asof=asof)
        warnings.extend(announcement_report["warnings"])
        if announcement_report["thesis_invalidation_hits"]:
            blocking.append("announcement_thesis_invalidated")
        if (
            announcement_report["review_hits"]
            and not announcement_report["thesis_invalidation_hits"]
            and not announcement_report["hard_risk_hits"]
        ):
            blocking.append("announcement_review_required")
        if announcement_report["hard_risk_hits"]:
            blocking.append("announcement_hard_risk")

    hard_blocks = {
        "not_tradeable",
        "announcement_thesis_invalidated",
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


def _infer_strategy_lane(
    candidate: Mapping[str, Any],
    explicit: str | None = None,
) -> str:
    raw = str(explicit or candidate.get("strategy_id") or "")
    if raw.startswith("daban"):
        return "daban"
    if raw.startswith("trend"):
        return "trend"
    for key in ("open_selected_by", "auction_selected_by", "selected_by"):
        selected = candidate.get(key)
        if isinstance(selected, Mapping):
            if selected.get("daban"):
                return "daban"
            if selected.get("trend"):
                return "trend"
    return "default"


def _candidate_atr(candidate: Mapping[str, Any]) -> float | None:
    for key in ("atr14", "atr", "ATR14"):
        value = candidate.get(key)
        try:
            if value not in (None, "", "-") and float(value) > 0:
                return float(value)
        except (TypeError, ValueError):
            continue
    technical = candidate.get("technical") or (candidate.get("scores") or {}).get("technical")
    if isinstance(technical, Mapping):
        return _candidate_atr(technical)
    return None


def build_execution_plan(
    candidate: Mapping[str, Any],
    quality_report: Mapping[str, Any],
    asof: date | datetime | str | None = None,
    stage: str = "open",
    atr: float | None = None,
    strategy_lane: str | None = None,
) -> dict[str, Any]:
    price = float(candidate.get("price") or candidate.get("indicative_price") or 0.0)
    prev_close = float(candidate.get("prev_close") or 0.0)
    action = str(candidate.get("action") or "")
    quality_status = quality_report.get("status")
    tradeable = (candidate.get("tradeability") or {}).get("tradeable", True)
    lane = _infer_strategy_lane(candidate, strategy_lane)
    is_daban = lane == "daban"
    atr = atr if atr is not None else _candidate_atr(candidate)

    max_chase_pct = 0.07
    max_chase_price = round(prev_close * (1 + max_chase_pct), 2) if prev_close > 0 else None
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

        if atr and atr > 0:
            stop_mult = 1.2 if is_daban else 2.0
            tp1_mult = 2.0 if is_daban else 3.0
            tp2_mult = 3.5 if is_daban else 5.0
            stop_price = round(price - stop_mult * atr, 2)
            target_price = round(price + tp1_mult * atr, 2)
            target_price_2 = round(price + tp2_mult * atr, 2)
            pricing_method = "atr_adaptive"
        else:
            stop_pct = 0.03 if is_daban else 0.05
            tp1_pct = 0.05 if is_daban else 0.08
            tp2_pct = 0.08 if is_daban else 0.12
            stop_price = round(price * (1 - stop_pct), 2)
            target_price = round(price * (1 + tp1_pct), 2)
            target_price_2 = round(price * (1 + tp2_pct), 2)
            pricing_method = "fixed_pct_fallback"

        max_chase_price = max_chase_price or entry_high
        entry_high = min(entry_high, max_chase_price)
        price_range = (
            f"{entry_low:.2f}-{entry_high:.2f}"
            if entry_low <= entry_high else None
        )
    else:
        stop_price = target_price = target_price_2 = max_chase_price = None
        price_range = None
        pricing_method = "none"

    selected_by = candidate.get("open_selected_by") or candidate.get("auction_selected_by") or {}
    position_pct = 4.0 if selected_by.get("daban") or is_daban else 6.0
    if decision not in {"buy", "conditional_buy"}:
        position_pct = 0.0

    constraints = dict(quality_report.get("execution_constraints") or {})
    return {
        "schema": "a_share_execution_plan_v2",
        "decision": decision,
        "stage": stage,
        "strategy_lane": lane,
        "pricing_method": pricing_method,
        "entry_range": price_range,
        "max_chase_price": max_chase_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "target_price_2": target_price_2,
        "atr": round(atr, 3) if atr else None,
        "position_pct": position_pct,
        "horizon": "T+1" if is_daban else "T+1到T+3",
        "trigger": "价格位于买入区间、可成交性正常且公告质检通过",
        "beyond_max_chase": beyond_max_chase,
        "invalidation": [
            "超过最高追价线",
            "开盘后快速跌破竞价价且无法收回",
            "公告质检出现澄清、监管或硬风险",
        ],
        **constraints,
    }
