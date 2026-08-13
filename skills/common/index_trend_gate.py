#!/usr/bin/env python3
"""
指数趋势闸门（index_trend_gate）— 打板优化方案 P1，SHADOW ONLY
==============================================================
手册 T-01/T-02/T-05（共识 B 级）："短线第一要素是回避指数主跌浪"。当前 market_gate
里没有任何指数级判据。本模块补上：沪指收盘 vs 5/10/20 日线 + 两日量能 vs 20 日均量。

量能阈值不写死"1.5万亿/2万亿"（水位随时代漂移，手册自己也说口径会变），改用相对
口径：近两日均量 < 20 日均量 × volume_shrink_ratio 即视为缩量降仓。

SHADOW ONLY：只描述"若启用会减仓/转防守"，绝不真的改仓位/排序。启用留给 P2。
Fail-closed：K 线不足 min_bars 时 available=False，不臆造趋势判断。

阈值走 config/daban_thresholds.yaml 的 index_trend 节。纯标准库计算 + 复用
indicators.calc_ma；取数走 a_stock_http.fetch_tencent_kline（可注入以便单测不触网）。
"""

from __future__ import annotations

import argparse
import json
from statistics import mean
from typing import Any, Callable, Mapping, Optional, Sequence

from daban_config import section as _daban_section
from indicators import calc_ma

_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "index_code": "000001",
    "index_market": "sh",
    "ma_periods": [5, 10, 20],
    "volume_shrink_ratio": 0.7,
    "defend_below_ma": 20,
    "reduce_below_ma": 5,
    "min_bars": 21,
}


def _config(config: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    # daban_config.section 自身 fail-safe（缺块/损坏回退 DEFAULTS），从不抛出。
    base = dict(_DEFAULTS)
    if config is None:
        config = _daban_section("index_trend")
    for key, value in dict(config or {}).items():
        base[key] = value
    return base


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def assess_index_trend(
    bars: Sequence[Mapping[str, Any]] | None,
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """纯函数：从指数日线算 MA 位置与量能收缩。bars 需按日期升序。

    fail-closed：bars 不足或含缺失收盘时 available=False，不输出方向性判断。
    """
    cfg = _config(config)
    min_bars = int(cfg.get("min_bars") or 21)
    rows = list(bars or [])
    closes = [c for c in (_num(row.get("close")) for row in rows) if c is not None]
    volumes = [v for v in (_num(row.get("volume")) for row in rows) if v is not None]
    if len(rows) < min_bars or len(closes) < min_bars or len(volumes) < 20:
        return {
            "schema": "index_trend_gate_v1",
            "enabled": bool(cfg.get("enabled")),
            "shadow_only": True,
            "available": False,
            "reason": f"指数K线不足({len(rows)}<{min_bars})或含缺失，fail-closed",
            "would_reduce": False,
            "would_defend": False,
            "reasons": [],
        }

    last_close = closes[-1]
    periods = [int(p) for p in cfg.get("ma_periods") or [5, 10, 20]]
    ma_values: dict[str, Optional[float]] = {}
    below: dict[str, bool] = {}
    for period in periods:
        series = calc_ma(closes, period)
        ma = series[-1] if series else None
        ma_values[str(period)] = ma
        below[str(period)] = ma is not None and last_close < ma

    avg_20 = mean(volumes[-20:])
    recent_two_day = mean(volumes[-2:])
    shrink_ratio = float(cfg.get("volume_shrink_ratio") or 0.7)
    volume_shrink = avg_20 > 0 and recent_two_day < avg_20 * shrink_ratio

    reduce_ma = str(int(cfg.get("reduce_below_ma") or 5))
    defend_ma = str(int(cfg.get("defend_below_ma") or 20))
    would_reduce = bool(below.get(reduce_ma))
    would_defend = bool(below.get(defend_ma))

    reasons: list[str] = []
    if would_defend:
        reasons.append(f"收盘跌破{defend_ma}日线：影子转防守（手册 T-02）")
    elif would_reduce:
        reasons.append(f"收盘跌破{reduce_ma}日线：影子减仓（手册 T-02）")
    if volume_shrink:
        reasons.append(
            f"近两日均量{recent_two_day:.0f}<20日均量{avg_20:.0f}×{shrink_ratio}：影子降仓（手册 T-05）"
        )

    return {
        "schema": "index_trend_gate_v1",
        "enabled": bool(cfg.get("enabled")),
        "shadow_only": True,
        "available": True,
        "index_code": cfg.get("index_code"),
        "last_close": round(last_close, 2),
        "ma": {k: (round(v, 2) if v is not None else None) for k, v in ma_values.items()},
        "below_ma": below,
        "avg_volume_20d": round(avg_20, 0),
        "recent_two_day_avg_volume": round(recent_two_day, 0),
        "volume_shrink": volume_shrink,
        "would_reduce": would_reduce,
        "would_defend": would_defend,
        "reasons": reasons,
    }


def fetch_index_trend(
    *,
    config: Mapping[str, Any] | None = None,
    fetcher: Callable[..., Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """取指数日线并评估。fetcher 可注入以便单测不触网。"""
    cfg = _config(config)
    from a_stock_http import DataSourceError

    if fetcher is None:
        from a_stock_http import fetch_tencent_kline
        fetcher = fetch_tencent_kline
    days = max(int(cfg.get("min_bars") or 21) + 10, 40)
    try:
        bars = fetcher(
            str(cfg.get("index_code")),
            market=str(cfg.get("index_market") or "sh"),
            days=days,
        )
    except DataSourceError as exc:  # 外部取数失败 → fail-closed（不当中性证据）
        return {
            "schema": "index_trend_gate_v1",
            "enabled": bool(cfg.get("enabled")),
            "shadow_only": True,
            "available": False,
            "reason": f"指数取数失败：{exc}",
            "would_reduce": False,
            "would_defend": False,
            "reasons": [],
        }
    return assess_index_trend(bars, config=cfg)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="指数趋势闸门（影子）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    result = fetch_index_trend()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result.get("reason") or "; ".join(result.get("reasons") or []) or "指数趋势正常")
