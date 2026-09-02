"""板块价格类因子 —— RS / 超额动量 / RS 斜率 / 广度（RESEARCH ONLY）。

**未经任何真实数据验证。** 这些因子是按用户决定「清单上的都要做完、不等验证」
在 2026-09-02 一次性建起来的，没有等 ``sector-crowding-daily`` 的前向样本先证明
拥挤度那条有区分度。因此产物一律 ``live_effect: none``、无消费端；任何人想把它
接进排序，先补前向验证，别拿「代码已经在仓库里」当成「结论已经成立」。

基准口径：全 A 中位数，不是指数
================================
报告用「行业指数 / 沪深300」。本仓 ``theme_strength`` 用的是**全 A 中位数**收益，
并显式拒绝指数代理 —— 那是更强的口径（指数被权重股绑架，中位数不会），本模块沿用
它，不倒退回指数。

同一条口径下 ``sector_momentum`` 的 5 日比较用的是上证指数：它跑在盘中、手里只有
东财板块快照，算不出全 A 中位数。那不是等价基准，因此在它的产物里显式标注为
degraded，不允许两套口径混着当同一个东西读。

四个因子
========
- ``rs``               行业等权指数 / 基准指数（两者都由日收益累乘得到）
- ``excess_momentum``  N 日行业累计收益 − N 日基准累计收益
- ``rs_slope``         log(RS) 在最近 N 日上的 OLS 斜率
- ``breadth``          收盘价站上 MA20 的成分股占比

Fail-closed（每一条都对应一种「缺数据被读成信号」）
==================================================
- 基准中位数需要足够多的样本，不足则当日**没有基准**，跨过它的窗口整体不可得；
  绝不把缺失日当成「当日基准收益为 0」。
- 广度的分母为空时返回不可得，**不是** 0.0 —— 0.0 广度读起来是「板块极弱」，
  那是一个凭空造出来的强信号（空集给出好看数字，是本仓黑名单里的老坑）。
- 窗口内缺任何一天即不可得，不做插值、不缩窗口。
"""

from __future__ import annotations

from math import log
from statistics import median
from typing import Any, Mapping, Sequence

SCHEMA = "sector_price_factors_v1"
UNAVAILABLE = "unavailable"

CONFIG_SECTION = "sector_price_factors"

#: 基准口径标签。canonical = 全 A 中位数；degraded = 指数代理。
BASIS_WHOLE_A_MEDIAN = "whole_a_median"
BASIS_INDEX_DEGRADED = "index_degraded"

DEFAULTS: dict[str, Any] = {
    "ma_window": 20,
    # 一个真正的全 A 基准需要足够宽的横截面；沿用 theme_strength 的 100 只下限。
    "min_market_samples": 100,
    # 板块当日有效成分少于此值：等权收益与广度都没有意义。
    "min_members_observed": 5,
    # 报告用 250 日中期动量；本地缓存深度下限 180 个交易日，因此主窗口取 60，
    # 120 作为可得时的长窗口。这不是「等价的短窗口」，产物里标出实际窗口。
    "momentum_windows": [20, 60, 120],
    "rs_slope_windows": [20, 60],
}


def load_config(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """读取 ``config/scoring.yaml`` 的 ``sector_price_factors`` 节，缺项回落 DEFAULTS。"""
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
        key: list(value) if isinstance(value, list) else value
        for key, value in DEFAULTS.items()
    }
    if isinstance(section, Mapping):
        for key, value in section.items():
            if key in resolved:
                resolved[key] = list(value) if isinstance(value, Sequence) and not isinstance(value, str) else value
    return resolved


def _num(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def market_median_return(bars: Sequence[Mapping[str, Any]], *, min_samples: int) -> float | None:
    """全 A 中位数日收益（小数，非百分比）。样本不足返回 ``None``。

    样本不足必须是 ``None``：拿十几只票的中位数当「全市场」，比没有基准更糟。
    """
    changes = [value / 100.0 for value in (_num(row.get("pct_chg")) for row in bars) if value is not None]
    if len(changes) < int(min_samples):
        return None
    return round(median(changes), 6)


def build_daily_series(
    day_bars: Sequence[tuple[str, Sequence[Mapping[str, Any]]]],
    membership: Mapping[str, str],
    *,
    config: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """按交易日升序的 ``(日期, 当日全市场日线)`` → 每日的板块等权收益与广度。

    单趟扫描，逐 code 维护最近 ``ma_window`` 根收盘价，因此不需要把整段历史一次性
    读进内存，也不需要第二次查询。
    """
    settings = {**DEFAULTS, **dict(config or {})}
    ma_window = int(settings["ma_window"])
    min_samples = int(settings["min_market_samples"])

    closes_history: dict[str, list[float]] = {}
    series: list[dict[str, Any]] = []

    for trading_date, bars in day_bars:
        basis = market_median_return(bars, min_samples=min_samples)
        sectors: dict[str, dict[str, Any]] = {}
        for row in bars:
            code = str(row.get("code") or "")
            sector = str(membership.get(code, "") or "").strip()
            close = _num(row.get("close"))
            change = _num(row.get("pct_chg"))

            window = closes_history.setdefault(code, [])
            # MA 用**含今日**的最近 ma_window 根：站上 MA20 是当日事实。
            if close is not None:
                window.append(close)
                del window[:-ma_window]

            if not sector:
                continue
            entry = sectors.setdefault(
                sector,
                {"member_count": 0, "returns": [], "above_ma": 0, "valid_ma": 0},
            )
            entry["member_count"] += 1
            if change is not None:
                entry["returns"].append(change / 100.0)
            if close is not None and len(window) == ma_window:
                entry["valid_ma"] += 1
                if close > sum(window) / ma_window:
                    entry["above_ma"] += 1

        series.append(
            {
                "schema": SCHEMA,
                "trading_date": trading_date,
                "market_median_return": basis,
                "market_basis": BASIS_WHOLE_A_MEDIAN if basis is not None else UNAVAILABLE,
                "observed_codes": len(bars),
                "sectors": {
                    sector: {
                        "member_count": entry["member_count"],
                        # 收益样本为空 -> None，绝不折叠成 0.0（那是「板块平盘」）
                        "equal_weight_return": (
                            round(sum(entry["returns"]) / len(entry["returns"]), 6)
                            if entry["returns"] else None
                        ),
                        "return_samples": len(entry["returns"]),
                        "above_ma": entry["above_ma"],
                        "valid_ma": entry["valid_ma"],
                    }
                    for sector, entry in sectors.items()
                },
            }
        )
    return series


def breadth(day_sector: Mapping[str, Any], *, min_members: int) -> float | None:
    """站上 MA20 的成分占比（0-1）。分母为空或过小返回 ``None``，**不是** 0.0。"""
    valid = int(day_sector.get("valid_ma") or 0)
    if valid < int(min_members):
        return None
    return round(int(day_sector.get("above_ma") or 0) / valid, 4)


def _window_returns(
    series: Sequence[Mapping[str, Any]], sector: str, window: int
) -> tuple[list[float], list[float]] | None:
    """窗口内 ``(板块日收益, 基准日收益)``。缺任何一天即 ``None``（不插值、不缩窗）。"""
    if len(series) < window:
        return None
    sector_returns: list[float] = []
    market_returns: list[float] = []
    for day in series[-window:]:
        basis = _num(day.get("market_median_return"))
        entry = (day.get("sectors") or {}).get(sector) or {}
        value = _num(entry.get("equal_weight_return"))
        if basis is None or value is None:
            return None
        sector_returns.append(value)
        market_returns.append(basis)
    return sector_returns, market_returns


def _compound(returns: Sequence[float]) -> float:
    total = 1.0
    for value in returns:
        total *= 1.0 + value
    return total


def excess_momentum(
    series: Sequence[Mapping[str, Any]], sector: str, window: int
) -> float | None:
    """N 日行业累计收益 − N 日基准累计收益。窗口不完整返回 ``None``。"""
    pair = _window_returns(series, sector, window)
    if pair is None:
        return None
    sector_returns, market_returns = pair
    return round(_compound(sector_returns) - _compound(market_returns), 6)


def rs_slope(
    series: Sequence[Mapping[str, Any]], sector: str, window: int
) -> float | None:
    """log(RS) 在窗口上的 OLS 斜率。RS 由两条日收益累乘序列相除得到。"""
    pair = _window_returns(series, sector, window)
    if pair is None:
        return None
    sector_returns, market_returns = pair

    logs: list[float] = []
    sector_level = 1.0
    market_level = 1.0
    for sector_return, market_return in zip(sector_returns, market_returns):
        sector_level *= 1.0 + sector_return
        market_level *= 1.0 + market_return
        if sector_level <= 0 or market_level <= 0:
            return None
        logs.append(log(sector_level / market_level))

    n = len(logs)
    mean_x = (n - 1) / 2.0
    mean_y = sum(logs) / n
    denominator = sum((index - mean_x) ** 2 for index in range(n))
    if denominator == 0:
        return None
    numerator = sum((index - mean_x) * (value - mean_y) for index, value in enumerate(logs))
    return round(numerator / denominator, 8)


def sector_price_factors(
    series: Sequence[Mapping[str, Any]],
    sector: str,
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """单个板块的四类价格因子。缺数据一律 ``None``，不补默认值。"""
    settings = {**DEFAULTS, **dict(config or {})}
    today = series[-1] if series else {}
    entry = (today.get("sectors") or {}).get(sector) or {}
    member_count = int(entry.get("member_count") or 0)

    base: dict[str, Any] = {
        "schema": SCHEMA,
        "sector": sector,
        "member_count": member_count,
        "market_basis": today.get("market_basis") or UNAVAILABLE,
    }
    if member_count < int(settings["min_members_observed"]):
        return {**base, "status": UNAVAILABLE,
                "reason": f"当日有效成分 {member_count} 少于下限 {settings['min_members_observed']}"}

    momentum = {
        f"excess_momentum_{window}d": excess_momentum(series, sector, window)
        for window in settings["momentum_windows"]
    }
    slopes = {
        f"rs_slope_{window}d": rs_slope(series, sector, window)
        for window in settings["rs_slope_windows"]
    }
    breadth_value = breadth(entry, min_members=int(settings["min_members_observed"]))

    values = {**momentum, **slopes, "breadth_ma20": breadth_value}
    if all(value is None for value in values.values()):
        return {**base, "status": UNAVAILABLE, **values,
                "reason": "四类因子全部不可得（窗口不完整或基准缺失）"}
    return {**base, "status": "ok", **values,
            "unavailable_factors": sorted(name for name, value in values.items() if value is None)}


def build_sector_price_factors(
    series: Sequence[Mapping[str, Any]],
    *,
    asof: str,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """整段序列 → 全部板块的价格因子产物。"""
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
        sector_price_factors(series, sector, config=settings)
        for sector in sorted((series[-1].get("sectors") or {}))
    ]
    usable = [row for row in rows if row["status"] == "ok"]
    return {
        "schema": SCHEMA,
        "asof": asof,
        "status": "ok" if usable else UNAVAILABLE,
        # 与 sector_crowding 同一条纪律：用今天的行业归属重建过去，只能是探索性重建。
        "evidence_qualification": "exploratory_reconstruction",
        "live_effect": "none",
        "validated": False,
        "market_basis": series[-1].get("market_basis") or UNAVAILABLE,
        "sessions": len(series),
        "sector_count": len(rows),
        "scored_count": len(usable),
        "unavailable_count": len(rows) - len(usable),
        "sectors": rows,
    }
