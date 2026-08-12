"""Point-in-time portfolio factor and liquidity evidence for open admission."""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from portfolio_policy import portfolio_value


MIN_RETURN_OBSERVATIONS = 20
FACTOR_FIELDS = (
    "correlation",
    "beta",
    "style_exposure_pct",
    "adv_participation_pct",
    "portfolio_volatility_pct",
)

# 尾部分位数比 beta 需要更长的样本：95% VaR 在 60 个观测下才落到第 3 差的那天，
# 少于此的样本估不出尾部，只能返回 None。
MIN_TAIL_OBSERVATIONS = 60
VAR_CONFIDENCE = 0.95
# 确定性压力情景：基准单日跌幅。取值对应 A 股常见的普跌/千股跌停/极端日，
# 不做随机模拟——情景本身必须可复现、可审计。
STRESS_BENCHMARK_SHOCKS_PCT = (-3.0, -7.0, -10.0)


def normalize_code(value: Any) -> str:
    code = str(value or "").strip().lower()
    if code.startswith(("sh", "sz")):
        code = code[2:]
    return code.zfill(6)


def _prior_bars(
    bars: Sequence[Mapping[str, Any]], decision_asof: str
) -> list[dict[str, Any]]:
    cutoff = date.fromisoformat(str(decision_asof)[:10])
    result: list[dict[str, Any]] = []
    for raw in bars:
        try:
            bar_day = date.fromisoformat(str(raw.get("date") or "")[:10])
            close = float(raw.get("close"))
        except (TypeError, ValueError):
            continue
        if bar_day >= cutoff or close <= 0 or not math.isfinite(close):
            continue
        result.append({**dict(raw), "date": bar_day.isoformat(), "close": close})
    return sorted(result, key=lambda item: item["date"])


def _returns(bars: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    values: dict[str, float] = {}
    previous: float | None = None
    for bar in bars:
        close = float(bar["close"])
        if previous and previous > 0:
            values[str(bar["date"])] = close / previous - 1.0
        previous = close
    return values


def _aligned(
    left: Mapping[str, float], right: Mapping[str, float]
) -> tuple[list[float], list[float]]:
    dates = sorted(set(left) & set(right))
    return [left[item] for item in dates], [right[item] for item in dates]


def _correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) < MIN_RETURN_OBSERVATIONS or len(left) != len(right):
        return None
    mean_left = statistics.fmean(left)
    mean_right = statistics.fmean(right)
    centered_left = [item - mean_left for item in left]
    centered_right = [item - mean_right for item in right]
    denominator = math.sqrt(
        sum(item * item for item in centered_left)
        * sum(item * item for item in centered_right)
    )
    if denominator <= 0:
        return None
    return sum(a * b for a, b in zip(centered_left, centered_right)) / denominator


def _beta(candidate: Mapping[str, float], benchmark: Mapping[str, float]) -> float | None:
    candidate_values, benchmark_values = _aligned(candidate, benchmark)
    if len(candidate_values) < MIN_RETURN_OBSERVATIONS:
        return None
    benchmark_mean = statistics.fmean(benchmark_values)
    candidate_mean = statistics.fmean(candidate_values)
    variance = sum((item - benchmark_mean) ** 2 for item in benchmark_values)
    if variance <= 0:
        return None
    covariance = sum(
        (candidate_item - candidate_mean) * (benchmark_item - benchmark_mean)
        for candidate_item, benchmark_item in zip(candidate_values, benchmark_values)
    )
    return covariance / variance


def _position_value(position: Mapping[str, Any]) -> float:
    try:
        if position.get("market_value") is not None:
            return max(0.0, float(position["market_value"]))
        price = float(position.get("current_price", position.get("price", position.get("cost", 0))) or 0)
        shares = float(position.get("shares") or 0)
        return max(0.0, price * shares)
    except (TypeError, ValueError):
        return 0.0


def _portfolio_returns(
    portfolio: Mapping[str, Any],
    bars_by_code: Mapping[str, Sequence[Mapping[str, Any]]],
    decision_asof: str,
) -> tuple[dict[str, float], list[str], list[str]]:
    total_assets = portfolio_value(portfolio)
    positions = [
        item for item in (portfolio.get("positions") or [])
        if isinstance(item, Mapping) and _position_value(item) > 0
    ]
    if not positions:
        return {}, [], []
    if total_assets <= 0:
        return {}, ["portfolio_value_unavailable"], []
    series: list[tuple[float, dict[str, float]]] = []
    missing: list[str] = []
    cutoffs: list[str] = []
    for position in positions:
        code = normalize_code(position.get("code"))
        prior = _prior_bars(bars_by_code.get(code, []), decision_asof)
        returns = _returns(prior)
        if len(returns) < MIN_RETURN_OBSERVATIONS:
            missing.append(f"holding_history_missing:{code}")
            continue
        series.append((_position_value(position) / total_assets, returns))
        cutoffs.append(prior[-1]["date"])
    if missing or len(series) != len(positions):
        return {}, missing, cutoffs
    dates = sorted(set.intersection(*(set(values) for _, values in series)))
    return {
        day: sum(weight * values[day] for weight, values in series)
        for day in dates
    }, [], cutoffs


def _style_exposure(
    candidate: Mapping[str, Any],
    portfolio: Mapping[str, Any],
    proposed_position_pct: float,
) -> float | None:
    sector = str(candidate.get("sector") or candidate.get("industry") or "").strip()
    total_assets = portfolio_value(portfolio)
    if not sector or total_assets <= 0:
        return None
    existing = sum(
        _position_value(item)
        for item in (portfolio.get("positions") or [])
        if isinstance(item, Mapping)
        and str(item.get("sector") or item.get("industry") or "").strip() == sector
    )
    return existing / total_assets * 100.0 + max(0.0, proposed_position_pct)


def _adv_participation(
    bars: Sequence[Mapping[str, Any]],
    *,
    proposed_trade_value: float,
) -> float | None:
    daily_values: list[float] = []
    for bar in bars[-20:]:
        try:
            amount = bar.get("amount")
            value = float(amount) if amount is not None else (
                float(bar["close"]) * float(bar["volume"]) * 100.0
            )
        except (KeyError, TypeError, ValueError):
            continue
        if value > 0 and math.isfinite(value):
            daily_values.append(value)
    if len(daily_values) < MIN_RETURN_OBSERVATIONS:
        return None
    return proposed_trade_value / statistics.fmean(daily_values) * 100.0


def _projected_volatility(
    candidate_returns: Mapping[str, float],
    portfolio_returns: Mapping[str, float],
    *,
    proposed_position_pct: float,
    has_positions: bool,
) -> float | None:
    candidate_weight = max(0.0, proposed_position_pct) / 100.0
    if has_positions:
        candidate_values, existing_values = _aligned(candidate_returns, portfolio_returns)
        if len(candidate_values) < MIN_RETURN_OBSERVATIONS:
            return None
        projected = [
            existing + candidate_weight * candidate
            for candidate, existing in zip(candidate_values, existing_values)
        ]
    else:
        projected = [candidate_weight * value for value in candidate_returns.values()]
    if len(projected) < MIN_RETURN_OBSERVATIONS:
        return None
    return statistics.pstdev(projected) * math.sqrt(252.0) * 100.0


def _factor_values(
    candidate: Mapping[str, Any],
    portfolio: Mapping[str, Any],
    *,
    candidate_bars: Sequence[Mapping[str, Any]],
    candidate_returns: Mapping[str, float],
    benchmark_returns: Mapping[str, float],
    existing_returns: Mapping[str, float],
    proposed_position_pct: float,
) -> dict[str, float | None]:
    has_positions = bool(portfolio.get("positions"))
    total_assets = portfolio_value(portfolio)
    if not has_positions:
        correlation = 0.0 if len(candidate_returns) >= MIN_RETURN_OBSERVATIONS else None
    else:
        left, right = _aligned(candidate_returns, existing_returns)
        correlation = _correlation(left, right)
    return {
        "correlation": correlation,
        "beta": _beta(candidate_returns, benchmark_returns),
        "style_exposure_pct": _style_exposure(candidate, portfolio, proposed_position_pct),
        "adv_participation_pct": _adv_participation(
            candidate_bars,
            proposed_trade_value=total_assets * proposed_position_pct / 100.0,
        ) if total_assets > 0 else None,
        "portfolio_volatility_pct": _projected_volatility(
            candidate_returns,
            existing_returns,
            proposed_position_pct=proposed_position_pct,
            has_positions=has_positions,
        ),
    }


def _oldest_required_cutoff(
    candidate_bars: Sequence[Mapping[str, Any]],
    benchmark_bars: Sequence[Mapping[str, Any]],
    holding_cutoffs: Sequence[str],
) -> str | None:
    if not candidate_bars or not benchmark_bars:
        return None
    required = [*holding_cutoffs, candidate_bars[-1]["date"], benchmark_bars[-1]["date"]]
    return min(required)


def build_candidate_evidence(
    candidate: Mapping[str, Any],
    portfolio: Mapping[str, Any],
    *,
    bars_by_code: Mapping[str, Sequence[Mapping[str, Any]]],
    benchmark_bars: Sequence[Mapping[str, Any]],
    proposed_position_pct: float,
    decision_asof: str,
) -> dict[str, Any]:
    try:
        proposed_position_pct = float(proposed_position_pct)
    except (TypeError, ValueError):
        proposed_position_pct = float("nan")
    position_valid = math.isfinite(proposed_position_pct) and proposed_position_pct >= 0
    if not position_valid:
        proposed_position_pct = 0.0
    code = normalize_code(candidate.get("code"))
    candidate_bars = _prior_bars(bars_by_code.get(code, []), decision_asof)
    benchmark_prior = _prior_bars(benchmark_bars, decision_asof)
    candidate_returns = _returns(candidate_bars)
    benchmark_returns = _returns(benchmark_prior)
    existing_returns, holding_missing, holding_cutoffs = _portfolio_returns(
        portfolio, bars_by_code, decision_asof
    )
    missing_reasons = list(holding_missing)
    if not position_valid:
        missing_reasons.append("proposed_position_invalid")
    if len(candidate_returns) < MIN_RETURN_OBSERVATIONS:
        missing_reasons.append("candidate_history_missing")
    if len(benchmark_returns) < MIN_RETURN_OBSERVATIONS:
        missing_reasons.append("benchmark_history_missing")

    values = _factor_values(
        candidate,
        portfolio,
        candidate_bars=candidate_bars,
        candidate_returns=candidate_returns,
        benchmark_returns=benchmark_returns,
        existing_returns=existing_returns,
        proposed_position_pct=proposed_position_pct,
    )
    if not position_valid:
        for field in ("style_exposure_pct", "adv_participation_pct", "portfolio_volatility_pct"):
            values[field] = None
    values = {
        field: value if value is None or math.isfinite(value) else None
        for field, value in values.items()
    }
    for field, value in values.items():
        if value is None:
            missing_reasons.append(f"{field.removesuffix('_pct')}_missing")
    data_cutoff = _oldest_required_cutoff(
        candidate_bars, benchmark_prior, holding_cutoffs
    )
    return {
        "schema": "portfolio_risk_evidence_v2",
        "asof": str(decision_asof)[:10],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_cutoff": data_cutoff,
        "source": "tencent_https_daily_kline_v1",
        "proposed_position_pct": round(max(0.0, proposed_position_pct), 4),
        "coverage": round(sum(value is not None for value in values.values()) / len(FACTOR_FIELDS), 4),
        **{
            key: round(value, 6) if isinstance(value, float) else value
            for key, value in values.items()
        },
        "missing_reasons": list(dict.fromkeys(missing_reasons)),
        "lookback_observations": len(candidate_returns),
        "assumptions": [
            "tencent_daily_volume_is_lots_when_amount_is_unavailable",
            "style_exposure_uses_sector_as_the_runtime_style_proxy",
        ],
    }


def _historical_var_cvar(
    returns: Sequence[float], *, confidence: float = VAR_CONFIDENCE
) -> tuple[float, float] | None:
    """历史模拟法 VaR/CVaR，返回**正数损失百分比**；样本不足返回 None。

    不拟合分布：直接取经验分位。95% 置信下 tail_size = floor(0.05 * n)，
    VaR 为第 tail_size 差的那天，CVaR 为这 tail_size 天的均值。
    """
    ordered = sorted(float(value) for value in returns if math.isfinite(value))
    if len(ordered) < MIN_TAIL_OBSERVATIONS:
        return None
    tail_size = max(1, int(math.floor((1.0 - confidence) * len(ordered))))
    threshold = ordered[tail_size - 1]
    tail_mean = statistics.fmean(ordered[:tail_size])
    return -threshold * 100.0, -tail_mean * 100.0


def _stress_scenarios(portfolio_beta: float | None) -> list[dict[str, Any]]:
    """基准冲击 × 组合 beta 的确定性传导；beta 未知时不编造数字。"""
    if portfolio_beta is None or not math.isfinite(portfolio_beta):
        return []
    return [
        {
            "scenario": f"benchmark_{abs(shock):g}pct_single_day_drop",
            "benchmark_shock_pct": shock,
            "projected_portfolio_pct": round(portfolio_beta * shock, 4),
        }
        for shock in STRESS_BENCHMARK_SHOCKS_PCT
    ]


def build_tail_risk_evidence(
    portfolio: Mapping[str, Any],
    *,
    bars_by_code: Mapping[str, Sequence[Mapping[str, Any]]],
    benchmark_bars: Sequence[Mapping[str, Any]],
    decision_asof: str,
) -> dict[str, Any]:
    """组合层尾部风险证据：历史 VaR/CVaR + 确定性压力情景。

    只产出证据，不参与准入否决——风险度量与准入门槛分开改，避免一次改动
    同时移动测量尺和判定线。样本不足时字段为 None 并写明原因，不返回 0.0。
    """
    portfolio_returns, missing, cutoffs = _portfolio_returns(
        portfolio, bars_by_code, decision_asof
    )
    benchmark_returns = _returns(_prior_bars(benchmark_bars, decision_asof))
    reasons = list(missing)
    if not portfolio_returns:
        reasons.append("portfolio_history_missing")
    if len(benchmark_returns) < MIN_RETURN_OBSERVATIONS:
        reasons.append("benchmark_history_missing")
    if len(portfolio_returns) < MIN_TAIL_OBSERVATIONS:
        reasons.append("tail_sample_insufficient")
    tail = _historical_var_cvar(list(portfolio_returns.values()))
    portfolio_beta = _beta(portfolio_returns, benchmark_returns)
    worst_day = min(portfolio_returns.values()) * 100.0 if portfolio_returns else None
    return {
        "schema": "portfolio_tail_risk_v1",
        "asof": str(decision_asof)[:10],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_cutoff": min(cutoffs) if cutoffs else None,
        "source": "tencent_https_daily_kline_v1",
        "observations": len(portfolio_returns),
        "minimum_observations": MIN_TAIL_OBSERVATIONS,
        "confidence": VAR_CONFIDENCE,
        "var_pct": round(tail[0], 4) if tail else None,
        "cvar_pct": round(tail[1], 4) if tail else None,
        "worst_observed_day_pct": round(worst_day, 4) if worst_day is not None else None,
        "portfolio_beta": round(portfolio_beta, 6) if portfolio_beta is not None else None,
        "stress_scenarios": _stress_scenarios(portfolio_beta),
        "missing_reasons": list(dict.fromkeys(reasons)),
        "advisory_only": True,
        "assumptions": [
            "historical_simulation_without_a_fitted_distribution",
            "stress_transmission_is_beta_only_and_ignores_idiosyncratic_shocks",
            "equal_weight_of_each_observed_day_no_volatility_decay",
        ],
    }


def build_evidence_bundle(
    candidates: Sequence[Mapping[str, Any]],
    portfolio: Mapping[str, Any],
    *,
    bars_by_code: Mapping[str, Sequence[Mapping[str, Any]]],
    benchmark_bars: Sequence[Mapping[str, Any]],
    proposed_position_pct: float,
    decision_asof: str,
) -> dict[str, Any]:
    evidence_by_code = {}
    for candidate in candidates:
        code = normalize_code(candidate.get("code"))
        if code == "000000":
            continue
        plan = candidate.get("execution_plan") or {}
        try:
            candidate_position_pct = float(
                plan.get("position_pct", candidate.get("position_pct", proposed_position_pct))
                or proposed_position_pct
            )
        except (TypeError, ValueError):
            candidate_position_pct = proposed_position_pct
        evidence_by_code[code] = build_candidate_evidence(
            candidate,
            portfolio,
            bars_by_code=bars_by_code,
            benchmark_bars=benchmark_bars,
            proposed_position_pct=candidate_position_pct,
            decision_asof=decision_asof,
        )
    return {
        "schema": "portfolio_risk_evidence_batch_v1",
        "asof": str(decision_asof)[:10],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "evidence_by_code": evidence_by_code,
    }
