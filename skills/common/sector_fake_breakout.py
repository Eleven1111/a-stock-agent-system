"""板块假突破风险 FakeBreakoutRisk 0-100（RESEARCH ONLY，**未经真实数据验证**）。

与 [[sector_price_factors]] 同批建起：按「清单上的都要做完、不等验证」的决定，
没有等前向样本先证明拥挤度/价格因子有区分度。产物带 ``validated: false`` /
``live_effect: none``，无消费端。

报告的九个子项，我们能算六个
============================
| 子项 | 报告权重 | 本仓 |
| --- | ---: | --- |
| 突破持续性不足 | 15 | ✅ 板块等权指数是否守住突破位 |
| 量价背离 | 10 | ✅ AmountZ20 高但 PriceEfficiency5 低 |
| 成交集中度 | 10 | ✅ Top-N 成交占比的历史分位 |
| 广度不足 | 12 | ✅ breadth < 50% 或突破以来下降 |
| 盈利预期背离 | 12 | ❌ 无一致预期数据源 |
| 景气背离 | 12 | ❌ 无中观景气数据源 |
| 高位拥挤 | 12 | ✅ 复用 sector_crowding 的分数 |
| 龙头/行业背离 | 7 | ✅ 剔除成交前三后的板块收益显著更弱 |
| 新闻/政策依赖 | 10 | ❌ 本链路无结构化事件输入 |

缺的三项**不参与加权**：权重在可得子项上重新归一，并把缺哪几项写进产物。
补零会让「没有基本面数据」看起来像「基本面没有背离」—— 那是给假突破发放行证。

一条来自报告、必须守住的时序纪律
================================
**不能用「突破后三天发生了什么」来决定三天前是否买入。** 突破持续性只能随时间
逐日更新：每个决策点只看该点为止已经发生的信息。本模块的所有子项都只读
``series[:t+1]``，没有任何一项回看未来窗口。
"""

from __future__ import annotations

from statistics import mean, pstdev
from typing import Any, Mapping, Sequence

SCHEMA = "sector_fake_breakout_v1"
UNAVAILABLE = "unavailable"

CONFIG_SECTION = "sector_fake_breakout"

#: 报告九子项原权重。缺失项在 :func:`_renormalise` 里剔除并把权重摊回可得项 ——
#: 保留原值是为了让「我们改了什么」一眼可查。
REPORT_WEIGHTS: dict[str, float] = {
    "breakout_durability": 15.0,
    "volume_price_divergence": 10.0,
    "turnover_concentration": 10.0,
    "breadth_deficit": 12.0,
    "earnings_divergence": 12.0,     # 无数据源，恒不可得
    "prosperity_divergence": 12.0,   # 无数据源，恒不可得
    "high_crowding": 12.0,
    "leader_divergence": 7.0,
    "news_dependence": 10.0,         # 无结构化事件输入，恒不可得
}

AVAILABLE_SUBRISKS = (
    "breakout_durability",
    "volume_price_divergence",
    "turnover_concentration",
    "breadth_deficit",
    "high_crowding",
    "leader_divergence",
)

DEFAULTS: dict[str, Any] = {
    "breakout_window": 60,
    "amount_z_window": 20,
    "amount_z_threshold": 2.0,
    "price_efficiency_window": 5,
    "price_efficiency_threshold": 0.25,
    "breadth_floor": 0.50,
    "breadth_drop_threshold": 0.10,
    "concentration_percentile_threshold": 80.0,
    "crowding_threshold": 70.0,
    "leader_divergence_gap": 0.02,
    "min_history_sessions": 61,
}


def load_config(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """读取 ``config/scoring.yaml`` 的 ``sector_fake_breakout`` 节，缺项回落 DEFAULTS。"""
    if payload is None:
        try:
            import yaml
            from config_registry import config_path

            with open(config_path("scoring"), encoding="utf-8") as handle:
                payload = yaml.safe_load(handle)
        except (OSError, ValueError, ImportError):
            payload = None
    section = (payload or {}).get(CONFIG_SECTION) if isinstance(payload, Mapping) else None
    resolved = dict(DEFAULTS)
    if isinstance(section, Mapping):
        for key, value in section.items():
            if key in resolved:
                resolved[key] = value
    return resolved


def _num(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def sector_index_levels(
    series: Sequence[Mapping[str, Any]], sector: str
) -> list[float] | None:
    """板块等权指数（日收益累乘，起点 1.0）。缺任何一天返回 ``None``。"""
    levels: list[float] = []
    level = 1.0
    for day in series:
        value = _num(((day.get("sectors") or {}).get(sector) or {}).get("equal_weight_return"))
        if value is None:
            return None
        level *= 1.0 + value
        levels.append(level)
    return levels


def price_efficiency(levels: Sequence[float], window: int) -> float | None:
    """``|C_t − C_{t−N}| / Σ|ΔC|``：净位移占总行程的比例，越低越像来回震荡。"""
    if len(levels) < window + 1:
        return None
    segment = list(levels[-(window + 1):])
    path = sum(abs(segment[index + 1] - segment[index]) for index in range(len(segment) - 1))
    if path <= 0:
        return None
    return round(abs(segment[-1] - segment[0]) / path, 6)


def amount_zscore(amounts: Sequence[float], window: int) -> float | None:
    """当日成交额相对前 ``window`` 日的 z 分。历史不足或零方差返回 ``None``。"""
    if len(amounts) < window + 1:
        return None
    history = list(amounts[-(window + 1):-1])
    spread = pstdev(history)
    if spread <= 0:
        return None
    return round((amounts[-1] - mean(history)) / spread, 6)


def _breakout_state(levels: Sequence[float], window: int) -> dict[str, Any]:
    """突破判定与突破以来的表现（只读到当日为止，绝不回看未来）。"""
    if len(levels) < window + 1:
        return {"status": UNAVAILABLE, "reason": f"历史不足 {window + 1} 日"}
    level = levels[-1]
    break_level = max(levels[-(window + 1):-1])
    if level <= break_level:
        return {"status": "ok", "broke_out": False, "break_level": break_level,
                "level": level}
    # 突破以来的天数：从后往前数还在突破位之上的连续交易日
    days_since = 0
    for value in reversed(levels[:-1]):
        if value > break_level:
            days_since += 1
        else:
            break
    return {"status": "ok", "broke_out": True, "break_level": break_level,
            "level": level, "days_since_breakout": days_since}


def _renormalise(subrisks: Mapping[str, float | None]) -> tuple[float | None, dict[str, float]]:
    usable = {name: value for name, value in subrisks.items() if value is not None}
    if not usable:
        return None, {}
    total = sum(REPORT_WEIGHTS[name] for name in usable)
    weights = {name: REPORT_WEIGHTS[name] / total for name in usable}
    return round(100.0 * sum(weights[name] * usable[name] for name in usable), 2), weights


def _series_subrisks(
    levels: Sequence[float],
    breakout: Mapping[str, Any],
    settings: Mapping[str, Any],
    amounts: Sequence[float] | None,
) -> tuple[float | None, float | None, dict[str, float]]:
    """两个由板块自身价量序列决定的子项。返回 ``(量价效率, 成交额 z, 子项)``。"""
    subrisks: dict[str, float] = {}

    # 1. 突破持续性：曾经站上过突破位、现在又掉回来 = 满风险；从没突破过不算。
    if breakout.get("status") == "ok":
        if breakout["broke_out"]:
            subrisks["breakout_durability"] = 0.0
        else:
            recent_high = max(levels[:-1][-int(settings["breakout_window"]):])
            subrisks["breakout_durability"] = (
                1.0
                if levels[-1] < recent_high
                and any(value > recent_high * 0.999 for value in levels[-5:-1])
                else 0.0
            )

    # 2. 量价背离：放量但净位移很小。
    efficiency = price_efficiency(levels, int(settings["price_efficiency_window"]))
    # 成交额是**另一条**输入通道，长度对不上就是未来数据泄漏的入口：调用方若把
    # 完整序列的 amounts 喂给一个被截断的 series，z 分会读到决策点之后的成交额。
    # 因此长度不等即判该子项不可得，绝不静默截断（截断会把调用方的错误藏起来）。
    aligned = list(amounts or [])
    if aligned and len(aligned) != len(levels):
        return efficiency, None, subrisks
    z_score = amount_zscore(aligned, int(settings["amount_z_window"]))
    if efficiency is not None and z_score is not None:
        subrisks["volume_price_divergence"] = (
            1.0
            if z_score > float(settings["amount_z_threshold"])
            and efficiency < float(settings["price_efficiency_threshold"])
            else 0.0
        )
    return efficiency, z_score, subrisks


def _threshold_subrisks(
    settings: Mapping[str, Any],
    *,
    breadth_today: float | None,
    breadth_prior: float | None,
    concentration_percentile: float | None,
    crowding_score: float | None,
) -> dict[str, float]:
    """三个由外部指标直接映射的子项。输入缺失即该项不出现（保持不可得）。"""
    subrisks: dict[str, float] = {}

    # 3. 成交集中度：历史分位越高越危险，线性映射到 [0,1]。
    if concentration_percentile is not None:
        threshold = float(settings["concentration_percentile_threshold"])
        subrisks["turnover_concentration"] = (
            round(min(1.0, max(0.0, (concentration_percentile - threshold)
                               / (100.0 - threshold))), 4)
            if threshold < 100.0 else 0.0
        )

    # 4. 广度不足：低于地板，或相对突破前明显下滑。
    if breadth_today is not None:
        deficit = 1.0 if breadth_today < float(settings["breadth_floor"]) else 0.0
        if breadth_prior is not None and (breadth_prior - breadth_today) > float(
            settings["breadth_drop_threshold"]
        ):
            deficit = 1.0
        subrisks["breadth_deficit"] = deficit

    # 7. 高位拥挤：拥挤分越过阈值后非线性抬升。
    if crowding_score is not None:
        threshold = float(settings["crowding_threshold"])
        excess = max(0.0, crowding_score - threshold) / max(1e-9, 100.0 - threshold)
        subrisks["high_crowding"] = round(min(1.0, excess ** 2), 4)

    return subrisks


def sector_fake_breakout_risk(
    series: Sequence[Mapping[str, Any]],
    sector: str,
    *,
    breadth_today: float | None = None,
    breadth_prior: float | None = None,
    concentration_percentile: float | None = None,
    crowding_score: float | None = None,
    amounts: Sequence[float] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """单板块的 FakeBreakoutRisk。每个子项独立可得/不可得，绝不补默认值。"""
    settings = {**DEFAULTS, **dict(config or {})}
    levels = sector_index_levels(series, sector)
    base: dict[str, Any] = {"schema": SCHEMA, "sector": sector}

    if levels is None or len(levels) < int(settings["min_history_sessions"]):
        return {**base, "status": UNAVAILABLE, "risk": None,
                "reason": f"板块等权指数序列不足 {settings['min_history_sessions']} 日或存在缺口"}

    breakout = _breakout_state(levels, int(settings["breakout_window"]))
    subrisks: dict[str, float | None] = {name: None for name in REPORT_WEIGHTS}

    efficiency, z_score, series_subrisks = _series_subrisks(
        levels, breakout, settings, amounts
    )
    subrisks.update(series_subrisks)
    subrisks.update(
        _threshold_subrisks(
            settings,
            breadth_today=breadth_today,
            breadth_prior=breadth_prior,
            concentration_percentile=concentration_percentile,
            crowding_score=crowding_score,
        )
    )

    # 8. 龙头/行业背离：剔除成交前三后，后排明显更弱。
    today = series[-1]
    entry = (today.get("sectors") or {}).get(sector) or {}
    full_return = _num(entry.get("equal_weight_return"))
    ex_top_return = _num(entry.get("ex_top_return"))
    if full_return is not None and ex_top_return is not None:
        subrisks["leader_divergence"] = (
            1.0 if (full_return - ex_top_return) > float(settings["leader_divergence_gap"]) else 0.0
        )

    risk, weights = _renormalise(subrisks)
    if risk is None:
        return {**base, "status": UNAVAILABLE, "risk": None, "subrisks": subrisks,
                "reason": "六个可得子项全部缺失"}

    return {
        **base,
        "status": "ok",
        "risk": risk,
        "broke_out": breakout.get("broke_out"),
        "days_since_breakout": breakout.get("days_since_breakout"),
        "price_efficiency": efficiency,
        "amount_z": z_score,
        "subrisks": subrisks,
        "applied_weights": {name: round(weight, 4) for name, weight in weights.items()},
        "missing_subrisks": sorted(set(REPORT_WEIGHTS) - set(weights)),
    }


def build_sector_fake_breakout(
    series: Sequence[Mapping[str, Any]],
    *,
    asof: str,
    breadth_by_sector: Mapping[str, float] | None = None,
    breadth_prior_by_sector: Mapping[str, float] | None = None,
    concentration_percentiles: Mapping[str, float] | None = None,
    crowding_scores: Mapping[str, float] | None = None,
    amounts_by_sector: Mapping[str, Sequence[float]] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """整段序列 → 全部板块的假突破风险产物。"""
    settings = {**DEFAULTS, **dict(config or {})}
    if not series or str(series[-1].get("trading_date")) != str(asof):
        return {
            "schema": SCHEMA,
            "asof": asof,
            "status": UNAVAILABLE,
            "reason": "序列为空或最后一天不是 asof —— 不拿旧日期冒充当日",
            "sectors": [],
        }

    rows = [
        sector_fake_breakout_risk(
            series,
            sector,
            breadth_today=(breadth_by_sector or {}).get(sector),
            breadth_prior=(breadth_prior_by_sector or {}).get(sector),
            concentration_percentile=(concentration_percentiles or {}).get(sector),
            crowding_score=(crowding_scores or {}).get(sector),
            amounts=(amounts_by_sector or {}).get(sector),
            config=settings,
        )
        for sector in sorted((series[-1].get("sectors") or {}))
    ]
    usable = [row for row in rows if row["status"] == "ok"]
    return {
        "schema": SCHEMA,
        "asof": asof,
        "status": "ok" if usable else UNAVAILABLE,
        "evidence_qualification": "exploratory_reconstruction",
        "live_effect": "none",
        "validated": False,
        "sessions": len(series),
        "sector_count": len(rows),
        "scored_count": len(usable),
        "unavailable_count": len(rows) - len(usable),
        "sectors": rows,
    }
