"""P4 仓位管理与风控阶梯（paper 先行）— 升级方案 §7。

四块内容合在一个模块里，因为它们共享同一个口径（R = 一次预设止损的亏损额），
拆开会让「R 到底是谁的 R」重新变成三处各自定义：

  (a) 1+1+1 加仓状态机   —— 原书第二十七课
  (b) R 化风险预算仓位     —— Position = min(ModeCap, RiskBudget / StopDistance)
  (c) 环境总仓表           —— 消费五档温度 / S0-S6 状态
  (e) 熔断阶梯 R 化        —— 单日 −2R / 单周 −4~−5R / 回撤 8%,10% / 同主题 ≤2R /
                              连续 3 笔系统外交易

(d) 四层止损属于持仓期退出，实现在 ``exit_signals.py``（theme_invalid /
leader_invalid + 事件止损优先），不在本模块。

**边界（重要）**：本模块是纯函数库，不自己接线到实盘。它的消费方目前只有 paper
trading 与研究/回测路径；``decision_policy`` 里的环境总仓表默认关闭
（``HERMES_ENV_POSITION_TABLE`` 未设为 ``enforce`` 时输出与改造前逐字段一致）。

三条硬约束，全部来自原书而不是本实现的偏好：

1. **1+1+1 不是摊低成本**。三条腿是「逻辑成立 → 确认成立 → 盈利确认」，浮亏时
   confirm/profit 两条腿**永久关闭**（``locked``），不是本次跳过。亏损加仓是这套
   方法论明确禁止的动作。
2. **R 化仓位替代「我觉得胜率高所以重仓」**。交易者无法可靠知道自己的条件胜率，
   所以仓位由「愿意亏多少」÷「止损多远」决定，不由信心决定。
3. **StopDistance ≤ 0 必须 fail-closed**。除零会得到无穷仓位；「没有止损」在本模块
   里等价于「不许开仓」，不等价于「不设上限」。

与 ``discipline_score`` / ``behavior_risk`` 的关系：三者输出的仓位倍率一律取
**更保守**的那个，绝不相乘——它们是相互独立的口径，相乘会把同一个坏日子惩罚多次。

纯标准库，cron-safe，不触网。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

SCHEMA = "position_risk_v1"

AVAILABLE = "available"
UNAVAILABLE = "unavailable"
BLOCKED = "blocked"

# ── (a) 1+1+1 加仓状态机 ──────────────────────────────────────────────────────
LEG_ORDER: tuple[str, ...] = ("logic_leg", "confirm_leg", "profit_leg")
DEFAULT_LEG_PCT = 10.0
DEFAULT_MAX_SINGLE_POSITION_PCT = 30.0

#: 浮亏时被永久关闭的腿（原书：绝不因为第一笔亏损而机械加仓）。
LOSS_LOCKED_LEGS = frozenset({"confirm_leg", "profit_leg"})

LADDER_EVENT_TYPE = "position.ladder_leg"

# ── (b) R 化风险预算 ─────────────────────────────────────────────────────────
#: 账户净值的 0.5%-1.0%（方案 §7.1(b)）。超出区间的配置被夹回，并在 notes 标注。
RISK_BUDGET_PCT_RANGE = (0.5, 1.0)
DEFAULT_RISK_BUDGET_PCT = 0.75
#: ATR 倍数研究区间与止损距离研究区间（方案 §7.1(b)）。
ATR_MULTIPLE_RANGE = (1.2, 2.0)
DEFAULT_ATR_MULTIPLE = 1.5
STOP_DISTANCE_PCT_RANGE = (3.0, 8.0)

# ── (c) 环境总仓表 ───────────────────────────────────────────────────────────
#: S0-S6 → 总仓区间（方案 §7.1(c)）。取代 decision_policy 里「S6 压到 20%」的单点规则。
#: 标签与 market_temperature.MARKET_STATE 的语义一一对应；五档温度经 TIER_TO_STATE
#: 折进同一张表（冰点→S0 修复→S1 发酵→S2 加速→S3 极热→S4），不维护第二份阈值。
ENVIRONMENT_POSITION_TABLE: dict[str, dict[str, Any]] = {
    "S0": {"label": "冰点未确认", "min_pct": 0.0, "max_pct": 10.0},
    "S1": {"label": "萌芽确认", "min_pct": 20.0, "max_pct": 40.0},
    "S2": {"label": "发酵", "min_pct": 40.0, "max_pct": 70.0},
    "S3": {"label": "加速", "min_pct": 30.0, "max_pct": 60.0},
    "S4": {"label": "高潮", "min_pct": 0.0, "max_pct": 30.0},
    "S5": {"label": "分歧转一致", "min_pct": 40.0, "max_pct": 70.0},
    "S6": {"label": "退潮确认", "min_pct": 0.0, "max_pct": 10.0},
}

#: 与 market_temperature.TIER_TO_STATE 同值。这里保留一份局部映射是为了让本模块
#: 保持零依赖（cron / 回测路径可以单独 import），值改动须两边同步。
TIER_TO_STATE = {"冰点": "S0", "修复": "S1", "发酵": "S2", "加速": "S3", "极热": "S4"}

# ── (e) 熔断阶梯 ─────────────────────────────────────────────────────────────
DEFAULT_CIRCUIT_CONFIG: dict[str, Any] = {
    "day_loss_r_stop": -2.0,        # 单日 −2R → 停新开
    "week_loss_r_reduce": -4.0,     # 单周 −4R → 降仓
    "week_loss_r_freeze": -5.0,     # 单周 −5R → 冻结（更重的一档）
    "drawdown_halve_pct": 8.0,      # 回撤 8% → 仓位减半
    "drawdown_stop_pct": 10.0,      # 回撤 10% → 停实盘 + 强制复盘周
    "theme_risk_r_max": 2.0,        # 同主题总风险 ≤ 2R
    "off_system_streak_max": 3,     # 连续 3 笔系统外交易 → 强制停手
}

CIRCUIT_EVENT_TYPE = "risk.circuit_rung"

__all__ = [
    "SCHEMA", "AVAILABLE", "UNAVAILABLE", "BLOCKED",
    "LEG_ORDER", "DEFAULT_LEG_PCT", "DEFAULT_MAX_SINGLE_POSITION_PCT",
    "LOSS_LOCKED_LEGS", "LADDER_EVENT_TYPE",
    "RISK_BUDGET_PCT_RANGE", "DEFAULT_RISK_BUDGET_PCT",
    "ATR_MULTIPLE_RANGE", "DEFAULT_ATR_MULTIPLE", "STOP_DISTANCE_PCT_RANGE",
    "ENVIRONMENT_POSITION_TABLE", "TIER_TO_STATE",
    "DEFAULT_CIRCUIT_CONFIG", "CIRCUIT_EVENT_TYPE",
    "new_ladder", "apply_leg", "ladder_leg_event",
    "resolve_stop_distance_pct", "r_sized_position",
    "environment_position_band", "environment_position_multiplier",
    "assess_circuit_ladder", "circuit_ladder_events",
    "merge_position_multipliers", "theme_risk_from_positions",
]


def _number(value: Any) -> Optional[float]:
    """严格数值化：``None`` / 非数值 / 布尔一律返回 None（不静默当 0 或 1）。"""
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, (int, float)):
        return None
    if value != value:  # NaN：pandas 记录取值的常见污染源，不能当真值传下去
        return None
    return float(value)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# ═══════════════════════════════════════════════════════════════════════════
# (a) 1+1+1 加仓状态机
# ═══════════════════════════════════════════════════════════════════════════

def new_ladder(code: Any, *, leg_pct: float = DEFAULT_LEG_PCT,
               max_single_position_pct: float = DEFAULT_MAX_SINGLE_POSITION_PCT
               ) -> dict[str, Any]:
    """新建一个空的加仓阶梯状态（不可变风格：``apply_leg`` 返回新对象）。"""
    return {
        "schema": SCHEMA,
        "code": str(code or "").zfill(6) if code is not None else "",
        "filled_legs": [],
        "position_pct": 0.0,
        "locked": False,
        "lock_reason": None,
        "leg_pct": float(leg_pct),
        "max_single_position_pct": float(max_single_position_pct),
    }


def _leg_conditions(leg: str, context: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """每条腿自己的成立条件。返回 (满足, 未满足原因列表)。"""
    unmet: list[str] = []
    if leg == "logic_leg":
        if context.get("signal_valid") is not True:
            unmet.append("signal_not_valid")
    elif leg == "confirm_leg":
        if context.get("sector_confirmed") is not True:
            unmet.append("sector_not_confirmed")
        if context.get("leader_confirmed") is not True:
            unmet.append("leader_not_confirmed")
        if context.get("market_deteriorated") is True:
            unmet.append("market_deteriorated")
    elif leg == "profit_leg":
        pnl = _number(context.get("unrealized_pnl_pct"))
        if pnl is None or pnl <= 0:
            unmet.append("no_unrealized_profit")
        if context.get("logic_still_valid") is not True:
            unmet.append("logic_no_longer_valid")
    return (not unmet), unmet


def _ladder_precheck(ladder: Mapping[str, Any], leg: str,
                     context: Mapping[str, Any]) -> tuple[list[str], bool]:
    """顺序/上限/亏损三类结构性拒绝。返回 (原因列表, 是否要永久上锁)。"""
    reasons: list[str] = []
    filled = list(ladder.get("filled_legs") or [])
    if leg not in LEG_ORDER:
        return ["unknown_leg"], False
    if ladder.get("locked"):
        return [str(ladder.get("lock_reason") or "ladder_locked")], False
    if leg in filled:
        reasons.append("leg_already_filled")
    elif LEG_ORDER.index(leg) != len(filled):
        # 跳级：只有 confirm 落地后才谈 profit，不允许直接跳到 profit_leg。
        reasons.append("leg_out_of_order")

    pnl = _number(context.get("unrealized_pnl_pct"))
    lock = False
    if leg in LOSS_LOCKED_LEGS:
        if pnl is None:
            reasons.append("unrealized_pnl_unavailable")
        elif pnl < 0:
            # 永久关闭，不是本次跳过：亏损加仓是原书的硬禁令。
            reasons.append("losing_add_forbidden")
            lock = True

    leg_pct = float(ladder.get("leg_pct") or DEFAULT_LEG_PCT)
    cap = float(ladder.get("max_single_position_pct") or DEFAULT_MAX_SINGLE_POSITION_PCT)
    if float(ladder.get("position_pct") or 0.0) + leg_pct > cap + 1e-9:
        reasons.append("single_position_cap_exceeded")
    return reasons, lock


def apply_leg(ladder: Mapping[str, Any], leg: str,
              context: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """尝试落一条腿。返回 ``{"decision": ..., "ladder": 新状态}``，不修改入参。

    ``decision.allowed`` 为 False 时 ``ladder`` 仍可能变化——浮亏触发的永久上锁
    必须落在返回的状态里，否则下一次调用会重新放行。
    """
    ctx = dict(context or {})
    state = {**dict(ladder), "filled_legs": list(ladder.get("filled_legs") or [])}
    reasons, lock = _ladder_precheck(state, leg, ctx)
    if lock:
        state["locked"] = True
        state["lock_reason"] = "losing_add_forbidden"
    if not reasons:
        satisfied, unmet = _leg_conditions(leg, ctx)
        if not satisfied:
            reasons.extend(unmet)
    allowed = not reasons
    if allowed:
        state["filled_legs"] = list(state["filled_legs"]) + [leg]
        state["position_pct"] = round(
            float(state.get("position_pct") or 0.0)
            + float(state.get("leg_pct") or DEFAULT_LEG_PCT), 4)
    decision = {
        "schema": SCHEMA,
        "leg": leg,
        "allowed": allowed,
        "status": AVAILABLE if allowed else BLOCKED,
        "position_pct_delta": (
            float(state.get("leg_pct") or DEFAULT_LEG_PCT) if allowed else 0.0),
        "position_pct_after": float(state.get("position_pct") or 0.0),
        "filled_legs_after": list(state["filled_legs"]),
        "locked": bool(state.get("locked")),
        "reasons": reasons,
    }
    return {"decision": decision, "ladder": state}


def ladder_leg_event(decision: Mapping[str, Any], links: Mapping[str, Any],
                     *, extra: Optional[Mapping[str, Any]] = None
                     ) -> dict[str, Any]:
    """把一次加仓尝试（成功或被拒）渲染成 signal_ledger 事件，审计可回放。

    被拒的腿同样落账：只记成功的话，「今天为什么没加仓」在回放里查不到。
    """
    leg = str(decision.get("leg") or "")
    status = "filled" if decision.get("allowed") else "blocked"
    key_parts = [str(links.get("signal_id") or links.get("code") or ""), leg, status]
    payload = {
        "leg": leg,
        "allowed": bool(decision.get("allowed")),
        "position_pct_delta": decision.get("position_pct_delta"),
        "position_pct_after": decision.get("position_pct_after"),
        "filled_legs_after": list(decision.get("filled_legs_after") or []),
        "locked": bool(decision.get("locked")),
        "reasons": list(decision.get("reasons") or []),
        **dict(extra or {}),
    }
    return {
        "event_type": LADDER_EVENT_TYPE,
        "links": dict(links),
        "payload": payload,
        "idempotency_key": f"{LADDER_EVENT_TYPE}:" + ":".join(key_parts),
    }


# ═══════════════════════════════════════════════════════════════════════════
# (b) R 化风险预算
# ═══════════════════════════════════════════════════════════════════════════

def resolve_stop_distance_pct(
    *,
    structural_stop_pct: Any = None,
    atr_pct: Any = None,
    atr_multiple: Any = DEFAULT_ATR_MULTIPLE,
) -> dict[str, Any]:
    """止损距离（%）。优先策略结构止损，其次 ATR 倍数；一律夹进 3-8% 研究区间。

    两者都拿不到时返回 ``unavailable`` 且 ``stop_distance_pct=None`` —— **不返回 0**，
    因为 0 会在下游被当成除数。
    """
    notes: list[str] = []
    multiple = _number(atr_multiple)
    if multiple is None:
        multiple = DEFAULT_ATR_MULTIPLE
    clamped_multiple = _clamp(multiple, *ATR_MULTIPLE_RANGE)
    if clamped_multiple != multiple:
        notes.append("atr_multiple_clamped")

    structural = _number(structural_stop_pct)
    atr = _number(atr_pct)
    if structural is not None and structural > 0:
        raw, source = structural, "structural"
    elif atr is not None and atr > 0:
        raw, source = atr * clamped_multiple, "atr"
    else:
        return {"schema": SCHEMA, "status": UNAVAILABLE, "stop_distance_pct": None,
                "source": None, "atr_multiple": clamped_multiple,
                "notes": notes + ["stop_distance_unavailable"]}

    clamped = _clamp(raw, *STOP_DISTANCE_PCT_RANGE)
    if abs(clamped - raw) > 1e-9:
        notes.append("stop_distance_clamped")
    return {"schema": SCHEMA, "status": AVAILABLE,
            "stop_distance_pct": round(clamped, 4), "raw_stop_distance_pct": round(raw, 4),
            "source": source, "atr_multiple": clamped_multiple, "notes": notes}


def r_sized_position(
    *,
    net_asset_value: Any,
    stop_distance_pct: Any,
    mode_cap_pct: Any,
    risk_budget_pct: Any = DEFAULT_RISK_BUDGET_PCT,
) -> dict[str, Any]:
    """``Position = min(ModeCap, RiskBudget / StopDistance)``。

    单位换算：RiskBudget(元) = NAV × rb%/100，StopDistance(比例) = sd%/100，
    于是 仓位% = rb% / sd% × 100。算例见 docs/position-risk-p4-2026-08.md。

    fail-closed 的三个入口：NAV ≤ 0、StopDistance ≤ 0/缺失、ModeCap ≤ 0 —— 全部返回
    ``status=blocked`` 且 ``position_pct=0.0``。**不返回无穷仓位，也不静默改用默认值。**
    """
    reasons: list[str] = []
    nav = _number(net_asset_value)
    sd = _number(stop_distance_pct)
    cap = _number(mode_cap_pct)
    budget = _number(risk_budget_pct)
    if budget is None:
        budget = DEFAULT_RISK_BUDGET_PCT
    clamped_budget = _clamp(budget, *RISK_BUDGET_PCT_RANGE)
    if abs(clamped_budget - budget) > 1e-9:
        reasons.append("risk_budget_pct_clamped")

    if nav is None or nav <= 0:
        reasons.append("net_asset_value_unavailable")
    if sd is None or sd <= 0:
        reasons.append("stop_distance_unavailable")
    if cap is None or cap <= 0:
        reasons.append("mode_cap_unavailable")
    blocking = {"net_asset_value_unavailable", "stop_distance_unavailable",
                "mode_cap_unavailable"}
    if blocking & set(reasons):
        return {"schema": SCHEMA, "status": BLOCKED, "position_pct": 0.0,
                "position_value": 0.0, "risk_budget_pct": clamped_budget,
                "risk_budget_value": None, "stop_distance_pct": sd,
                "mode_cap_pct": cap, "binding": "fail_closed", "reasons": reasons}

    risk_pct = clamped_budget / sd * 100.0
    position_pct = min(cap, risk_pct)
    binding = "mode_cap" if cap <= risk_pct else "risk_budget"
    return {
        "schema": SCHEMA,
        "status": AVAILABLE,
        "position_pct": round(position_pct, 4),
        "position_value": round(nav * position_pct / 100.0, 2),
        "risk_budget_pct": clamped_budget,
        "risk_budget_value": round(nav * clamped_budget / 100.0, 2),
        "risk_sized_pct": round(risk_pct, 4),
        "stop_distance_pct": sd,
        "mode_cap_pct": cap,
        "binding": binding,
        "reasons": reasons,
    }


# ═══════════════════════════════════════════════════════════════════════════
# (c) 环境总仓表
# ═══════════════════════════════════════════════════════════════════════════

def environment_position_band(state: Any = None, tier: Any = None) -> dict[str, Any]:
    """S0-S6（或五档温度）→ 总仓区间。

    识别不出状态时 fail-closed 到 0-0%，而不是回落到某个「中性」区间：不知道环境
    是什么的那天，恰好是最不该按中性仓位下注的那天。
    """
    key = str(state or "").upper().strip()
    resolved_from = "state"
    if key not in ENVIRONMENT_POSITION_TABLE:
        tier_key = str(tier or "").strip()
        key = TIER_TO_STATE.get(tier_key, "")
        resolved_from = "tier" if key else "none"
    row = ENVIRONMENT_POSITION_TABLE.get(key)
    if row is None:
        return {"schema": SCHEMA, "status": UNAVAILABLE, "state": None, "label": None,
                "min_pct": 0.0, "max_pct": 0.0, "resolved_from": "none",
                "reason": "market_state_unknown"}
    return {"schema": SCHEMA, "status": AVAILABLE, "state": key,
            "label": row["label"], "min_pct": row["min_pct"], "max_pct": row["max_pct"],
            "resolved_from": resolved_from, "reason": None}


def environment_position_multiplier(state: Any = None, tier: Any = None
                                    ) -> dict[str, Any]:
    """把总仓表折成仓位倍率（相对满仓 100%），供 decision_policy 与 paper 层消费。"""
    band = environment_position_band(state, tier)
    return {
        "schema": SCHEMA,
        "status": band["status"],
        "state": band["state"],
        "label": band["label"],
        "position_multiplier": round(float(band["max_pct"]) / 100.0, 4),
        "band_pct": [band["min_pct"], band["max_pct"]],
        "reason": band["reason"],
    }


# ═══════════════════════════════════════════════════════════════════════════
# (e) 熔断阶梯（R 化）
# ═══════════════════════════════════════════════════════════════════════════

def _rung(name: str, triggered: bool, *, observed: Any, threshold: Any,
          multiplier: Optional[float], detail: str) -> dict[str, Any]:
    return {
        "rung": name,
        "triggered": bool(triggered),
        "observed": observed,
        "threshold": threshold,
        "position_multiplier": multiplier if triggered else None,
        "detail": detail,
    }


def _theme_risk_rungs(theme_risk_r: Optional[Mapping[str, Any]], limit: float
                      ) -> tuple[list[dict[str, Any]], list[str]]:
    rungs: list[dict[str, Any]] = []
    blocked: list[str] = []
    for theme, value in sorted(dict(theme_risk_r or {}).items()):
        amount = _number(value)
        if amount is None:
            continue
        hit = amount > limit + 1e-9
        if hit:
            blocked.append(str(theme))
        rungs.append(_rung(f"theme_risk_cap:{theme}", hit, observed=round(amount, 4),
                           threshold=limit, multiplier=0.0 if hit else None,
                           detail=f"同主题 {theme} 总风险 {amount:.2f}R（上限 {limit:.1f}R）"))
    return rungs, blocked


def assess_circuit_ladder(
    *,
    day_pnl_r: Any = None,
    week_pnl_r: Any = None,
    drawdown_pct: Any = None,
    theme_risk_r: Optional[Mapping[str, Any]] = None,
    off_system_streak: Any = None,
    config: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """熔断阶梯：每一档触发与未触发都返回，供落账回放对账。

    R 化的意义：−2R 在不同账户净值下是不同的金额，但是同一个「两次预设止损」。
    调用方负责把成交盈亏折算成 R（一次预设止损的亏损额为 1R）。

    缺数据的那一档标 ``triggered=False`` 且 ``observed=None``——**不臆造 0**，
    否则「今天没亏」和「今天没数据」在回放里无法区分。
    """
    cfg = {**DEFAULT_CIRCUIT_CONFIG, **dict(config or {})}
    day = _number(day_pnl_r)
    week = _number(week_pnl_r)
    dd = _number(drawdown_pct)
    streak = _number(off_system_streak)

    rungs = [
        _rung("day_loss_2r", day is not None and day <= float(cfg["day_loss_r_stop"]),
              observed=day, threshold=cfg["day_loss_r_stop"], multiplier=0.0,
              detail="单日 −2R：停止新开仓"),
        _rung("week_loss_reduce",
              week is not None and week <= float(cfg["week_loss_r_reduce"]),
              observed=week, threshold=cfg["week_loss_r_reduce"], multiplier=0.5,
              detail="单周 −4R：降仓"),
        _rung("week_loss_freeze",
              week is not None and week <= float(cfg["week_loss_r_freeze"]),
              observed=week, threshold=cfg["week_loss_r_freeze"], multiplier=0.0,
              detail="单周 −5R：冻结新开仓"),
        _rung("drawdown_halve",
              dd is not None and dd >= float(cfg["drawdown_halve_pct"]),
              observed=dd, threshold=cfg["drawdown_halve_pct"], multiplier=0.5,
              detail="回撤 8%：仓位减半"),
        _rung("drawdown_stop",
              dd is not None and dd >= float(cfg["drawdown_stop_pct"]),
              observed=dd, threshold=cfg["drawdown_stop_pct"], multiplier=0.0,
              detail="回撤 10%：停实盘 + 强制复盘周"),
        _rung("off_system_streak",
              streak is not None and streak >= float(cfg["off_system_streak_max"]),
              observed=streak, threshold=cfg["off_system_streak_max"], multiplier=0.0,
              detail="连续 3 笔系统外交易：强制停手"),
    ]
    theme_rungs, blocked_themes = _theme_risk_rungs(
        theme_risk_r, float(cfg["theme_risk_r_max"]))
    rungs.extend(theme_rungs)

    triggered = [rung for rung in rungs if rung["triggered"]]
    multipliers = [rung["position_multiplier"] for rung in triggered
                   if rung["position_multiplier"] is not None]
    multiplier = min(multipliers) if multipliers else 1.0
    stop_rungs = {"day_loss_2r", "week_loss_freeze", "drawdown_stop", "off_system_streak"}
    names = {rung["rung"] for rung in triggered}
    return {
        "schema": SCHEMA,
        "status": AVAILABLE,
        "rungs": rungs,
        "triggered": sorted(names),
        "position_multiplier": round(float(multiplier), 4),
        "new_open_allowed": not (names & stop_rungs),
        "blocked_themes": blocked_themes,
        "review_week_required": "drawdown_stop" in names,
        "live_trading_halted": "drawdown_stop" in names or "off_system_streak" in names,
        "thresholds": cfg,
    }


def circuit_ladder_events(result: Mapping[str, Any], links: Mapping[str, Any],
                          *, asof: Optional[str] = None) -> list[dict[str, Any]]:
    """每一档一条事件（含未触发档），使「当天熔断状态」可逐档回放对账。"""
    day = str(asof or links.get("trade_date") or links.get("asof") or "")
    events: list[dict[str, Any]] = []
    for rung in result.get("rungs") or []:
        name = str(rung.get("rung"))
        events.append({
            "event_type": CIRCUIT_EVENT_TYPE,
            "links": dict(links),
            "payload": {
                "rung": name,
                "triggered": bool(rung.get("triggered")),
                "observed": rung.get("observed"),
                "threshold": rung.get("threshold"),
                "position_multiplier": rung.get("position_multiplier"),
                "detail": rung.get("detail"),
                "asof": day or None,
            },
            "idempotency_key": f"{CIRCUIT_EVENT_TYPE}:{day}:{name}",
        })
    return events


def merge_position_multipliers(*sources: Any) -> dict[str, Any]:
    """多路仓位倍率合并 —— 取**更保守**（最小）的一个，绝不相乘。

    入参可以是裸数值，也可以是带 ``position_multiplier`` 字段的结果字典
    （``assess_circuit_ladder`` / ``discipline_score.combined_position_multiplier`` /
    ``environment_position_multiplier`` 都能直接丢进来）。全部不可用时返回
    ``unavailable`` 而不是 1.0：没有任何风控口径可用的那天不该拿到满仓授权。
    """
    values: list[tuple[str, float]] = []
    for index, source in enumerate(sources):
        if isinstance(source, Mapping):
            name = str(source.get("source") or source.get("schema") or f"input_{index}")
            value = _number(source.get("position_multiplier"))
        else:
            name, value = f"input_{index}", _number(source)
        if value is not None:
            values.append((name, value))
    if not values:
        return {"schema": SCHEMA, "status": UNAVAILABLE, "position_multiplier": None,
                "source": None, "inputs": [], "reason": "no_multiplier_available"}
    chosen_name, chosen = min(values, key=lambda pair: pair[1])
    return {
        "schema": SCHEMA,
        "status": AVAILABLE,
        "position_multiplier": round(chosen, 4),
        "source": chosen_name,
        "inputs": [{"source": name, "position_multiplier": value}
                   for name, value in values],
        "reason": None,
    }


def theme_risk_from_positions(positions: Iterable[Mapping[str, Any]] | Sequence[Any],
                              *, risk_unit_value: Any) -> dict[str, float]:
    """按主题汇总在险金额并折成 R，供 ``assess_circuit_ladder(theme_risk_r=...)``。

    单笔在险金额 = 持仓市值 × 止损距离%/100；缺止损距离的持仓**跳过而不是当 0**
    （它不是「没有风险」，而是「风险未知」，由上游的 fail-closed 单独处理）。
    """
    unit = _number(risk_unit_value)
    if unit is None or unit <= 0:
        return {}
    totals: dict[str, float] = {}
    for position in positions or ():
        if not isinstance(position, Mapping):
            continue
        value = _number(position.get("market_value"))
        stop_pct = _number(position.get("stop_distance_pct"))
        if value is None or stop_pct is None or stop_pct <= 0:
            continue
        theme = str(position.get("theme") or position.get("sector") or "unknown")
        totals[theme] = round(totals.get(theme, 0.0) + value * stop_pct / 100.0 / unit, 4)
    return totals
