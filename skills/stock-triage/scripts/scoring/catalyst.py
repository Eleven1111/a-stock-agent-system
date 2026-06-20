"""Pure news-catalyst scoring with tier-specific freshness decay."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional


CATALYST_TIERS = {
    "bullish": [
        (1.2, ["国家战略", "政策支持", "国产替代", "重大重组", "战略合作", "补贴",
               "纳入指数", "重大突破"]),
        (0.8, ["业绩增长", "业绩预增", "中标", "大额订单", "订单", "产能释放",
               "扩产", "回购", "增持", "涨价"]),
        (0.4, ["利好", "突破", "创新高", "获批"]),
    ],
    "bearish": [
        (1.2, ["退市", "暴雷", "立案调查", "财务造假", "制裁"]),
        (0.8, ["减持", "亏损", "诉讼", "处罚", "业绩下滑", "产能过剩", "限制"]),
        (0.4, ["利空", "回调", "破位"]),
    ],
}
T1_WEIGHT = 1.2
CLARIFICATION_RISK_TERMS = [
    "澄清",
    "不属实",
    "未涉及",
    "不存在相关",
    "无相关业务",
    "尚未形成收入",
    "未形成收入",
    "对业绩影响较小",
    "风险提示",
    "异常波动",
]


def news_age_days(date_str: str, now: Optional[datetime] = None) -> Optional[float]:
    if not date_str:
        return None
    ref = now or datetime.now()
    text = str(date_str).strip().lower()
    try:
        if "ago" in text:
            num = float(text.split()[0])
            if "minute" in text:
                return num / 1440
            if "hour" in text:
                return num / 24
            if "day" in text:
                return num
            if "week" in text:
                return num * 7
            if "month" in text:
                return num * 30
            return None
        head = text.split(",")[0].strip()
        parsed = datetime.strptime(head, "%m/%d/%Y")
        return max(0.0, (ref - parsed).total_seconds() / 86400)
    except (ValueError, IndexError):
        return None


def freshness_factor(age_days: Optional[float], slow: bool = False) -> float:
    if age_days is None:
        return 0.6
    if slow:
        if age_days <= 10:
            return 1.0
        if age_days <= 30:
            return 0.6
        if age_days <= 60:
            return 0.3
        return 0.15
    if age_days <= 3:
        return 1.0
    if age_days <= 7:
        return 0.7
    if age_days <= 30:
        return 0.4
    return 0.2


def news_catalyst_score(
    news: List[Dict],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    delta = 0.0
    signals = []
    for item in news[:5]:
        title = item.get("title", "")
        text = title + " " + item.get("snippet", "")
        age = news_age_days(item.get("date", ""), now)
        clarification = next(
            (term for term in CLARIFICATION_RISK_TERMS if term in text),
            None,
        )
        if clarification:
            fresh = freshness_factor(age, slow=False)
            contribution = -T1_WEIGHT * fresh
            delta += contribution
            age_str = f"{age:.0f}d" if age is not None else "?d"
            signals.append(
                f"⚠️ 澄清否定({clarification},{contribution:+.1f},{age_str}): {title[:24]}"
            )
            continue
        for direction, sign in (("bullish", 1), ("bearish", -1)):
            for weight, keywords in CATALYST_TIERS[direction]:
                hit = next((keyword for keyword in keywords if keyword in text), None)
                if hit:
                    fresh = freshness_factor(age, slow=(weight == T1_WEIGHT))
                    contribution = sign * weight * fresh
                    delta += contribution
                    mark = "" if sign > 0 else "⚠️ "
                    age_str = f"{age:.0f}d" if age is not None else "?d"
                    signals.append(
                        f"{mark}{hit}({contribution:+.1f},{age_str}): {title[:24]}"
                    )
                    break
    return {"delta": round(delta, 2), "signals": signals}
