#!/usr/bin/env python3
"""
情绪周期确定性特征（emotion_cycle_features）— P1-C 研究信号
==============================================================
把 emotion_cycle 量化口径移植为确定性特征。受数据可得性约束（腾讯日线只有
{date,open,close,high,low,volume}，quote 只有当日单值 turnover，无换手率/
成交额时序），裁剪为 4 个特征 + 1 个合成判定：

- F1 volume_percentile_60d — volume 近 60 日时序分位 + 分档（换手率时序分位
  退化为 volume 时序分位，已采纳）
- F2 volume_spike_ratio — 当日 volume / 前 20 日均量
- F3 ma_coil_ratio — MA5/10/20 收敛度（蓄势判定）
- F4 atr_contraction_pct — ATR14 在近 60 日 ATR 序列中的分位
- F5 emotion_extreme — F1-F4 多条件计数合成的底/顶标签

全部为研究信号：本模块不做任何实盘计权判断（门控在 four_dim_scorer 里通过
strategy_registry.is_allowed_in_live 完成）。

Fail-closed 规则：数据不足时返回 {"available": False, "reason": "...",
"value": None}，绝不返回中性数值伪装成有效结果。

纯标准库实现，不触网，复用 skills/common/indicators.py 的 calc_ma/calc_atr。
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from indicators import calc_atr, calc_ma  # noqa: E402

try:
    from config_registry import config_path  # noqa: E402
except Exception:  # noqa: BLE001
    config_path = None  # type: ignore[assignment]


# ========== 默认配置（config/scoring.yaml 缺块时的 fail-safe 同值默认）==========

_DEFAULT_CONFIG: Dict[str, Any] = {
    "strategy_id": "emotion_cycle:v1",
    "volume_percentile": {
        "window": 60,
        "min_samples": 20,
        "buckets": {"cold": 0.20, "active": 0.70, "hot": 0.90, "extreme": 0.98},
    },
    "volume_spike": {
        "window": 20,
        "distribution_suspect": 5.0,
        "heavy": 2.5,
        "shrink": 0.5,
    },
    "ma_coil": {"periods": [5, 10, 20], "coil_threshold": 0.02},
    "atr_contraction": {
        "period": 14,
        "min_atr_points": 10,
        "contracting_pct": 0.20,
        "expanding_pct": 0.90,
    },
    "synthesis": {"bottom_min": 3, "top_min": 2},
}


def _load_config() -> Dict[str, Any]:
    """读取 config/scoring.yaml 的 emotion_cycle 块；缺失/损坏用模块内默认（fail-safe）。"""
    if config_path is None:
        return _DEFAULT_CONFIG
    try:
        import yaml

        with open(config_path("scoring"), encoding="utf-8") as file:
            payload = yaml.safe_load(file)
        block = payload.get("emotion_cycle") if isinstance(payload, dict) else None
        if not isinstance(block, dict):
            return _DEFAULT_CONFIG
        merged = dict(_DEFAULT_CONFIG)
        for key, value in block.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                nested = dict(merged[key])
                nested.update(value)
                merged[key] = nested
            else:
                merged[key] = value
        return merged
    except Exception:  # noqa: BLE001
        return _DEFAULT_CONFIG


_CONFIG = _load_config()


def _fail(reason: str, **extra: Any) -> Dict[str, Any]:
    result = {"available": False, "reason": reason, "value": None}
    result.update(extra)
    return result


def _extract_series(klines: List[Dict[str, Any]], field: str) -> List[Optional[float]]:
    return [k.get(field) for k in (klines or []) if isinstance(k, dict)]


# ========== F1: volume_percentile_60d ==========

def compute_volume_percentile(klines: List[Dict[str, Any]],
                              config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = (config or _CONFIG).get("volume_percentile", _DEFAULT_CONFIG["volume_percentile"])
    window = int(cfg.get("window", 60))
    min_samples = int(cfg.get("min_samples", 20))
    buckets = cfg.get("buckets", _DEFAULT_CONFIG["volume_percentile"]["buckets"])

    volumes = _extract_series(klines, "volume")
    volumes = [v for v in volumes if v is not None]
    if len(volumes) < min_samples:
        return _fail(f"insufficient volume samples: {len(volumes)} < {min_samples}")

    window_used = min(window, len(volumes))
    windowed = volumes[-window_used:]
    today = windowed[-1]
    history = windowed[:-1]
    if not history:
        return _fail("insufficient history after windowing")

    hits = sum(1 for v in history if v <= today)
    pct = hits / len(history)

    if pct < buckets.get("cold", 0.20):
        bucket = "cold"
    elif pct < buckets.get("active", 0.70):
        bucket = "normal"
    elif pct < buckets.get("hot", 0.90):
        bucket = "active"
    elif pct < buckets.get("extreme", 0.98):
        bucket = "hot"
    else:
        bucket = "extreme"

    return {
        "available": True,
        "value": round(pct, 4),
        "pct": round(pct, 4),
        "bucket": bucket,
        "window_used": window_used,
    }


# ========== F2: volume_spike_ratio ==========

def compute_volume_spike(klines: List[Dict[str, Any]],
                         config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = (config or _CONFIG).get("volume_spike", _DEFAULT_CONFIG["volume_spike"])
    window = int(cfg.get("window", 20))
    distribution_suspect = float(cfg.get("distribution_suspect", 5.0))
    heavy = float(cfg.get("heavy", 2.5))
    shrink = float(cfg.get("shrink", 0.5))

    volumes = _extract_series(klines, "volume")
    volumes = [v for v in volumes if v is not None]
    if len(volumes) < window + 1:
        return _fail(f"insufficient volume samples: {len(volumes)} < {window + 1}")

    today = volumes[-1]
    prior = volumes[-(window + 1):-1]
    avg = sum(prior) / len(prior) if prior else 0.0
    if avg <= 0:
        return _fail("avg volume window is non-positive")

    ratio = today / avg
    if ratio >= distribution_suspect:
        label = "distribution_suspect"
    elif ratio >= heavy:
        label = "heavy_volume"
    elif ratio <= shrink:
        label = "shrink"
    else:
        label = "normal"

    return {
        "available": True,
        "value": round(ratio, 4),
        "ratio": round(ratio, 4),
        "label": label,
    }


# ========== F3: ma_coil_ratio ==========

def compute_ma_coil(klines: List[Dict[str, Any]],
                    config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = (config or _CONFIG).get("ma_coil", _DEFAULT_CONFIG["ma_coil"])
    periods = cfg.get("periods", [5, 10, 20])
    coil_threshold = float(cfg.get("coil_threshold", 0.02))

    closes = _extract_series(klines, "close")
    closes = [c for c in closes if c is not None]
    min_required = max(periods) if periods else 20
    if len(closes) < min_required:
        return _fail(f"insufficient close samples: {len(closes)} < {min_required}")

    ma_values = []
    for period in periods:
        series = calc_ma(closes, period)
        last = series[-1] if series else None
        if last is None:
            return _fail(f"MA{period} unavailable at current length")
        ma_values.append(last)

    ma_sorted = sorted(ma_values)
    median = ma_sorted[len(ma_sorted) // 2] if len(ma_sorted) % 2 == 1 else (
        (ma_sorted[len(ma_sorted) // 2 - 1] + ma_sorted[len(ma_sorted) // 2]) / 2
    )
    if median <= 0:
        return _fail("median MA is non-positive")

    coil = (max(ma_values) - min(ma_values)) / median
    coiled = coil <= coil_threshold

    return {
        "available": True,
        "value": round(coil, 6),
        "coil": round(coil, 6),
        "coiled": coiled,
        "ma_values": [round(v, 4) for v in ma_values],
    }


# ========== F4: atr_contraction_pct ==========

def compute_atr_contraction(klines: List[Dict[str, Any]],
                            config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = (config or _CONFIG).get("atr_contraction", _DEFAULT_CONFIG["atr_contraction"])
    period = int(cfg.get("period", 14))
    min_atr_points = int(cfg.get("min_atr_points", 10))
    contracting_pct = float(cfg.get("contracting_pct", 0.20))
    expanding_pct = float(cfg.get("expanding_pct", 0.90))

    highs = _extract_series(klines, "high")
    lows = _extract_series(klines, "low")
    closes = _extract_series(klines, "close")
    if any(v is None for v in highs) or any(v is None for v in lows) or any(v is None for v in closes):
        # 对齐长度不足直接过滤 None（与 four_dim_scorer 现有口径一致）
        n = min(len(highs), len(lows), len(closes))
        highs = [h for h in highs[:n] if h is not None]
        lows = [lw for lw in lows[:n] if lw is not None]
        closes = [c for c in closes[:n] if c is not None]

    n = min(len(highs), len(lows), len(closes))
    highs, lows, closes = highs[:n], lows[:n], closes[:n]

    if n < period + 1:
        return _fail(f"insufficient bars for ATR{period}: {n}")

    atr_raw = calc_atr(highs, lows, closes, period)
    atr_series = [a for a in atr_raw if a is not None]
    if len(atr_series) < min_atr_points:
        return _fail(f"insufficient ATR points: {len(atr_series)} < {min_atr_points}")

    today_atr = atr_series[-1]
    history = atr_series[:-1]
    if not history:
        return _fail("insufficient ATR history after excluding today")

    hits = sum(1 for a in history if a <= today_atr)
    pct = hits / len(history)

    if pct <= contracting_pct:
        label = "contracting"
    elif pct >= expanding_pct:
        label = "expanding"
    else:
        label = "normal"

    return {
        "available": True,
        "value": round(pct, 4),
        "pct": round(pct, 4),
        "label": label,
        "today_atr": round(today_atr, 4),
    }


# ========== F5: emotion_extreme（合成判定）==========

def synthesize_emotion_extreme(sub_features: Dict[str, Dict[str, Any]],
                               config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """基于 F1-F4 子特征的多条件计数合成底/顶标签。

    不可用子特征不计入命中，记入 degraded_features；可用子特征数不足时
    label=neutral, available=False（fail-closed）。
    """
    cfg = (config or _CONFIG).get("synthesis", _DEFAULT_CONFIG["synthesis"])
    bottom_min = int(cfg.get("bottom_min", 3))
    top_min = int(cfg.get("top_min", 2))

    vol_pct = sub_features.get("volume_percentile_60d") or {}
    vol_spike = sub_features.get("volume_spike_ratio") or {}
    ma_coil = sub_features.get("ma_coil_ratio") or {}
    atr_contr = sub_features.get("atr_contraction_pct") or {}

    degraded_features = [
        name for name, feat in (
            ("volume_percentile_60d", vol_pct),
            ("volume_spike_ratio", vol_spike),
            ("ma_coil_ratio", ma_coil),
            ("atr_contraction_pct", atr_contr),
        )
        if not feat.get("available")
    ]

    available_count = 4 - len(degraded_features)
    # 需要至少能判定出 bottom_min 或 top_min 所需的最少可用子特征数，
    # 否则视为整体不可用（子特征全 fail-closed 或严重降级）。
    min_needed = min(bottom_min, top_min)
    if available_count < min_needed:
        return {
            "available": False,
            "label": "neutral",
            "bottom_hits": 0,
            "top_hits": 0,
            "degraded_features": degraded_features,
        }

    bottom_hits = 0
    if vol_pct.get("available") and vol_pct.get("bucket") == "cold":
        bottom_hits += 1
    if ma_coil.get("available") and ma_coil.get("coiled"):
        bottom_hits += 1
    if atr_contr.get("available") and atr_contr.get("label") == "contracting":
        bottom_hits += 1
    if vol_spike.get("available") and vol_spike.get("label") == "shrink":
        bottom_hits += 1

    top_hits = 0
    if vol_pct.get("available") and vol_pct.get("bucket") in ("hot", "extreme"):
        top_hits += 1
    if vol_spike.get("available") and vol_spike.get("label") == "distribution_suspect":
        top_hits += 1
    if atr_contr.get("available") and atr_contr.get("label") == "expanding":
        top_hits += 1

    if bottom_hits >= bottom_min:
        label = "emotion_bottom"
    elif top_hits >= top_min:
        label = "emotion_top"
    else:
        label = "neutral"

    return {
        "available": True,
        "label": label,
        "bottom_hits": bottom_hits,
        "top_hits": top_hits,
        "degraded_features": degraded_features,
    }


# ========== 顶层聚合入口 ==========

def compute_emotion_features(klines: List[Dict[str, Any]],
                             config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """计算全部情绪周期特征（F1-F4 + F5 合成）。纯函数，输入 klines 时间升序。"""
    cfg = config or _CONFIG
    vol_pct = compute_volume_percentile(klines, cfg)
    vol_spike = compute_volume_spike(klines, cfg)
    ma_coil = compute_ma_coil(klines, cfg)
    atr_contr = compute_atr_contraction(klines, cfg)

    sub_features = {
        "volume_percentile_60d": vol_pct,
        "volume_spike_ratio": vol_spike,
        "ma_coil_ratio": ma_coil,
        "atr_contraction_pct": atr_contr,
    }
    emotion_extreme = synthesize_emotion_extreme(sub_features, cfg)

    return {
        "volume_percentile_60d": vol_pct,
        "volume_spike_ratio": vol_spike,
        "ma_coil_ratio": ma_coil,
        "atr_contraction_pct": atr_contr,
        "emotion_extreme": emotion_extreme,
    }
