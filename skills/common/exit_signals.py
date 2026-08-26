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
  9. deep_research_exit: 深研低分复核（普通/过期评分不得直接触发交易）
 10. theme_invalid:   题材失效（题材分跌幅≥20 或主线降为后排 + 助攻大面积掉队）
 11. leader_invalid:  龙头失效（LeaderScore 跌幅≥20 或龙头断板 + 承接断层）
 12. event_stop:      事件止损（龙头大幅低开 ∧ 助攻无溢价 ∧ 昨日后排跌停）

每个信号返回 {triggered, signal_type, severity, reason, action}。
severity: critical(立即卖) / warning(减仓/关注) / info(记录)

四层止损（升级方案 P4(d)）：市场层(sentiment_exit/flow_reversal) → 题材层(theme_invalid)
→ 龙头层(leader_invalid/event_stop/lhb_climax) → 个股层(stop_loss/trailing/…)。
``evaluate_all_exit_signals`` 给每条信号打 ``exit_layer`` 标签，并让**事件止损优先于
价格止损**：同为 critical 时，事件类信号排在价格类之前。理由是价格止损在情绪股上
天然滞后——龙头低开、助攻无溢价、昨日后排跌停已经把承接打穿时，ATR 还没触及，
但那一口承接明天不会回来。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

#: 每类信号归属的止损层（四层止损）。未登记的信号落 "stock"，不静默丢弃。
EXIT_LAYERS: dict[str, str] = {
    "sentiment_exit": "market",
    "flow_reversal": "market",
    "theme_invalid": "theme",
    "leader_invalid": "leader",
    "event_stop": "leader",
    "lhb_climax": "leader",
    "stop_loss": "stock",
    "take_profit": "stock",
    "trailing_stop": "stock",
    "time_stop": "stock",
    "catalyst_negated": "stock",
    "deep_research_exit": "stock",
    "auction_premium_exit": "stock",
}

#: 事件止损集合：同 severity 下排序优先于价格止损。只含 P4 新增的三类，
#: 既有信号的相对次序一字未动（改动它们会静默重排历史告警的 top_signal）。
EVENT_STOP_SIGNALS = frozenset({"event_stop", "leader_invalid", "theme_invalid"})

#: 题材/龙头失效的评分跌幅阈值（方案 §7.1(d)：≥20）。
SCORE_COLLAPSE_DROP = 20.0
#: 助攻「大面积掉队」的比例阈值：过半助攻掉队即视为主线塌方。
ASSIST_LAGGARD_RATIO = 0.5
#: 事件止损中「大幅低开」的口径。
LEADER_GAP_DOWN_PCT = -3.0


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
    deep_score: float | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把低深研分降级为人工复核，不把 agent 判断伪装成交易风控证据。

    当前 deep_research_cache_v1 没有结构化、独立验证且新鲜的 hard-risk
    evidence schema，因此普通数值和缓存记录一律 ``execution_eligible=False``。
    价格止损、催化否定等确定性退出信号仍由各自检查器独立执行。
    """
    record = deep_score if isinstance(deep_score, Mapping) else None
    score = record.get("deep_score") if record is not None else deep_score
    if not isinstance(score, (int, float)):
        return {"triggered": False, "signal_type": "deep_research_exit"}
    if score >= 5:
        return {"triggered": False, "signal_type": "deep_research_exit"}
    stale = bool(record and record.get("stale"))
    freshness_status = (
        str(record.get("freshness_status") or ("stale" if stale else "fresh"))
        if record is not None
        else "unknown"
    )
    freshness_note = "且研究缓存已过期，" if stale else "，"
    return {
        "triggered": True,
        "signal_type": "deep_research_exit",
        "severity": "info",
        "reason": (
            f"深研评分{score:.1f}/10低于复核线5.0{freshness_note}"
            "但该评分未绑定可执行硬风险证据，仅要求研究复核"
        ),
        "action": "hold",
        "deep_score": score,
        "review_required": True,
        "execution_eligible": False,
        "evidence_status": "unbound_score",
        "freshness_status": freshness_status,
    }


def _pct(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value:  # NaN
        return None
    return float(value)


def check_theme_invalid(
    theme_score_drop_pct: Any = None,
    theme_rank_demoted: bool = False,
    assist_laggard_ratio: Any = None,
) -> dict[str, Any]:
    """题材层：主线塌方就退出，不等个股价格确认。

    两条独立触发路径（方案 §7.1(d)）：
      1. ThemeScore 跌幅 ≥ 20；
      2. 主线降为后排 **且** 助攻大面积掉队（过半）—— 单独降级不算，题材轮动里
         排名波动是常态，只有「降级 + 助攻整体掉队」才说明这条线没人接了。
    """
    drop = _pct(theme_score_drop_pct)
    ratio = _pct(assist_laggard_ratio)
    score_collapsed = drop is not None and drop >= SCORE_COLLAPSE_DROP
    breadth_collapsed = bool(theme_rank_demoted) and ratio is not None and ratio >= ASSIST_LAGGARD_RATIO
    triggered = score_collapsed or breadth_collapsed
    parts = []
    if score_collapsed:
        parts.append(f"题材分跌幅{drop:.0f}(阈值{SCORE_COLLAPSE_DROP:.0f})")
    if breadth_collapsed:
        parts.append(f"主线降为后排且助攻掉队{ratio:.0%}")
    return {
        "triggered": triggered,
        "signal_type": "theme_invalid",
        "severity": "critical" if triggered else "info",
        "reason": "；".join(parts) if triggered else "",
        "action": "sell" if triggered else "hold",
        "theme_score_drop_pct": drop,
        "assist_laggard_ratio": ratio,
    }


def check_leader_invalid(
    leader_score_drop_pct: Any = None,
    leader_streak_broken: bool = False,
    bid_support_broken: bool = False,
) -> dict[str, Any]:
    """龙头层：龙头失效则跟风盘全部失去定价锚，持仓逻辑不再成立。

    触发路径：LeaderScore 跌幅 ≥ 20，或 龙头断板 **且** 承接断层。断板单独出现
    可能只是换手，配上承接断层才是「没人接了」。
    """
    drop = _pct(leader_score_drop_pct)
    score_collapsed = drop is not None and drop >= SCORE_COLLAPSE_DROP
    support_collapsed = bool(leader_streak_broken) and bool(bid_support_broken)
    triggered = score_collapsed or support_collapsed
    parts = []
    if score_collapsed:
        parts.append(f"龙头分跌幅{drop:.0f}(阈值{SCORE_COLLAPSE_DROP:.0f})")
    if support_collapsed:
        parts.append("龙头断板且承接断层")
    return {
        "triggered": triggered,
        "signal_type": "leader_invalid",
        "severity": "critical" if triggered else "info",
        "reason": "；".join(parts) if triggered else "",
        "action": "sell" if triggered else "hold",
        "leader_score_drop_pct": drop,
    }


def check_event_stop(
    leader_gap_pct: Any = None,
    assist_premium_pct: Any = None,
    laggard_limit_down: bool = False,
) -> dict[str, Any]:
    """事件止损：龙头大幅低开 ∧ 助攻无溢价 ∧ 昨日后排跌停 → 未触 ATR 也退出。

    三个条件必须同时成立（方案 §7.1(d) 原文）。任一缺失即返回未触发——这条规则
    强到可以越过价格止损，不能让缺数据把它推成默认成立。
    """
    gap = _pct(leader_gap_pct)
    premium = _pct(assist_premium_pct)
    if gap is None or premium is None:
        return {"triggered": False, "signal_type": "event_stop"}
    triggered = (
        gap <= LEADER_GAP_DOWN_PCT
        and premium <= 0
        and bool(laggard_limit_down)
    )
    if not triggered:
        return {"triggered": False, "signal_type": "event_stop"}
    return {
        "triggered": True,
        "signal_type": "event_stop",
        "severity": "critical",
        "reason": (
            f"龙头低开{gap:.1f}%、助攻溢价{premium:.1f}%、昨日后排跌停"
            "——承接已断，事件止损优先于价格止损"
        ),
        "action": "sell",
        "price_stop_bypassed": True,
    }


def _rank_triggered(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """排序触发的信号：先按 severity，同 severity 下**事件止损优先于价格止损**。

    ``sort`` 稳定，因此既有信号之间的相对次序一字未动——改动它们会静默重排历史
    告警的 top_signal。
    """
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    triggered = [c for c in checks if c.get("triggered")]
    triggered.sort(key=lambda c: (
        severity_order.get(c.get("severity", "info"), 9),
        0 if c.get("signal_type") in EVENT_STOP_SIGNALS else 1,
    ))
    return triggered


def _exit_verdict(checks: list[dict[str, Any]],
                  triggered: list[dict[str, Any]]) -> dict[str, Any]:
    """把排好序的触发信号折成最终行动建议 + 四层命中情况。"""
    if not triggered:
        return {"action": "hold", "triggered_count": 0, "signals": checks,
                "layers_triggered": [], "summary": "无退出信号"}
    top = triggered[0]
    layers = [layer for layer in ("market", "theme", "leader", "stock")
              if any(c.get("exit_layer") == layer for c in triggered)]
    return {
        "action": top["action"],
        "triggered_count": len(triggered),
        "top_signal": top,
        "signals": checks,
        "layers_triggered": layers,
        "event_stop_priority": top.get("signal_type") in EVENT_STOP_SIGNALS,
        "summary": f"{len(triggered)}个退出信号: " + "; ".join(
            f"[{c['severity']}]{c['signal_type']}" for c in triggered
        ),
    }


def _auction_premium_check(entry_date: Any, open_premium_pct: Any,
                           asof: Any) -> list[dict[str, Any]]:
    try:
        from daban_adjustments import check_auction_premium_exit
    except ImportError:  # pragma: no cover - flat sys.path imports
        return []
    return [check_auction_premium_exit(
        entry_date=entry_date, open_premium_pct=open_premium_pct, asof=asof)]


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
    deep_score: float | Mapping[str, Any] | None = None,
    theme_score_drop_pct: float | None = None,
    theme_rank_demoted: bool = False,
    assist_laggard_ratio: float | None = None,
    leader_score_drop_pct: float | None = None,
    leader_streak_broken: bool = False,
    bid_support_broken: bool = False,
    leader_gap_pct: float | None = None,
    assist_premium_pct: float | None = None,
    laggard_limit_down: bool = False,
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
        check_theme_invalid(theme_score_drop_pct, theme_rank_demoted, assist_laggard_ratio),
        check_leader_invalid(leader_score_drop_pct, leader_streak_broken, bid_support_broken),
        check_event_stop(leader_gap_pct, assist_premium_pct, laggard_limit_down),
    ]
    checks.extend(_auction_premium_check(entry_date, auction_open_premium_pct, asof))
    for check in checks:
        check["exit_layer"] = EXIT_LAYERS.get(str(check.get("signal_type")), "stock")

    return _exit_verdict(checks, _rank_triggered(checks))
