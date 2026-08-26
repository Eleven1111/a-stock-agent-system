#!/usr/bin/env python3
"""
S6 冰点反转（IcePointReversal）信号 — 升级方案 §6.1，NON-LIVE 研究层
=======================================================================
原书专门批评过的最容易被误用的模式：

    炸板率很高 + 涨停少 + 跌停多 = 明天抄底   ← 绝对不是

正确的阶段序列是：
    A 极端亏钱效应 → B 继续恐慌但恶化速度下降 → C 逆势活口出现
    → D 涨停溢价/涨跌比/炸板率至少两项改善 → E 活口被市场确认 + 板块扩散 → 才买入

量化谓词（四项**全部**满足才触发，缺一不可 —— 一名交易者机械在冰点打高度板、
两周大赚后连续大面回撤 30%+ 的教训就是"否极并不必然泰来"，单独"冰点"永不构成
买入许可）：

    Signal = (S_{t-1} < 20) ∧ (ΔS_t > 10) ∧ (LeaderConfirm = 1) ∧ (SectorBreadth >= 3)

本策略的成败点（务必读完再改代码）
----------------------------------
1) **S_t / ΔS 必须复用 skills/common/sentiment_score.py（P0，#267）**，本模块
   不重新实现滚动分位/加权评分——两份情绪分实现迟早分叉，而分叉后没人知道
   回测/实盘用的是哪一份。``evaluate()`` 只调用
   ``sentiment_score.compute_sentiment_score()`` 取 S_t/ΔS，调用
   ``sentiment_score.load_config()`` 取四条阈值（20/10/3 全部定义在
   config/scoring.yaml 的 sentiment_score.ice_confirm 节，本模块**没有**
   这些数字的第二份拷贝）。sentiment_score 本身已经实现了同构的
   ``ice_point_confirmed()`` 组合判定；本模块额外拆成 4 个独立 condition
   条目，是为了让"逐项去掉任一项都不触发"这条纪律可以被逐条断言，而不是为了
   重算数值。
2) **单独"冰点"（只满足 S_{t-1}<20）永不触发买入**——四项必须全部满足，
   缺任一项都不得触发，测试须覆盖"只满足极弱、其余三项都不满足"与"逐项去掉
   任一项"两类用例。
3) fail-closed 纪律：S_t 不可得（预热不足180日、config 缺失、样本为空等）
   一律 status="unavailable"，绝不当作"不是冰点"折叠成 no_signal——那是把
   "没数据"包装成"已验证的负结果"，是假绿的一种。LeaderConfirm/SectorBreadth
   证据缺失同理。
4) **前置依赖未满足**：升级方案 §6.1 明确写明 S6"依赖 P1 校准结论支持后才
   启动回测"。P1（#269，State PnL 分阶段收益归因）在本机是**零样本
   UNVERIFIED**——情绪状态是否真有区分度既未证实也未证伪。因此本模块虽然
   管道齐全，但注册条件比其他策略更严：需要 P1 先在 full 模式下产出覆盖
   样本、且分档单格 n>=30，本轮严禁注册，严禁用小样本给出胜率/PF结论。

阈值单一事实源：config/scoring.yaml 的 sentiment_score 节（含 ice_confirm 子节）。
本模块的 daban_thresholds.yaml 里**没有**新增节——新增一份阈值拷贝只会制造
"两处配置谁说了算"的分歧，risk见 CLAUDE.md 黑名单 A 组"用配置断言替代行为断言"
一条的反面教训。

红线：本模块未在 strategy_registry 注册，任何调用方都不得把它的输出折进实盘
排序、评分或仓位。升级路径见 config/strategy_packs/ice_point_reversal.yaml。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

import sentiment_score as ss

SCHEMA = "ice_point_reversal_signal_v1"

STATUS_SIGNAL = "signal"
STATUS_NO_SIGNAL = "no_signal"
STATUS_UNAVAILABLE = "unavailable"

# 四项合取条件 id —— 报告/测试按 id 逐条断言，避免用中文串匹配。
COND_PREV_SCORE_EXTREME = "prev_score_extreme_below_threshold"
COND_DELTA_IMPROVING = "delta_score_improving_above_threshold"
COND_LEADER_CONFIRM = "leader_confirm"
COND_SECTOR_BREADTH = "sector_breadth_at_least_threshold"
CONDITION_IDS = (
    COND_PREV_SCORE_EXTREME, COND_DELTA_IMPROVING,
    COND_LEADER_CONFIRM, COND_SECTOR_BREADTH,
)


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN 不是数字


def _bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    return None


def _condition(cid: str, ok: Optional[bool], detail: str) -> dict[str, Any]:
    return {"id": cid, "ok": ok, "detail": detail}


# =========================================================================== #
# 四项合取条件 —— S_t/ΔS 的数值全部来自 sentiment_score.compute_sentiment_score()
# 的返回值，本函数只做阈值比较，阈值取自 sentiment_score.load_config() 的
# ice_confirm 子节，不重复定义 20/10/3
# =========================================================================== #
def prev_score_extreme_condition(
    score: Mapping[str, Any], thresholds: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    if score.get("status") != "ok":
        return (_condition(COND_PREV_SCORE_EXTREME, None, "情绪评分S_t不可用，S_t-1无法判定"),
                ["sentiment_score_unavailable"])
    prev = _num(score.get("previous_score"))
    if prev is None:
        return (_condition(COND_PREV_SCORE_EXTREME, None, "上一日情绪分S_t-1不可得（样本不足两日）"),
                ["previous_score_missing"])
    maximum = _num(thresholds.get("prev_score_max"))
    if maximum is None:
        return (_condition(COND_PREV_SCORE_EXTREME, None, "prev_score_max阈值配置缺失"),
                ["prev_score_max_missing"])
    ok = prev < maximum
    return (_condition(
        COND_PREV_SCORE_EXTREME, ok, f"S_t-1={prev:.4f}{'<' if ok else '>='}{maximum:g}"), [])


def delta_improving_condition(
    score: Mapping[str, Any], thresholds: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    if score.get("status") != "ok":
        return (_condition(COND_DELTA_IMPROVING, None, "情绪评分S_t不可用，ΔS_t无法判定"),
                ["sentiment_score_unavailable"])
    delta = _num(score.get("delta"))
    if delta is None:
        return (_condition(COND_DELTA_IMPROVING, None, "ΔS_t不可得（样本不足三日）"),
                ["delta_missing"])
    minimum = _num(thresholds.get("delta_min"))
    if minimum is None:
        return (_condition(COND_DELTA_IMPROVING, None, "delta_min阈值配置缺失"),
                ["delta_min_missing"])
    ok = delta > minimum
    return (_condition(
        COND_DELTA_IMPROVING, ok, f"ΔS_t={delta:.4f}{'>' if ok else '<='}{minimum:g}"), [])


def leader_confirm_condition(
    leader_confirm: Any,
) -> tuple[dict[str, Any], list[str]]:
    value = _bool(leader_confirm)
    if value is None:
        return (_condition(COND_LEADER_CONFIRM, None, "逆势活口是否被市场确认不可知"),
                ["leader_confirm_missing"])
    return (_condition(COND_LEADER_CONFIRM, value, f"逆势活口确认={value}"), [])


def sector_breadth_condition(
    sector_breadth_top: Any, thresholds: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    breadth = _num(sector_breadth_top)
    if breadth is None:
        return (_condition(COND_SECTOR_BREADTH, None, "板块扩散广度不可知"),
                ["sector_breadth_missing"])
    minimum = _num(thresholds.get("sector_breadth_min"))
    if minimum is None:
        return (_condition(COND_SECTOR_BREADTH, None, "sector_breadth_min阈值配置缺失"),
                ["sector_breadth_min_missing"])
    ok = breadth >= minimum
    return (_condition(
        COND_SECTOR_BREADTH, ok, f"板块扩散广度{breadth:g}{'>=' if ok else '<'}{minimum:g}"), [])


# =========================================================================== #
# 单标的入场信号 —— 情绪评分本身是市场级状态（同一天所有候选共享同一份
# sentiment_series/leader_confirm/sector_breadth_top），同 S5 的 market_state
# 传参方式：不在每条记录里重复携带市场级证据
# =========================================================================== #
def evaluate(
    record: Mapping[str, Any],
    *,
    market_state: Optional[Mapping[str, Any]] = None,
    cfg: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """对单个（code, date）候选判 S6。

    ``market_state`` 形状：{"sentiment_series": [...按交易日升序，末日=当前日...],
    "leader_confirm": bool, "sector_breadth_top": int}。``sentiment_series``
    原样喂给 ``sentiment_score.compute_sentiment_score()``——本函数不做任何
    情绪分计算。

    返回 {schema, status, conditions[4], reasons[]}。status ∈
    signal/no_signal/unavailable；四项条件必须**全部**为 True 才是 signal，
    缺任一项证据一律 unavailable（不得折叠成 no_signal）。
    """
    settings = dict(cfg) if cfg is not None else ss.load_config()
    state = dict(market_state or {})
    code = str(record.get("code") or "")

    if not settings:
        prev_cond = _condition(COND_PREV_SCORE_EXTREME, None, "情绪评分配置(scoring.yaml)不可用")
        delta_cond = _condition(COND_DELTA_IMPROVING, None, "情绪评分配置(scoring.yaml)不可用")
        breadth_cond = _condition(COND_SECTOR_BREADTH, None, "情绪评分配置(scoring.yaml)不可用")
        leader_cond, leader_reasons = leader_confirm_condition(state.get("leader_confirm"))
        conditions = [prev_cond, delta_cond, leader_cond, breadth_cond]
        reasons = ["sentiment_score_config_missing"] + leader_reasons
        seen: set[str] = set()
        deduped = [r for r in reasons if not (r in seen or seen.add(r))]
        return _result(code, record.get("date"), STATUS_UNAVAILABLE, conditions, deduped)

    series = state.get("sentiment_series")
    score = ss.compute_sentiment_score(list(series or []), config=settings)
    thresholds = dict(settings.get("ice_confirm") or {})

    prev_cond, prev_reasons = prev_score_extreme_condition(score, thresholds)
    delta_cond, delta_reasons = delta_improving_condition(score, thresholds)
    leader_cond, leader_reasons = leader_confirm_condition(state.get("leader_confirm"))
    breadth_cond, breadth_reasons = sector_breadth_condition(state.get("sector_breadth_top"), thresholds)

    conditions = [prev_cond, delta_cond, leader_cond, breadth_cond]
    reasons = list(prev_reasons) + list(delta_reasons) + list(leader_reasons) + list(breadth_reasons)
    seen = set()
    deduped_reasons = [r for r in reasons if not (r in seen or seen.add(r))]

    if deduped_reasons:
        status = STATUS_UNAVAILABLE
    elif all(c["ok"] for c in conditions):
        status = STATUS_SIGNAL
    else:
        status = STATUS_NO_SIGNAL

    return _result(code, record.get("date"), status, conditions, deduped_reasons)


def _result(
    code: str, date: Any, status: str, conditions: list[dict[str, Any]], reasons: list[str],
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "code": code,
        "date": date,
        "status": status,
        "conditions": conditions,
        "reasons": reasons,
        "degraded": [],
        "influences_live_ranking": False,
        "note": (
            "S6 未在 strategy_registry 注册；输出仅供研究/回测，不得进入实盘排序或仓位；"
            "四项合取（S_t-1<20 ∧ ΔS_t>10 ∧ LeaderConfirm ∧ SectorBreadth>=3）缺一不可，"
            "单独冰点不构成买入许可（原书教训：机械抄底两周大赚后连续大面回撤30%+）；"
            "P1(#269)校准结论本机零样本UNVERIFIED，方案§6.1明确要求S6依赖P1结论支持后"
            "才启动回测，本轮注册条件比其他策略更严"
        ),
    }


def evaluate_universe(
    records: Sequence[Mapping[str, Any]],
    *,
    market_state: Optional[Mapping[str, Any]] = None,
    cfg: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """逐条评估（S6 的四项证据都是市场级状态，同一批候选共享同一个
    market_state；无需按题材分组）。"""
    settings = dict(cfg) if cfg is not None else ss.load_config()
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


__all__ = [
    "SCHEMA", "STATUS_SIGNAL", "STATUS_NO_SIGNAL", "STATUS_UNAVAILABLE",
    "COND_PREV_SCORE_EXTREME", "COND_DELTA_IMPROVING", "COND_LEADER_CONFIRM",
    "COND_SECTOR_BREADTH", "CONDITION_IDS",
    "prev_score_extreme_condition", "delta_improving_condition",
    "leader_confirm_condition", "sector_breadth_condition",
    "evaluate", "evaluate_universe", "signal_codes", "summarize",
]
