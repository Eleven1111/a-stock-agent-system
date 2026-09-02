"""板块三池 + 多标签 regime + 合成分（RESEARCH ONLY，**未经真实数据验证**）。

与 [[sector_price_factors]] / [[sector_fake_breakout]] 同批建起，产物带
``validated: false`` / ``live_effect: none``，无消费端。

⚠️ 合成分只覆盖报告权重的 41%
==============================
报告的 RotationStartScore 是九项加权，其中**五项我们没有数据源**：

| 分项 | 报告权重 | 本仓 |
| --- | ---: | --- |
| 景气 prosperity | 0.20 | ❌ 无中观景气源 |
| 盈利上修 earnings | 0.14 | ❌ 无一致预期源 |
| 资金 flow | 0.12 | ❌ 无行业口径 ETF 份额/两融 |
| 相对强弱 RS | 0.13 | ✅ |
| 广度 breadth | 0.11 | ✅ |
| 估值赔率 valuation | 0.08 | ❌ 无行业估值源 |
| 反拥挤 anti-crowding | 0.08 | ✅ |
| 反假突破 anti-fake | 0.09 | ✅ |
| regime 契合 | 0.05 | ❌ 需先有可比 regime 定义 |

也就是说**能算的四项只占 0.41**，而且全是价量。报告反复强调「启动策略优先回答
基本面是否在改善，而不是哪个行业最近涨得最多」—— 在缺掉景气/盈利/资金/估值之后，
把剩下的四项归一得到的分数，恰恰就是报告警告的那种纯价量动量分。

因此：``missing_weight_share`` 直接写进产物，合成分**恒标 low confidence**，
且三池判定不是单看这个分数，而是要求条件共振（报告 A/B/C 三套规则也是共振而非
单一分数）。别把这个分数当成 RotationStartScore 的实现 —— 它是它的一个残片。

权重路径纪律（B3）
==================
专家规则 → 滚动 IC → ML，顺序不可颠倒。本模块只做第一步：权重取报告原值并在
可得项上归一，**没有任何拟合**。要进第二步（IC 加权），必须复用既有的 shadow
lane 纪律（``config/scoring.yaml`` 的 ``weight_shadow``：60 个拟合交易日 + 60 个
未见 OOS、不自动晋级），不得看结果回拟合。

三池
====
- ``avoid``      规避：拥挤状态不放行，或假突破风险过高
- ``mainline``   主线：RS 斜率为正 + 广度达标 + 不拥挤 + 假突破低（四重共振）
- ``watch``      观察：数据齐全但共振不足
- ``unavailable`` 数据不足 —— **不是** watch。缺数据不能产生一个可以被跟进的标签。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

SCHEMA = "sector_rotation_pools_v1"
UNAVAILABLE = "unavailable"

CONFIG_SECTION = "sector_rotation_pools"

#: 报告 RotationStartScore 九项原权重。缺项在 :func:`_renormalise` 里剔除。
REPORT_WEIGHTS: dict[str, float] = {
    "prosperity": 0.20,          # 无数据源
    "earnings": 0.14,            # 无数据源
    "flow": 0.12,                # 无数据源
    "relative_strength": 0.13,
    "breadth": 0.11,
    "valuation_odds": 0.08,      # 无数据源
    "anti_crowding": 0.08,
    "anti_fake_breakout": 0.09,
    "regime_fit": 0.05,          # 无可比 regime 定义
}

AVAILABLE_COMPONENTS = (
    "relative_strength",
    "breadth",
    "anti_crowding",
    "anti_fake_breakout",
)

#: 可得权重占比，写死在这里是为了让「只有 41%」这件事出现在测试断言里而不只是注释。
AVAILABLE_WEIGHT_SHARE = round(
    sum(REPORT_WEIGHTS[name] for name in AVAILABLE_COMPONENTS), 4
)

DEFAULTS: dict[str, Any] = {
    # 主线四重共振
    "mainline_rs_slope_min": 0.0,
    "mainline_breadth_min": 0.55,
    "mainline_max_fake_risk": 40.0,
    "mainline_min_score": 60.0,
    # 规避
    "avoid_fake_risk": 55.0,
    # RS 斜率映射到 0-100 的满量程（log-RS 的日斜率，1e-3 已是很陡的趋势）
    "rs_slope_full_scale": 0.002,
    "rs_slope_window": 20,
    # 市场级拥挤的多标签阈值（B2：只加一个维度，不新建分类器）
    "market_crowding_elevated": 0.60,
    "market_crowding_extreme": 0.75,
}


def load_config(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """读取 ``config/scoring.yaml`` 的 ``sector_rotation_pools`` 节，缺项回落 DEFAULTS。"""
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


def _renormalise(scores: Mapping[str, float | None]) -> tuple[float | None, dict[str, float]]:
    usable = {name: value for name, value in scores.items() if value is not None}
    if not usable:
        return None, {}
    total = sum(REPORT_WEIGHTS[name] for name in usable)
    weights = {name: REPORT_WEIGHTS[name] / total for name in usable}
    return round(sum(weights[name] * usable[name] for name in usable), 2), weights


def market_crowding_labels(
    crowding_score: float | None,
    *,
    extra_labels: Sequence[str] = (),
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """B2：把**既有**的市场级拥挤分转成一个 regime 标签，与调用方给的其它标签并列。

    刻意不新建分类器：本仓已有 ``market_cycle_state`` / ``market_temperature`` /
    ``index_trend_gate`` 三套状态判定，再加一套只会制造互相矛盾的口径。这里只补
    「拥挤」这一个此前没有进 regime 的维度，并允许多标签共存（报告 bkld 的口径：
    某一天可以同时是 TrendUp + HighCrowding）。
    """
    settings = {**DEFAULTS, **dict(config or {})}
    labels = [str(label) for label in extra_labels if str(label).strip()]
    if crowding_score is None:
        return {"labels": labels, "market_crowding": UNAVAILABLE,
                "reason": "市场级拥挤分不可得"}
    if crowding_score >= float(settings["market_crowding_extreme"]):
        tag = "EXTREME_CROWDING"
    elif crowding_score >= float(settings["market_crowding_elevated"]):
        tag = "ELEVATED_CROWDING"
    else:
        tag = "NORMAL_CROWDING"
    return {"labels": labels + [tag], "market_crowding": tag, "reason": ""}


def _rs_component(rs_slope: float | None, settings: Mapping[str, Any]) -> float | None:
    """log-RS 日斜率 → 0-100，双向对称。缺失返回 None（不补 50）。"""
    if rs_slope is None:
        return None
    full_scale = float(settings["rs_slope_full_scale"])
    if full_scale <= 0:
        return None
    ratio = max(-1.0, min(1.0, rs_slope / full_scale))
    return round(50.0 + 50.0 * ratio, 2)


def sector_pool(
    sector: str,
    *,
    rs_slope: float | None,
    breadth: float | None,
    crowding_score: float | None,
    crowding_state: str | None,
    fake_risk: float | None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """单板块的合成分与池归属。数据不足 → ``unavailable``，绝不落进 watch。"""
    settings = {**DEFAULTS, **dict(config or {})}
    components: dict[str, float | None] = {
        "relative_strength": _rs_component(rs_slope, settings),
        "breadth": None if breadth is None else round(100.0 * breadth, 2),
        "anti_crowding": None if crowding_score is None else round(100.0 - crowding_score, 2),
        "anti_fake_breakout": None if fake_risk is None else round(100.0 - fake_risk, 2),
    }
    score, weights = _renormalise(components)
    base: dict[str, Any] = {
        "schema": SCHEMA,
        "sector": sector,
        "components": components,
        "missing_components": sorted(set(REPORT_WEIGHTS) - set(weights)),
        "missing_weight_share": round(1.0 - AVAILABLE_WEIGHT_SHARE, 4),
        # 只覆盖 41% 的权重、且全是价量 —— 置信度恒低，不因为数据齐全就抬高。
        "confidence": "low",
    }
    if score is None:
        return {**base, "status": UNAVAILABLE, "pool": UNAVAILABLE, "score": None,
                "reason": "四个可得分项全部缺失"}

    base = {**base, "score": score,
            "applied_weights": {name: round(weight, 4) for name, weight in weights.items()}}

    # 规避优先判定：拥挤状态不放行 或 假突破过高。任一命中即规避，不看合成分。
    blocking_states = {"NO_ADD", "EXIT_RISK", "COOLDOWN", UNAVAILABLE}
    if crowding_state in blocking_states:
        return {**base, "status": "ok", "pool": "avoid",
                "reason": f"拥挤状态 {crowding_state} 不放行"}
    if fake_risk is not None and fake_risk >= float(settings["avoid_fake_risk"]):
        return {**base, "status": "ok", "pool": "avoid",
                "reason": f"假突破风险 {fake_risk:.1f} ≥ {settings['avoid_fake_risk']}"}

    # 主线要求四重共振：任何一项**不可得**都不算满足（缺证据 ≠ 满足）。
    confirmations = {
        "rs_slope": rs_slope is not None and rs_slope > float(settings["mainline_rs_slope_min"]),
        "breadth": breadth is not None and breadth >= float(settings["mainline_breadth_min"]),
        "fake_risk": fake_risk is not None and fake_risk < float(settings["mainline_max_fake_risk"]),
        "score": score >= float(settings["mainline_min_score"]),
    }
    if all(confirmations.values()):
        return {**base, "status": "ok", "pool": "mainline",
                "confirmations": confirmations, "reason": "四重共振"}
    return {**base, "status": "ok", "pool": "watch", "confirmations": confirmations,
            "reason": "共振不足"}


def build_sector_rotation_pools(
    sectors: Sequence[str],
    *,
    asof: str,
    price_factors: Mapping[str, Mapping[str, Any]] | None = None,
    crowding: Mapping[str, Mapping[str, Any]] | None = None,
    fake_breakout: Mapping[str, Mapping[str, Any]] | None = None,
    market_crowding_score: float | None = None,
    regime_labels: Sequence[str] = (),
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把三份已算好的产物合成板块三池。不重算任何一个因子。"""
    settings = {**DEFAULTS, **dict(config or {})}
    window = int(settings["rs_slope_window"])
    rows: list[dict[str, Any]] = []
    for sector in sorted(set(sectors)):
        price = (price_factors or {}).get(sector) or {}
        crowd = (crowding or {}).get(sector) or {}
        fake = (fake_breakout or {}).get(sector) or {}
        rows.append(
            sector_pool(
                sector,
                rs_slope=_num(price.get(f"rs_slope_{window}d")),
                breadth=_num(price.get("breadth_ma20")),
                crowding_score=_num(crowd.get("score")),
                crowding_state=str(crowd.get("state") or "") or None,
                fake_risk=_num(fake.get("risk")),
                config=settings,
            )
        )

    pools: dict[str, list[str]] = {"mainline": [], "watch": [], "avoid": [], UNAVAILABLE: []}
    for row in rows:
        pools[row["pool"]].append(row["sector"])

    return {
        "schema": SCHEMA,
        "asof": asof,
        "status": "ok" if any(row["status"] == "ok" for row in rows) else UNAVAILABLE,
        "evidence_qualification": "exploratory_reconstruction",
        "live_effect": "none",
        "validated": False,
        "confidence": "low",
        "available_weight_share": AVAILABLE_WEIGHT_SHARE,
        "missing_weight_share": round(1.0 - AVAILABLE_WEIGHT_SHARE, 4),
        "weight_path": "expert_only_no_fitting",
        "regime": market_crowding_labels(
            market_crowding_score, extra_labels=regime_labels, config=settings
        ),
        "pools": pools,
        "sector_count": len(rows),
        "sectors": rows,
    }
