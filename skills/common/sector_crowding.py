"""板块拥挤度 —— 研究口径的「别追已经一致的板块」减分信号（RESEARCH ONLY）。

为什么只做拥挤这一项
====================
两份行业轮动调研报告的共同核心结论是：**动量必须配拥挤度反向项**，否则就是一台
回撤机器。而本仓现有的板块信号（``sector_momentum``）只有加分方向 —— 涨得多加分、
已经跌下来才小幅扣分，**没有一条「涨得多但内部不扩散、成交高度集中」的扣分路径**。
本模块补的就是那条路径，不是把报告里的周频行业 ETF 轮动策略搬进来。

三个维度（报告的五维里我们能算的那三个）
========================================
- ``turnover_share``      行业成交额 / 全市场成交额，取历史分位
- ``relative_turnover``   行业平均换手 / 全市场平均换手，取历史分位
- ``concentration``       Top-N 成交占比与 HHI 的合成，取历史分位

报告的另外两维（ETF 份额流 + 两融的资金拥挤、PE/PB 的估值拥挤）本仓没有行业口径
的数据源，因此**不参与加权**：权重在可得分量上重新归一，并把缺哪一维写进产物。
静默补零会让缺数据的板块看起来「不拥挤」。

与报告的一处**故意不同**：coverage 折扣的方向
==============================================
报告对景气分用 ``Raw × min(1, sqrt(n_valid/n_required))`` 做覆盖率折扣，防止数据
丰富的行业系统性得高分。那套折扣对「高分=好」的分数是保守的，但**拥挤分是反的
——高分=危险**。同一个折扣会把数据稀疏的板块压向 0，读起来就是「不拥挤、可以
追」，正好把保守方向做反。

所以这里不折扣分值，而是把覆盖率单独表达为 ``confidence``，并让状态机在置信度
不足时输出 ``unavailable`` 而不是 ``NORMAL``：**缺数据永远不能产生一个放行的结论。**

点时口径
========
历史分位需要过去 N 个交易日的板块聚合，而行业归属的变更日志（``industry_map``）
是从 2026-09 才开始积累的，更早的日子只能用**今天的**归属去重建。因此本模块产出
的分位标注为 ``exploratory_reconstruction``：可以做诊断与观察，不能进 research
gate、不能生成订单、不能改实盘权重。等归属历史覆盖整个窗口后才谈 canonical。
"""

from __future__ import annotations

from statistics import mean
from typing import Any, Mapping, Sequence

SCHEMA = "sector_crowding_v1"
UNAVAILABLE = "unavailable"

CONFIG_SECTION = "sector_crowding"

#: 报告给的五维权重原值。我们只有其中三维，剩下两维在 :func:`_renormalise` 里
#: 被剔除并把权重按比例摊回 —— 保留原值是为了让「我们改了什么」一眼可查：
#: 权重不是我们调的，是报告权重去掉不可得分量后的归一化结果。
REPORT_WEIGHTS: dict[str, float] = {
    "turnover_share": 0.25,
    "relative_turnover": 0.20,
    "capital_crowding": 0.20,      # ETF 份额流 + 两融：本仓无行业口径，恒不可得
    "valuation_crowding": 0.15,    # PE/PB/PS 行业分位：本仓无数据源，恒不可得
    "concentration": 0.20,
}

AVAILABLE_COMPONENTS = ("turnover_share", "relative_turnover", "concentration")

DEFAULTS: dict[str, Any] = {
    # 本地日线缓存的研究深度下限是 180 个交易日，因此窗口取 120 而不是报告的 756。
    # 这不是「等价的短窗口」，是我们只有这么多历史 —— 产物里标 low_confidence。
    "percentile_window": 120,
    "min_percentile_samples": 60,
    # 当日板块内有效成分股少于此值：分位没有意义，判 unavailable。
    "min_members_observed": 5,
    # 覆盖率 = 当日观测到的成分数 / 该板块登记成分数；低于此值整体判 unavailable，
    # 绝不给一个「看起来不拥挤」的分数。
    "min_member_coverage": 0.60,
    "top_concentration_n": 5,
    "states": {"watch": 70.0, "no_add": 80.0, "exit_risk": 90.0},
    "cooldown_sessions": 3,
    "reentry_max_score": 75.0,
}


def load_config(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """读取 ``config/scoring.yaml`` 的 ``sector_crowding`` 节，缺项回落 DEFAULTS。"""
    if payload is None:
        try:
            import yaml
            from config_registry import config_path

            with open(config_path("scoring"), encoding="utf-8") as handle:
                payload = yaml.safe_load(handle)
        except (OSError, ValueError, ImportError):
            payload = None
    section = (payload or {}).get(CONFIG_SECTION) if isinstance(payload, Mapping) else None
    resolved = {
        key: dict(value) if isinstance(value, dict) else value
        for key, value in DEFAULTS.items()
    }
    if isinstance(section, Mapping):
        for key, value in section.items():
            if key in resolved:
                resolved[key] = dict(value) if isinstance(value, Mapping) else value
    return resolved


def _num(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number  # NaN 不是数字，别让它继续往下走


def aggregate_sector_day(
    bars: Sequence[Mapping[str, Any]],
    membership: Mapping[str, str],
    *,
    trading_date: str,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """一个交易日的全市场日线 → 每个板块的成交/换手/集中度聚合。

    只统计**当日真的有成交额**的成分股。停牌股在缓存里没有行，把它们算进分母会
    让集中度虚高；把它们算作 0 成交则会让「板块很集中」和「半个板块停牌」混成
    同一个数。
    """
    settings = dict(config or DEFAULTS)
    top_n = int(settings["top_concentration_n"])

    market_amount = 0.0
    market_turns: list[float] = []
    by_sector: dict[str, list[tuple[float, float]]] = {}
    for row in bars:
        amount = _num(row.get("amount"))
        turn = _num(row.get("turn"))
        if amount is None or amount <= 0:
            continue
        market_amount += amount
        if turn is not None:
            market_turns.append(turn)
        sector = str(membership.get(str(row.get("code")) or "", "") or "").strip()
        if sector:
            by_sector.setdefault(sector, []).append((amount, turn if turn is not None else float("nan")))

    sectors: dict[str, Any] = {}
    for sector, members in by_sector.items():
        amounts = sorted((amount for amount, _turn in members), reverse=True)
        turns = [turn for _amount, turn in members if turn == turn]
        total = sum(amounts)
        if total <= 0:
            continue
        hhi = sum((amount / total) ** 2 for amount in amounts)
        sectors[sector] = {
            "member_count": len(amounts),
            "amount": round(total, 2),
            "turnover_share": total / market_amount if market_amount > 0 else None,
            "mean_turn": mean(turns) if turns else None,
            "top_concentration": sum(amounts[:top_n]) / total,
            "amount_hhi": hhi,
        }

    return {
        "schema": SCHEMA,
        "trading_date": trading_date,
        "market_amount": round(market_amount, 2),
        "market_mean_turn": mean(market_turns) if market_turns else None,
        "observed_codes": sum(1 for _ in market_turns) or len(bars),
        "sectors": sectors,
    }


def percentile_rank(history: Sequence[float], value: float, *, min_samples: int) -> float | None:
    """``value`` 在 ``history`` 中的百分位（0-100）。样本不足返回 ``None``。

    样本不足必须是 ``None`` 而不是 50 —— 一个「中性」默认值会让没有历史的板块
    静默获得一个可以参与加权的数字。
    """
    samples = [float(item) for item in history if item is not None]
    if len(samples) < int(min_samples):
        return None
    below = sum(1 for item in samples if item < value)
    equal = sum(1 for item in samples if item == value)
    return round(100.0 * (below + 0.5 * equal) / len(samples), 2)


def _renormalise(scores: Mapping[str, float | None]) -> tuple[float | None, dict[str, float]]:
    """在**可得**分量上重新归一报告权重；一个都不可得则返回 ``None``。"""
    usable = {name: value for name, value in scores.items() if value is not None}
    if not usable:
        return None, {}
    total = sum(REPORT_WEIGHTS[name] for name in usable)
    weights = {name: REPORT_WEIGHTS[name] / total for name in usable}
    return round(sum(weights[name] * usable[name] for name in usable), 2), weights


def sector_crowding_score(
    sector: str,
    history: Sequence[Mapping[str, Any]],
    today: Mapping[str, Any],
    *,
    registered_members: int | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """单个板块的拥挤分。

    ``history`` 是该板块**过去**若干交易日的聚合（不含今天），``today`` 是当日聚合。
    返回恒带 ``status``：``ok`` 时才有 ``score``。
    """
    settings = dict(config or DEFAULTS)
    member_count = int(today.get("member_count") or 0)
    coverage = (
        member_count / registered_members
        if registered_members and registered_members > 0
        else None
    )

    base = {
        "schema": SCHEMA,
        "sector": sector,
        "member_count": member_count,
        "member_coverage": round(coverage, 4) if coverage is not None else None,
        "history_samples": len(history),
    }

    if member_count < int(settings["min_members_observed"]):
        return {**base, "status": UNAVAILABLE, "score": None,
                "reason": f"当日有效成分 {member_count} 少于下限 {settings['min_members_observed']}"}
    if coverage is not None and coverage < float(settings["min_member_coverage"]):
        return {**base, "status": UNAVAILABLE, "score": None,
                "reason": f"成分覆盖 {coverage:.0%} 低于下限 {float(settings['min_member_coverage']):.0%}"}

    min_samples = int(settings["min_percentile_samples"])
    concentration_today = _concentration_metric(today)
    components = {
        "turnover_share": percentile_rank(
            [value for value in (_num(day.get("turnover_share")) for day in history) if value is not None],
            _num(today.get("turnover_share")) if _num(today.get("turnover_share")) is not None else 0.0,
            min_samples=min_samples,
        ) if _num(today.get("turnover_share")) is not None else None,
        "relative_turnover": percentile_rank(
            [value for value in (_relative_turnover(day) for day in history) if value is not None],
            _relative_turnover(today) if _relative_turnover(today) is not None else 0.0,
            min_samples=min_samples,
        ) if _relative_turnover(today) is not None else None,
        "concentration": percentile_rank(
            [value for value in (_concentration_metric(day) for day in history) if value is not None],
            concentration_today if concentration_today is not None else 0.0,
            min_samples=min_samples,
        ) if concentration_today is not None else None,
    }

    score, weights = _renormalise(components)
    if score is None:
        return {**base, "status": UNAVAILABLE, "score": None, "components": components,
                "reason": f"三个分量都不可得（历史样本 {len(history)}，下限 {min_samples}）"}

    return {
        **base,
        "status": "ok",
        "score": score,
        "components": components,
        "applied_weights": {name: round(weight, 4) for name, weight in weights.items()},
        "missing_components": sorted(set(REPORT_WEIGHTS) - set(weights)),
        "confidence": _confidence(coverage, len(history), settings),
    }


def _relative_turnover(day: Mapping[str, Any]) -> float | None:
    sector_turn = _num(day.get("mean_turn"))
    market_turn = _num(day.get("market_mean_turn"))
    if sector_turn is None or market_turn is None or market_turn <= 0:
        return None
    return sector_turn / market_turn


def _concentration_metric(day: Mapping[str, Any]) -> float | None:
    """Top-N 成交占比 0.6 + HHI 0.4（报告 C5 的权重）。任一缺失即不可得。"""
    top = _num(day.get("top_concentration"))
    hhi = _num(day.get("amount_hhi"))
    if top is None or hhi is None:
        return None
    return 0.6 * top + 0.4 * hhi


def _confidence(coverage: float | None, samples: int, settings: Mapping[str, Any]) -> str:
    """置信度只影响**解读**，绝不折扣分值（见模块 docstring 的方向性说明）。"""
    window = int(settings["percentile_window"])
    if samples < window or (coverage is not None and coverage < 0.9):
        return "low"
    return "medium"


def crowding_state(
    score: float | None,
    *,
    prior_state: str | None = None,
    sessions_since_exit: int | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """拥挤度状态机 + 冷却期。

    ``score is None``（不可得）返回 ``unavailable`` 而**不是** ``NORMAL`` ——
    缺数据不能产生放行结论，这是本模块与报告的关键差别所在。
    """
    settings = dict(config or DEFAULTS)
    thresholds = dict(settings["states"])
    if score is None:
        return {"state": UNAVAILABLE, "allow_new_entry": False,
                "reason": "拥挤度不可得，按不放行处理"}

    if score >= float(thresholds["exit_risk"]):
        state = "EXIT_RISK"
    elif score >= float(thresholds["no_add"]):
        state = "NO_ADD"
    elif score >= float(thresholds["watch"]):
        state = "WATCH"
    else:
        state = "NORMAL"

    allow = state in {"NORMAL", "WATCH"}
    reason = f"拥挤分 {score:.1f} → {state}"

    # 冷却：从 EXIT_RISK 退出后，既要等够交易日，也要等分数真的降下来。
    # 只等天数会让「追高→止损→次日动量又追」原样复发。
    if prior_state == "EXIT_RISK" and allow:
        cooldown = int(settings["cooldown_sessions"])
        waited = -1 if sessions_since_exit is None else int(sessions_since_exit)
        if waited < cooldown:
            return {"state": "COOLDOWN", "allow_new_entry": False,
                    "reason": f"退出后仅 {max(waited, 0)}/{cooldown} 个交易日，冷却中"}
        if score > float(settings["reentry_max_score"]):
            return {"state": "COOLDOWN", "allow_new_entry": False,
                    "reason": f"冷却已满但拥挤分 {score:.1f} 仍高于重入上限 "
                              f"{settings['reentry_max_score']}"}

    return {"state": state, "allow_new_entry": allow, "reason": reason}


def build_sector_crowding(
    series: Sequence[Mapping[str, Any]],
    *,
    asof: str,
    registered_members: Mapping[str, int] | None = None,
    prior_states: Mapping[str, Mapping[str, Any]] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """按日聚合序列（升序，最后一天 = ``asof``）→ 全部板块的拥挤度产物。

    ``series`` 的每一项是 :func:`aggregate_sector_day` 的输出。
    """
    settings = dict(config or DEFAULTS)
    if not series or str(series[-1].get("trading_date")) != str(asof):
        return {
            "schema": SCHEMA,
            "asof": asof,
            "status": UNAVAILABLE,
            "reason": "序列为空或最后一天不是 asof —— 不拿旧日期冒充当日",
            "sectors": [],
        }

    window = int(settings["percentile_window"])
    history = list(series[:-1])[-window:]
    today = series[-1]
    registered = dict(registered_members or {})
    priors = dict(prior_states or {})

    rows: list[dict[str, Any]] = []
    for sector, aggregate in sorted((today.get("sectors") or {}).items()):
        sector_history = [
            {**(day.get("sectors") or {}).get(sector, {}),
             "market_mean_turn": day.get("market_mean_turn")}
            for day in history
            if sector in (day.get("sectors") or {})
        ]
        scored = sector_crowding_score(
            sector,
            sector_history,
            {**aggregate, "market_mean_turn": today.get("market_mean_turn")},
            registered_members=registered.get(sector),
            config=settings,
        )
        prior = priors.get(sector) or {}
        state = crowding_state(
            scored.get("score"),
            prior_state=str(prior.get("state") or "") or None,
            sessions_since_exit=prior.get("sessions_since_exit"),
            config=settings,
        )
        rows.append({**scored, **{"state": state["state"],
                                  "allow_new_entry": state["allow_new_entry"],
                                  "state_reason": state["reason"]}})

    usable = [row for row in rows if row["status"] == "ok"]
    return {
        "schema": SCHEMA,
        "asof": asof,
        "status": "ok" if usable else UNAVAILABLE,
        # 历史分位用今天的行业归属重建过去，因此整份产物只能是探索性重建：
        # 不得进 research gate、不得生成订单、不得改实盘权重。
        "evidence_qualification": "exploratory_reconstruction",
        "live_effect": "none",
        "percentile_window": window,
        "history_sessions": len(history),
        "sector_count": len(rows),
        "scored_count": len(usable),
        "unavailable_count": len(rows) - len(usable),
        "sectors": rows,
    }
