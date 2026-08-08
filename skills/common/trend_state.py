"""纯函数趋势状态分类器。

这个模块只负责把候选特征解释成趋势状态，不负责选股、下单或读写运行时
状态。特征名同时兼容候选流水线的字段名和 ``trend_follow_v2`` 可能使用的
语义化字段名；缺失特征按中性处理，而不会被当成趋势证据。
"""

from __future__ import annotations

from enum import Enum
from math import isfinite, tanh
from typing import Any, Dict, Mapping, Optional


class TrendState(str, Enum):
    """趋势状态，按状态机的四个基本阶段划分。"""

    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    SIDEWAYS = "SIDEWAYS"
    REVERSING = "REVERSING"


# Convenience exports for callers that prefer module-level constants.
TRENDING_UP = TrendState.TRENDING_UP
TRENDING_DOWN = TrendState.TRENDING_DOWN
SIDEWAYS = TrendState.SIDEWAYS
REVERSING = TrendState.REVERSING


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _first(features: Mapping[str, Any], *names: str) -> Optional[float]:
    for name in names:
        if name in features:
            value = _number(features.get(name))
            if value is not None:
                return value
    return None


def _bool_or_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    return _number(value)


def _signed_signal(value: Optional[float], scale: float) -> float:
    """Map a signed feature to [-1, 1], accepting fractions or percentages."""
    if value is None:
        return 0.0
    # Candidate data commonly stores returns as percentages (5.0), while
    # research features often store them as fractions (0.05).
    effective_scale = scale if abs(value) <= 1.0 else scale * 100.0
    return tanh(value / effective_scale)


def _efficiency(features: Mapping[str, Any]) -> float:
    value = _first(features, "trend_efficiency", "trend_efficiency_20d", "efficiency_ratio")
    if value is not None:
        # Efficiency ratios are normally [0, 1]; also accept a percentage.
        if value > 1.0:
            value /= 100.0
        return max(0.0, min(1.0, abs(value)))

    # A conservative fallback from fields already emitted by candidate_pipeline.
    # This is a proxy only: it is deliberately lower confidence than an explicit
    # efficiency ratio and should be replaced by a true path-efficiency feature.
    momentum = _first(features, "momentum_20d", "momentum_20d_raw")
    volatility = _first(features, "volatility_20d")
    if momentum is None or volatility is None or volatility < 0:
        return 0.0
    denominator = abs(momentum) + max(volatility, 0.01) * 20.0**0.5
    return max(0.0, min(1.0, abs(momentum) / denominator)) if denominator else 0.0


def _persistence(features: Mapping[str, Any]) -> float:
    value = None
    for name in ("breakout_persistence", "breakout_duration", "breakout_days", "breakout_20d"):
        if name in features:
            value = _bool_or_number(features.get(name))
            if value is not None:
                break
    if value is None:
        return 0.0
    if 0.0 <= value <= 1.0:
        return value
    return max(0.0, min(1.0, value / 5.0))


def _confirmation(features: Mapping[str, Any]) -> float:
    combined = None
    for name in ("volume_vwap_confirmation", "volume_price_confirmation", "price_volume_confirmation"):
        if name in features:
            combined = _bool_or_number(features.get(name))
            if combined is not None:
                break
    if combined is not None:
        if abs(combined) <= 1.0:
            return max(-1.0, min(1.0, combined))
        return _signed_signal(combined, 1.0)

    parts = []
    for name in ("volume_confirmation", "vwap_confirmation"):
        if name in features:
            value = _bool_or_number(features.get(name))
            if value is not None:
                parts.append(max(-1.0, min(1.0, value)))
    if parts:
        return sum(parts) / len(parts)

    # A volume ratio above one is weak confirmation; absence remains neutral.
    ratio = _first(features, "volume_ratio_5d", "volume_ratio")
    if ratio is not None:
        return max(-1.0, min(1.0, (ratio - 1.0) / 0.5))
    return 0.0


def classify(features: Mapping[str, Any]) -> Dict[str, Any]:
    """Classify candidate features into a trend state and confidence.

    ``confidence`` is in [0, 1].  Strong, efficient long-term evidence that is
    aligned with short-term inertia becomes ``TRENDING_UP`` or
    ``TRENDING_DOWN``.  A high-quality long-term trend with opposing inertia is
    ``REVERSING``; weak or conflicting evidence is ``SIDEWAYS``.
    """
    if not isinstance(features, Mapping):
        raise TypeError("features must be a mapping")

    efficiency = _efficiency(features)
    ma20 = _signed_signal(_first(features, "ma20_slope", "ma20_slope_pct", "slope_ma20"), 0.02)
    ma60 = _signed_signal(_first(features, "ma60_slope", "ma60_slope_pct", "slope_ma60"), 0.02)
    slope = 0.6 * ma20 + 0.4 * ma60
    residual = _signed_signal(
        _first(
            features,
            "industry_residual_momentum",
            "momentum_20d_resid",
            "industry_relative_strength",
            "industry_rs",
        ),
        0.05,
    )
    persistence = _persistence(features)
    breakout_direction = _first(features, "breakout_direction")
    breakout_sign = -1.0 if breakout_direction is not None and breakout_direction < 0 else 1.0
    confirmation = _confirmation(features)
    inertia = _signed_signal(
        _first(features, "short_term_inertia", "short_momentum", "momentum_5d", "momentum_5d_raw"),
        0.05,
    )

    # Long-term direction excludes short inertia so that a pullback can be
    # recognised as reversing instead of silently relabelled as sideways.
    long_direction = (
        0.40 * slope
        + 0.30 * residual
        + 0.15 * breakout_sign * persistence
        + 0.15 * confirmation
    )
    quality = (
        0.50 * efficiency
        + 0.20 * abs(slope)
        + 0.15 * abs(residual)
        + 0.10 * persistence
        + 0.05 * abs(confirmation)
    )
    high_quality = efficiency >= 0.45 and quality >= 0.42 and abs(long_direction) >= 0.30

    if high_quality and long_direction >= 0.30 and inertia >= 0.05:
        state = TrendState.TRENDING_UP
    elif high_quality and long_direction <= -0.30 and inertia <= -0.05:
        state = TrendState.TRENDING_DOWN
    elif high_quality and (
        (long_direction >= 0.30 and inertia <= -0.15)
        or (long_direction <= -0.30 and inertia >= 0.15)
    ):
        state = TrendState.REVERSING
    else:
        state = TrendState.SIDEWAYS

    alignment = max(0.0, 1.0 - abs(long_direction - inertia) / 2.0)
    confidence = max(0.0, min(1.0, 0.55 * quality + 0.30 * abs(long_direction) + 0.15 * alignment))
    if state is TrendState.SIDEWAYS:
        confidence = max(0.0, min(1.0, 1.0 - confidence))
    return {
        "state": state,
        "confidence": round(confidence, 4),
        "trend_efficiency": round(efficiency, 4),
        "long_direction": round(long_direction, 4),
        "short_term_inertia": round(inertia, 4),
        "trend_quality": round(quality, 4),
    }


__all__ = [
    "REVERSING",
    "SIDEWAYS",
    "TRENDING_DOWN",
    "TRENDING_UP",
    "TrendState",
    "classify",
]
