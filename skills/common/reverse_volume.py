#!/usr/bin/env python3
"""
S5 反量龙回头（ReverseVolume）信号 — 升级方案 §6.1，NON-LIVE 研究层
=======================================================================
信号原型取自原书摩恩电气案例，核心抽象是**力的反转**：恐慌下跌 → 波动收敛 →
空量衰竭 → 多量超过旧空量 → 回踩继续缩量 → 再次上攻放量。

    前提：      标的是前一周期最高人气/高度股 ∧ 从高点回撤 25%-40% ∧ 大盘/情绪不再加速恶化
    初步观察：  3-5 日波动收窄 ∧ 成交量缩至 20 日低分位
    反量确认：  MaxUpMinuteVolume ≥ 1.3-1.5 × 此前最大下跌分钟量
    回踩确认：  价格下探但 MaxDownMinuteVolume 继续下降
    入场：      10% 观察仓
    二次确认：  MaxUpMinuteVolume / MaxDownMinuteVolume ≥ 1.5 ∧ 价格突破短期平衡区 → 加至 20%-30%

本策略的成败点（务必读完再改代码）
----------------------------------
1) **反量确认/回踩确认里的"分钟量峰值"必须复用 skills/common/minute_derived.py**，
   不得自己再解析一遍分钟数据——两份解析迟早分叉，且单位换算（腾讯"手"累计值 /
   新浪"股"增量值）minute_derived 已经踩过坑并有测试守着。本模块只在
   ``max_directional_minute_volume`` 里加一层 minute_derived 没有的语义：把
   分钟行按涨跌方向分类，用来分别取"上攻分钟"和"下跌分钟"各自的成交量极值。
2) **"此前最大下跌分钟量"必须是入场时刻之前的历史极值**，不能用整段行情
   （含入场之后）的极值——否则用未来数据证明了过去的判断，是标准的未来函数。
   ``max_directional_minute_volume`` 的 ``until_time`` 截断参数就是唯一防线：
   调用方把入场后的分钟数据也喂进来，只要 ``until_time`` 不变，结果必须不变。
3) **1.3-1.5 / 1.5 这些比值来自单个历史案例（摩恩电气）的工程化，不是统计结论**，
   全部放进 config/daban_thresholds.yaml 的 reverse_volume 节，不硬编码；
   pack 与闸门报告里必须显式标注"未经样本外验证"。
4) 回撤 25%-40%、波动收窄比例、缩量分位同样进 config，禁止硬编码。

fail-closed 纪律（缺证据 ≠ 不触发）：任一必需字段缺失 → status="unavailable"
并给出 reasons，**绝不返回 no_signal**。把"没数据"折叠成"不触发"会让零样本看起来
像已验证的负结果，是假绿的一种。

阈值单一事实源：config/daban_thresholds.yaml 的 reverse_volume 节
（缺失回退 daban_config.DEFAULTS，同值）。

红线：本模块未在 strategy_registry 注册，任何调用方都不得把它的输出折进实盘排序、
评分或仓位。升级路径见 config/strategy_packs/reverse_volume.yaml。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

import daban_config as _cfg
import minute_derived as md

SCHEMA = "reverse_volume_signal_v1"

STATUS_SIGNAL = "signal"
STATUS_NO_SIGNAL = "no_signal"
STATUS_UNAVAILABLE = "unavailable"

DIRECTION_UP = "up"
DIRECTION_DOWN = "down"

# 入场七条件 id（前提3 + 初步观察2 + 反量/回踩确认2）—— 报告/测试按 id 逐条断言，
# 避免用中文串匹配。
COND_PRIOR_LEADER = "prior_period_top_leader"
COND_DRAWDOWN = "drawdown_in_range"
COND_SENTIMENT_STABLE = "sentiment_not_deteriorating"
COND_VOLATILITY_CONTRACTION = "volatility_contraction"
COND_VOLUME_LOW_PERCENTILE = "volume_low_percentile"
COND_REVERSAL_VOLUME = "reversal_volume_confirmed"
COND_PULLBACK_SHRINK = "pullback_down_volume_shrinking"
CONDITION_IDS = (
    COND_PRIOR_LEADER, COND_DRAWDOWN, COND_SENTIMENT_STABLE,
    COND_VOLATILITY_CONTRACTION, COND_VOLUME_LOW_PERCENTILE,
    COND_REVERSAL_VOLUME, COND_PULLBACK_SHRINK,
)

# 二次确认（加仓）条件 id —— 与入场七条件完全独立评估，见 second_confirmation()。
COND2_RATIO = "reversal_volume_ratio_second_confirm"
COND2_BREAKOUT = "breakout_above_balance_zone"
SECOND_CONFIRMATION_CONDITION_IDS = (COND2_RATIO, COND2_BREAKOUT)


def config(path: Optional[str] = None) -> dict[str, Any]:
    """取 reverse_volume 阈值（yaml 覆盖 DEFAULTS）。"""
    return _cfg.section("reverse_volume", path)


def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN 不是数字（pandas 记录常见）


def _bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    return None


def _condition(cid: str, ok: Optional[bool], detail: str) -> dict[str, Any]:
    return {"id": cid, "ok": ok, "detail": detail}


# =========================================================================== #
# 分钟量峰值 —— 唯一新增的派生语义（方向分类），一切单位换算/时间解析都走
# minute_derived，本节不重新实现
# =========================================================================== #
def _attach_direction(rows: Optional[Iterable[Mapping[str, Any]]],
                      source: str) -> Optional[list[dict[str, Any]]]:
    """原始供应商行 → minute_derived 归一化行（增量成交股数）+ 涨跌方向标记。

    方向判据依供应商形状而不同（都来自原始行自带的价格字段，不额外触网/猜测）：
      - 腾讯分时（``price`` 为该分钟的现价 tick）：与上一分钟价格比较；首分钟
        无上一分钟可比，方向记 None（不计入涨跌分钟量的候选）。
      - 新浪 5 分钟 K（``open``/``close`` 为该 bar 自身的开收）：bar 自身
        close 与 open 比较。

    只要 minute_derived 的归一化失败（返回 None）本函数立即返回 None，
    绝不退回自己解析原始字段这条路。
    """
    raw_rows = list(rows or [])
    if source == md.SOURCE_TENCENT_INTRADAY:
        normalized = md.normalize_tencent_minute(raw_rows)
        if normalized is None:
            return None
        directions: list[Optional[str]] = []
        prev_price: Optional[float] = None
        for raw in raw_rows:
            price = _num(raw.get("price"))
            if price is None or prev_price is None:
                directions.append(None)
            elif price > prev_price:
                directions.append(DIRECTION_UP)
            elif price < prev_price:
                directions.append(DIRECTION_DOWN)
            else:
                directions.append(None)
            if price is not None:
                prev_price = price
    elif source == md.SOURCE_SINA_5MIN:
        normalized = md.normalize_sina_minute(raw_rows)
        if normalized is None:
            return None
        directions = []
        for raw in raw_rows:
            open_ = _num(raw.get("open"))
            close_ = _num(raw.get("close"))
            if open_ is None or close_ is None:
                directions.append(None)
            elif close_ > open_:
                directions.append(DIRECTION_UP)
            elif close_ < open_:
                directions.append(DIRECTION_DOWN)
            else:
                directions.append(None)
    else:
        return None
    if len(directions) != len(normalized):
        return None
    return [dict(row, direction=direction) for row, direction in zip(normalized, directions)]


def max_directional_minute_volume(
    rows: Optional[Iterable[Mapping[str, Any]]],
    *,
    source: str,
    direction: str,
    until_time: Any = None,
) -> dict[str, Any]:
    """截至 ``until_time``（含该分钟，``None``=不设上限）某方向上单分钟增量成交量的
    历史极值。

    反未来函数的唯一防线：调用方把 ``until_time`` 之后的行也喂进来，只要
    ``until_time`` 不变，返回值必须不变——见 tests/test_reverse_volume.py 的
    专门用例。方向分类见 ``_attach_direction``；成交量口径/单位换算全部来自
    minute_derived.normalize_tencent_minute / normalize_sina_minute，本函数
    不做任何原始字段解析。
    """
    if direction not in (DIRECTION_UP, DIRECTION_DOWN):
        raise ValueError(f"direction 必须是 {DIRECTION_UP!r}/{DIRECTION_DOWN!r}，收到 {direction!r}")
    tagged = _attach_direction(rows, source)
    if not tagged:
        return {"value": None, "minute": None,
                "availability": f"{md.UNAVAILABLE}:minute_rows_unavailable"}
    until: Optional[int] = None
    if until_time is not None:
        until = md.parse_minute(until_time)
        if until is None:
            return {"value": None, "minute": None,
                    "availability": f"{md.UNAVAILABLE}:bad_until_time({until_time})"}
    matches = [
        row for row in tagged
        if row.get("direction") == direction and (until is None or int(row["minute"]) <= until)
    ]
    if not matches:
        return {"value": None, "minute": None,
                "availability": f"{md.UNAVAILABLE}:no_{direction}_minute_before_cutoff"}
    best = max(matches, key=lambda row: float(row["volume_shares"]))
    return {"value": float(best["volume_shares"]), "minute": int(best["minute"]),
            "availability": md.AVAILABLE}


# =========================================================================== #
# 前提三条
# =========================================================================== #
def prior_leader_condition(
    record: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    was_leader = _bool(record.get("was_prior_period_top_leader"))
    if was_leader is None:
        return (_condition(COND_PRIOR_LEADER, None, "是否前一周期最高人气/高度股不可知"),
                ["prior_period_leader_status_missing"])
    return (_condition(COND_PRIOR_LEADER, was_leader,
                       f"前一周期最高人气/高度股={was_leader}"), [])


def drawdown_condition(
    record: Mapping[str, Any], settings: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    minimum = float(settings.get("min_drawdown_pct", 0.25))
    maximum = float(settings.get("max_drawdown_pct", 0.40))
    drawdown = _num(record.get("drawdown_pct"))
    if drawdown is None:
        return (_condition(COND_DRAWDOWN, None, "从高点回撤幅度不可用"),
                ["drawdown_pct_missing"])
    ok = minimum <= drawdown <= maximum
    return (_condition(
        COND_DRAWDOWN, ok, f"回撤{drawdown:.4f}∈[{minimum:g},{maximum:g}]={ok}"), [])


def sentiment_condition(
    market_state: Optional[Mapping[str, Any]], settings: Mapping[str, Any],
) -> dict[str, Any]:
    """大盘/情绪是否不再加速恶化。

    ``market_state`` 取 {available, deteriorating} 形状（复用既有市场状态口径的
    调用方负责把它翻译成这个布尔量，本模块不重造温度/周期判定）。不可用 → fail-closed。
    """
    state = dict(market_state or {})
    if state.get("available") is not True or _bool(state.get("deteriorating")) is None:
        return {"available": False, "stable": None, "reason": "market_sentiment_unavailable"}
    deteriorating = bool(state.get("deteriorating"))
    return {"available": True, "stable": not deteriorating,
            "reason": "sentiment_deteriorating" if deteriorating else "sentiment_stable"}


def _sentiment_condition_entry(
    market_state: Optional[Mapping[str, Any]], settings: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    result = sentiment_condition(market_state, settings)
    if not result["available"]:
        return (_condition(COND_SENTIMENT_STABLE, None, "大盘/情绪状态不可用"),
                ["market_sentiment_unavailable"])
    return (_condition(COND_SENTIMENT_STABLE, bool(result["stable"]),
                       f"情绪状态:{result['reason']}"), [])


# =========================================================================== #
# 初步观察两条
# =========================================================================== #
def volatility_contraction_condition(
    record: Mapping[str, Any], settings: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    maximum = float(settings.get("max_volatility_contraction_ratio", 0.6))
    ratio = _num(record.get("volatility_contraction_ratio"))
    if ratio is None:
        return (_condition(COND_VOLATILITY_CONTRACTION, None, "3-5日波动收窄比例不可用"),
                ["volatility_contraction_ratio_missing"])
    ok = ratio <= maximum
    return (_condition(
        COND_VOLATILITY_CONTRACTION, ok,
        f"波动收窄比例{ratio:.4f}{'≤' if ok else '>'}{maximum:g}"), [])


def volume_low_percentile_condition(
    record: Mapping[str, Any], settings: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    maximum = float(settings.get("max_volume_percentile_20d", 0.30))
    pct = _num(record.get("volume_percentile_20d"))
    if pct is None:
        return (_condition(COND_VOLUME_LOW_PERCENTILE, None, "成交量20日分位不可用"),
                ["volume_percentile_20d_missing"])
    ok = pct <= maximum
    return (_condition(
        COND_VOLUME_LOW_PERCENTILE, ok,
        f"成交量20日分位{pct:.4f}{'≤' if ok else '>'}{maximum:g}"), [])


# =========================================================================== #
# 反量确认 + 回踩确认（用的是已经算好的分钟量峰值字段，不在这里现算——
# 现算走 max_directional_minute_volume，由调用方/回测接线层在喂进 evaluate() 之前
# 完成，见模块 docstring 第1条）
# =========================================================================== #
def reversal_volume_condition(
    record: Mapping[str, Any], settings: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    ratio_min = float(settings.get("reversal_volume_ratio_min", 1.3))
    up = _num(record.get("max_up_minute_volume"))
    down_prior = _num(record.get("max_down_minute_volume_prior"))
    if up is None or down_prior is None:
        return (_condition(COND_REVERSAL_VOLUME, None, "反量确认所需分钟量峰值不可用"),
                ["reversal_volume_evidence_missing"])
    if down_prior <= 0:
        return (_condition(COND_REVERSAL_VOLUME, None, "此前最大下跌分钟量非正，比值不可判定"),
                ["max_down_minute_volume_prior_non_positive"])
    ratio = up / down_prior
    ok = ratio >= ratio_min
    return (_condition(
        COND_REVERSAL_VOLUME, ok,
        f"上攻/此前下跌分钟量峰值={ratio:.4f}{'≥' if ok else '<'}{ratio_min:g}"), [])


def pullback_shrink_condition(
    record: Mapping[str, Any], settings: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    down_prior = _num(record.get("max_down_minute_volume_prior"))
    pullback_down = _num(record.get("pullback_max_down_minute_volume"))
    if down_prior is None or pullback_down is None:
        return (_condition(COND_PULLBACK_SHRINK, None, "回踩期下跌分钟量峰值不可用"),
                ["pullback_volume_evidence_missing"])
    ok = pullback_down < down_prior
    return (_condition(
        COND_PULLBACK_SHRINK, ok,
        f"回踩下跌分钟量峰值{pullback_down:g}{'<' if ok else '≥'}此前{down_prior:g}"), [])


# =========================================================================== #
# 单标的入场信号（时间序列信号，无需同题材peer组——回撤/反量都是标的自身历史）
# =========================================================================== #
def evaluate(
    record: Mapping[str, Any],
    *,
    market_state: Optional[Mapping[str, Any]] = None,
    cfg: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """对单个（code, date）候选判 S5。

    返回 {schema, status, conditions[], reasons[]}。
    status ∈ signal / no_signal / unavailable —— 缺任一必需证据一律 unavailable。
    ``suggested_entry_position_pct`` 只是把方案原文的"10%观察仓"抄进结果供解释，
    不是可执行仓位——真实仓位一律由 recommendation_audit.position_guidance 决定，
    NON-LIVE 状态下恒为 0。
    """
    settings = dict(cfg if cfg is not None else config())
    code = str(record.get("code") or "")
    reasons: list[str] = []

    leader_cond, leader_reasons = prior_leader_condition(record)
    drawdown_cond, drawdown_reasons = drawdown_condition(record, settings)
    sentiment_cond, sentiment_reasons = _sentiment_condition_entry(market_state, settings)
    contraction_cond, contraction_reasons = volatility_contraction_condition(record, settings)
    volume_pct_cond, volume_pct_reasons = volume_low_percentile_condition(record, settings)
    reversal_cond, reversal_reasons = reversal_volume_condition(record, settings)
    pullback_cond, pullback_reasons = pullback_shrink_condition(record, settings)

    conditions = [
        leader_cond, drawdown_cond, sentiment_cond, contraction_cond,
        volume_pct_cond, reversal_cond, pullback_cond,
    ]
    reasons.extend(leader_reasons)
    reasons.extend(drawdown_reasons)
    reasons.extend(sentiment_reasons)
    reasons.extend(contraction_reasons)
    reasons.extend(volume_pct_reasons)
    reasons.extend(reversal_reasons)
    reasons.extend(pullback_reasons)
    seen: set[str] = set()
    deduped_reasons = [r for r in reasons if not (r in seen or seen.add(r))]

    if deduped_reasons:
        status = STATUS_UNAVAILABLE
    elif all(c["ok"] for c in conditions):
        status = STATUS_SIGNAL
    else:
        status = STATUS_NO_SIGNAL

    return {
        "schema": SCHEMA,
        "code": code,
        "date": record.get("date"),
        "status": status,
        "conditions": conditions,
        "reasons": deduped_reasons,
        "degraded": [],
        "suggested_entry_position_pct": float(settings.get("entry_position_pct", 0.10))
        if status == STATUS_SIGNAL else None,
        "influences_live_ranking": False,
        "note": "S5 未在 strategy_registry 注册；输出仅供研究/回测，不得进入实盘排序或仓位；"
                "反量比值(1.3-1.5/1.5)来自单一历史案例的工程化，未经样本外验证",
    }


def evaluate_universe(
    records: Sequence[Mapping[str, Any]],
    *,
    market_state: Optional[Mapping[str, Any]] = None,
    cfg: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """逐条评估（S5 无需按题材分组——回撤/反量都是标的自身的时间序列证据）。"""
    settings = dict(cfg if cfg is not None else config())
    return [evaluate(r, market_state=market_state, cfg=settings) for r in records or []]


def signal_codes(results: Iterable[Mapping[str, Any]]) -> set[tuple[str, str]]:
    """命中集合 {(code, date)}，供回测按事件键筛选。"""
    return {
        (str(r.get("code") or ""), str(r.get("date") or ""))
        for r in results if r.get("status") == STATUS_SIGNAL
    }


def summarize(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """状态分布 + unavailable 原因计数（零样本时必须能看出是缺哪类数据）。"""
    counts = {STATUS_SIGNAL: 0, STATUS_NO_SIGNAL: 0, STATUS_UNAVAILABLE: 0}
    reasons: dict[str, int] = {}
    for result in results or []:
        counts[str(result.get("status"))] = counts.get(str(result.get("status")), 0) + 1
        for reason in result.get("reasons") or []:
            key = str(reason).split("(")[0]
            reasons[key] = reasons.get(key, 0) + 1
    return {"schema": SCHEMA, "total": len(results or []), "status_counts": counts,
            "unavailable_reasons": dict(sorted(reasons.items()))}


# =========================================================================== #
# 二次确认（加仓）—— 与入场七条件完全独立评估，只有入场已经是 signal 时才有意义，
# 但函数本身不强制检查这一点（同 S3 exit_signal 与 entry 的独立性原则）
# =========================================================================== #
SECOND_CONFIRMATION_SCHEMA = "reverse_volume_second_confirmation_v1"


def second_ratio_condition(
    record: Mapping[str, Any], settings: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    ratio_min = float(settings.get("reversal_volume_ratio_second_min", 1.5))
    up = _num(record.get("second_max_up_minute_volume"))
    down = _num(record.get("pullback_max_down_minute_volume"))
    if up is None or down is None:
        return (_condition(COND2_RATIO, None, "二次确认所需分钟量峰值不可用"),
                ["second_confirmation_volume_evidence_missing"])
    if down <= 0:
        return (_condition(COND2_RATIO, None, "回踩下跌分钟量峰值非正，比值不可判定"),
                ["pullback_max_down_minute_volume_non_positive"])
    ratio = up / down
    ok = ratio >= ratio_min
    return (_condition(
        COND2_RATIO, ok,
        f"二次上攻/回踩下跌分钟量峰值={ratio:.4f}{'≥' if ok else '<'}{ratio_min:g}"), [])


def breakout_condition(
    record: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    breakout = _bool(record.get("breakout_above_balance_zone"))
    if breakout is None:
        return (_condition(COND2_BREAKOUT, None, "是否突破短期平衡区不可知"),
                ["breakout_above_balance_zone_missing"])
    return (_condition(COND2_BREAKOUT, breakout, f"突破短期平衡区={breakout}"), [])


def second_confirmation(
    record: Mapping[str, Any],
    *,
    cfg: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """二次确认（加仓 20%-30%）：反量比值 ≥1.5 ∧ 价格突破短期平衡区。

    ``suggested_add_position_pct_min/max`` 同 evaluate() 的 suggested_entry_
    position_pct，只是把方案原文抄进结果供解释，不是可执行仓位。
    """
    settings = dict(cfg if cfg is not None else config())
    ratio_cond, ratio_reasons = second_ratio_condition(record, settings)
    breakout_cond, breakout_reasons = breakout_condition(record)
    conditions = [ratio_cond, breakout_cond]
    reasons = list(ratio_reasons) + list(breakout_reasons)

    if reasons:
        status = STATUS_UNAVAILABLE
    elif all(c["ok"] for c in conditions):
        status = STATUS_SIGNAL
    else:
        status = STATUS_NO_SIGNAL

    return {
        "schema": SECOND_CONFIRMATION_SCHEMA,
        "code": str(record.get("code") or ""),
        "date": record.get("date"),
        "status": status,
        "conditions": conditions,
        "reasons": reasons,
        "suggested_add_position_pct_min": float(settings.get("second_confirmation_position_pct_min", 0.20))
        if status == STATUS_SIGNAL else None,
        "suggested_add_position_pct_max": float(settings.get("second_confirmation_position_pct_max", 0.30))
        if status == STATUS_SIGNAL else None,
        "influences_live_ranking": False,
        "note": "S5 二次确认未在 strategy_registry 注册；比值1.5来自单一历史案例，未经样本外验证",
    }


__all__ = [
    "SCHEMA", "SECOND_CONFIRMATION_SCHEMA",
    "STATUS_SIGNAL", "STATUS_NO_SIGNAL", "STATUS_UNAVAILABLE",
    "DIRECTION_UP", "DIRECTION_DOWN",
    "COND_PRIOR_LEADER", "COND_DRAWDOWN", "COND_SENTIMENT_STABLE",
    "COND_VOLATILITY_CONTRACTION", "COND_VOLUME_LOW_PERCENTILE",
    "COND_REVERSAL_VOLUME", "COND_PULLBACK_SHRINK", "CONDITION_IDS",
    "COND2_RATIO", "COND2_BREAKOUT", "SECOND_CONFIRMATION_CONDITION_IDS",
    "config", "max_directional_minute_volume",
    "prior_leader_condition", "drawdown_condition", "sentiment_condition",
    "volatility_contraction_condition", "volume_low_percentile_condition",
    "reversal_volume_condition", "pullback_shrink_condition",
    "evaluate", "evaluate_universe", "signal_codes", "summarize",
    "second_ratio_condition", "breakout_condition", "second_confirmation",
]
