"""主动卖出信号检测 — 持仓期退出条件评估。

信号类型：
  1. stop_loss:       价格跌破 ATR 自适应止损位
  2. take_profit:     价格达到目标位
  3. trailing_stop:   从持仓最高点回落超过阈值
  4. sentiment_exit:  温度计退潮 + 板块连板断裂
  5. flow_reversal:   北向连续流出 + 个股主力出逃
  6. catalyst_negated: 催化事件被澄清/否定
  7. time_stop:       持仓超过持有窗口未达目标
  8. lhb_climax:      龙虎榜高潮见顶（净买突然放量3倍，issue #88）
  9. deep_research_exit: 深研评分红线（deep_score<5 必须减仓，<3 清仓）

每个信号返回 {triggered, signal_type, severity, reason, action}。
severity: critical(立即卖) / warning(减仓/关注) / info(记录)
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping


def check_stop_loss(
    current_price: float,
    stop_price: float,
) -> dict[str, Any]:
    if current_price <= 0 or stop_price <= 0:
        return {"triggered": False, "signal_type": "stop_loss"}
    triggered = current_price <= stop_price
    return {
        "triggered": triggered,
        "signal_type": "stop_loss",
        "severity": "critical" if triggered else "info",
        "reason": f"价格{current_price:.2f}已跌破止损位{stop_price:.2f}" if triggered else "",
        "action": "sell" if triggered else "hold",
    }


def check_take_profit(
    current_price: float,
    target_price: float,
    target_price_2: float | None = None,
) -> dict[str, Any]:
    if current_price <= 0 or target_price <= 0:
        return {"triggered": False, "signal_type": "take_profit"}
    hit_t2 = target_price_2 and current_price >= target_price_2
    hit_t1 = current_price >= target_price
    if hit_t2:
        return {
            "triggered": True,
            "signal_type": "take_profit",
            "severity": "critical",
            "reason": f"达到第二目标位{target_price_2:.2f}",
            "action": "sell",
        }
    if hit_t1:
        return {
            "triggered": True,
            "signal_type": "take_profit",
            "severity": "warning",
            "reason": f"达到第一目标位{target_price:.2f}",
            "action": "reduce",
        }
    return {"triggered": False, "signal_type": "take_profit"}


def check_trailing_stop(
    current_price: float,
    peak_price: float,
    trailing_pct: float = 5.0,
) -> dict[str, Any]:
    if current_price <= 0 or peak_price <= 0:
        return {"triggered": False, "signal_type": "trailing_stop"}
    drawdown_pct = (peak_price - current_price) / peak_price * 100
    triggered = drawdown_pct >= trailing_pct
    return {
        "triggered": triggered,
        "signal_type": "trailing_stop",
        "severity": "critical" if triggered else "info",
        "reason": f"从最高{peak_price:.2f}回落{drawdown_pct:.1f}%(阈值{trailing_pct}%)" if triggered else "",
        "action": "sell" if triggered else "hold",
        "drawdown_pct": round(drawdown_pct, 2),
    }


def check_sentiment_exit(
    temperature_tier: str | None,
    prev_tier: str | None = None,
    sector_lianban_broken: bool = False,
) -> dict[str, Any]:
    retreat_tiers = {
        ("加速", "修复"), ("加速", "冰点"),
        ("发酵", "冰点"), ("极热", "加速"),
        ("极热", "发酵"), ("极热", "修复"),
        ("极热", "冰点"),
    }
    tier_dropped = (prev_tier, temperature_tier) in retreat_tiers if prev_tier and temperature_tier else False
    triggered = tier_dropped or sector_lianban_broken
    parts = []
    if tier_dropped:
        parts.append(f"温度计{prev_tier}→{temperature_tier}")
    if sector_lianban_broken:
        parts.append("板块连板断裂")
    return {
        "triggered": triggered,
        "signal_type": "sentiment_exit",
        "severity": "warning" if triggered else "info",
        "reason": "；".join(parts) if triggered else "",
        "action": "reduce" if triggered else "hold",
    }


def check_flow_reversal(
    northbound_net_yi: float | None = None,
    stock_main_net_yi: float | None = None,
    consecutive_outflow_days: int = 0,
) -> dict[str, Any]:
    nb_risk = isinstance(northbound_net_yi, (int, float)) and northbound_net_yi < -30
    stock_risk = isinstance(stock_main_net_yi, (int, float)) and stock_main_net_yi < -1
    triggered = (nb_risk and consecutive_outflow_days >= 2) or (stock_risk and consecutive_outflow_days >= 2)
    parts = []
    if nb_risk:
        parts.append(f"北向净流出{abs(northbound_net_yi):.0f}亿")
    if stock_risk:
        parts.append(f"个股主力净流出{abs(stock_main_net_yi):.1f}亿")
    if consecutive_outflow_days >= 2:
        parts.append(f"连续{consecutive_outflow_days}日")
    return {
        "triggered": triggered,
        "signal_type": "flow_reversal",
        "severity": "warning" if triggered else "info",
        "reason": "；".join(parts) if triggered else "",
        "action": "reduce" if triggered else "hold",
    }


def check_catalyst_negated(
    catalyst_events: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    negation_terms = [
        "澄清", "不属实", "未涉及", "不存在相关", "无相关业务",
        "尚未形成收入", "未形成收入", "对业绩影响较小", "风险提示",
    ]
    if not catalyst_events:
        return {"triggered": False, "signal_type": "catalyst_negated"}
    hits = []
    for event in catalyst_events:
        text = str(event.get("title", "")) + " " + str(event.get("snippet", ""))
        for term in negation_terms:
            if term in text:
                hits.append(f"{term}: {str(event.get('title', ''))[:30]}")
                break
    triggered = len(hits) > 0
    return {
        "triggered": triggered,
        "signal_type": "catalyst_negated",
        "severity": "critical" if triggered else "info",
        "reason": "催化被否定: " + "; ".join(hits[:3]) if triggered else "",
        "action": "sell" if triggered else "hold",
        "negation_hits": hits,
    }


def check_time_stop(
    entry_date: date | str | None,
    horizon_days: int = 3,
    current_pnl_pct: float | None = None,
    asof: date | None = None,
) -> dict[str, Any]:
    if not entry_date:
        return {"triggered": False, "signal_type": "time_stop"}
    if isinstance(entry_date, str):
        entry_date = date.fromisoformat(entry_date[:10])
    today = asof or date.today()
    held_days = (today - entry_date).days
    if held_days < horizon_days:
        return {"triggered": False, "signal_type": "time_stop", "held_days": held_days}
    has_profit = current_pnl_pct is not None and current_pnl_pct > 0
    return {
        "triggered": True,
        "signal_type": "time_stop",
        "severity": "warning",
        "reason": f"持仓{held_days}天已超窗口({horizon_days}天)" + (
            f"，当前盈利{current_pnl_pct:+.1f}%" if has_profit else "，未达目标"
        ),
        "action": "reduce" if has_profit else "sell",
        "held_days": held_days,
    }


def check_lhb_climax(
    lhb_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """龙虎榜高潮见顶：单日净买放量3倍是"最后疯狂"。

    游资主导 → critical 立即止盈；机构主导趋势有支撑 → warning 减仓。
    """
    if not lhb_profile:
        return {"triggered": False, "signal_type": "lhb_climax"}
    climax = lhb_profile.get("climax") or {}
    if not climax.get("matched"):
        return {"triggered": False, "signal_type": "lhb_climax"}
    hot_money_led = lhb_profile.get("dominant_force") == "hot_money"
    return {
        "triggered": True,
        "signal_type": "lhb_climax",
        "severity": "critical" if hot_money_led else "warning",
        "reason": str(climax.get("note") or "龙虎榜净买突然放量，高潮见顶"),
        "action": "sell" if hot_money_led else "reduce",
    }


def check_deep_research_exit(
    deep_score: float | None = None,
) -> dict[str, Any]:
    """深研红线：deep_score<5 必须触发减仓，<3 清仓——不能只是"建议"后无动作。"""
    if not isinstance(deep_score, (int, float)):
        return {"triggered": False, "signal_type": "deep_research_exit"}
    if deep_score >= 5:
        return {"triggered": False, "signal_type": "deep_research_exit"}
    critical = deep_score < 3
    return {
        "triggered": True,
        "signal_type": "deep_research_exit",
        "severity": "critical" if critical else "warning",
        "reason": (
            f"深研评分{deep_score:.1f}/10低于红线5.0，"
            + ("基本面证伪，必须清仓" if critical else "必须减仓，不允许仅观望")
        ),
        "action": "sell" if critical else "reduce",
        "deep_score": deep_score,
    }


def evaluate_all_exit_signals(
    *,
    current_price: float,
    stop_price: float = 0,
    target_price: float = 0,
    target_price_2: float | None = None,
    peak_price: float = 0,
    trailing_pct: float = 5.0,
    entry_date: date | str | None = None,
    horizon_days: int = 3,
    current_pnl_pct: float | None = None,
    temperature_tier: str | None = None,
    prev_temperature_tier: str | None = None,
    sector_lianban_broken: bool = False,
    northbound_net_yi: float | None = None,
    stock_main_net_yi: float | None = None,
    consecutive_outflow_days: int = 0,
    catalyst_events: list[Mapping[str, Any]] | None = None,
    asof: date | None = None,
    auction_open_premium_pct: float | None = None,
    lhb_profile: Mapping[str, Any] | None = None,
    deep_score: float | None = None,
) -> dict[str, Any]:
    """综合评估所有退出信号，返回最高优先级的行动建议。

    lhb_profile 携带龙虎榜主体持有策略时，回撤止盈阈值随主体调整
    （机构主导放宽、游资主导收紧，issue #88 席位分类闭环）。
    """
    policy = (lhb_profile or {}).get("policy") or {}
    policy_trailing = policy.get("trailing_pct")
    if isinstance(policy_trailing, (int, float)) and policy_trailing > 0:
        trailing_pct = float(policy_trailing)
    checks = [
        check_stop_loss(current_price, stop_price),
        check_take_profit(current_price, target_price, target_price_2),
        check_trailing_stop(current_price, peak_price, trailing_pct),
        check_sentiment_exit(temperature_tier, prev_temperature_tier, sector_lianban_broken),
        check_flow_reversal(northbound_net_yi, stock_main_net_yi, consecutive_outflow_days),
        check_catalyst_negated(catalyst_events),
        check_time_stop(entry_date, horizon_days, current_pnl_pct, asof),
        check_lhb_climax(lhb_profile),
        check_deep_research_exit(deep_score),
    ]
    try:
        from daban_adjustments import check_auction_premium_exit

        checks.append(check_auction_premium_exit(
            entry_date=entry_date,
            open_premium_pct=auction_open_premium_pct,
            asof=asof,
        ))
    except ImportError:  # pragma: no cover - flat sys.path imports
        pass

    triggered = [c for c in checks if c.get("triggered")]
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    triggered.sort(key=lambda c: severity_order.get(c.get("severity", "info"), 9))

    if not triggered:
        return {
            "action": "hold",
            "triggered_count": 0,
            "signals": checks,
            "summary": "无退出信号",
        }

    top = triggered[0]
    return {
        "action": top["action"],
        "triggered_count": len(triggered),
        "top_signal": top,
        "signals": checks,
        "summary": f"{len(triggered)}个退出信号: " + "; ".join(
            f"[{c['severity']}]{c['signal_type']}" for c in triggered
        ),
    }
