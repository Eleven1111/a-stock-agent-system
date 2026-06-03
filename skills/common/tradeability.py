"""
可成交性检查 — 涨跌停 / 一字板 / 停牌判定
==========================================
打板选手核心关切：能不能打进、是否封死一字板、是否停牌。
一只封死的一字涨停板，分数再高也买不进——给方向性建议前必须先过这一关。

A股涨跌停规则:
- ST / *ST          → ±5%
- 创业板(300/301)   → ±20%
- 科创板(688)       → ±20%
- 北交所(8x/4x/920) → ±30%（按当前规则，无价格笼子时实际无涨跌停，这里保守按 30%）
- 其它主板          → ±10%

涨跌停价 = 昨收 × (1 ± pct%)，四舍五入到分（交易所采用 round half up）。
"""

import math
from typing import Dict, Any


def limit_pct(code: str, name: str = "") -> float:
    """根据代码/名称推断涨跌停幅度（百分比）。"""
    name = (name or "").upper()
    if "ST" in name:
        return 5.0
    if code.startswith(("300", "301", "688")):
        return 20.0
    if code.startswith(("8", "4", "920")):
        return 30.0
    return 10.0


def round_limit(prev_close: float, pct: float, up: bool = True) -> float:
    """涨/跌停价：昨收 × (1 ± pct%)，round half up 到分。"""
    raw = prev_close * (1 + pct / 100.0) if up else prev_close * (1 - pct / 100.0)
    return math.floor(raw * 100 + 0.5) / 100.0


def assess_tradeability(quote: Dict[str, Any], code: str, name: str = "") -> Dict[str, Any]:
    """
    判定可成交性。
    quote 需含: price, prev_close（必需）; open/high/low/volume（可选，用于一字板/停牌判断）。
    返回 tradeable ∈ {True, False, "risky"} + status + reason + 涨跌停价。
    """
    price = quote.get("price")
    prev = quote.get("prev_close")

    if price is None or prev is None or prev == 0:
        return {"tradeable": False, "status": "halted",
                "reason": "行情缺失/疑似停牌"}

    vol = quote.get("volume")
    if vol is not None and vol == 0:
        return {"tradeable": False, "status": "halted",
                "reason": "成交量为0，疑似停牌"}

    pct = limit_pct(code, name)
    up = round_limit(prev, pct, up=True)
    down = round_limit(prev, pct, up=False)

    o, hi, lo = quote.get("open"), quote.get("high"), quote.get("low")

    # 封涨停
    if price >= up - 0.005:
        is_yiziban = (
            o is not None and lo is not None
            and abs(o - up) < 0.01 and abs(lo - up) < 0.01
        )
        if is_yiziban:
            return {"tradeable": False, "status": "limit_up_sealed",
                    "reason": f"一字涨停板(￥{up})，全天封死，无法打进",
                    "limit_up": up, "limit_down": down}
        return {"tradeable": "risky", "status": "limit_up",
                "reason": f"封涨停(￥{up})，打板需排队，不保证成交",
                "limit_up": up, "limit_down": down}

    # 封跌停
    if price <= down + 0.005:
        return {"tradeable": "risky", "status": "limit_down",
                "reason": f"封跌停(￥{down})，买入无意义，卖出需排队",
                "limit_up": up, "limit_down": down}

    return {"tradeable": True, "status": "normal",
            "reason": "可正常成交",
            "limit_up": up, "limit_down": down}
