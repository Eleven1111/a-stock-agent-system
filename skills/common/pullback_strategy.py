"""RS 领先回调（第二策略）信号定义 — upgrade-plan v2 §7c 的可回测代理。

"主题主升回调"的完整形态需要主题生命周期历史（theme_registry 刚上线，无历史
可回测），本模块给出其**个股级可回测代理**：主升趋势中的相对强势领先股回调
至短均线支撑并企稳。主题成分的龙头正是 RS 领先个股，故该代理覆盖策略核心
假设（强势延续 + 回调低吸），主题过滤器在门控通过后作为叠加条件接入。

信号是纯前缀函数（无前视）：只用 bars[0..i] 判定第 i 根是否出信号，符合
chan_signal_backtest.extract_signal_events 的 analyzer 契约——首次可观测后
次日开盘入场，收益按框架 T+1/T+3 净收益口径评估。

铁律：本策略未通过 research_gate（IS/OOS + 置换 + FDR）前保持 research-only，
不得注册 strategy_registry、不得影响实盘排序。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


STRATEGY_ID = "rs_leader_pullback"
DIRECTION = "bullish"

# 参数固定为常量以保证门控评估的 rules fingerprint 稳定；调参 = 重新过门控。
PARAMS: dict[str, float | int] = {
    "min_bars": 25,
    "leadership_ret20_min": 0.15,
    "trend_ma_days": 20,
    "trend_rise_lookback": 5,
    "support_ma_days": 10,
    "support_tolerance": 1.01,
    "pullback_min": 0.03,
    "pullback_max": 0.15,
}


def _close(bar: Mapping[str, Any]) -> float | None:
    try:
        value = float(bar.get("close"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _sma(values: Sequence[float], days: int) -> float | None:
    if len(values) < days:
        return None
    window = values[-days:]
    return sum(window) / days


def signal_on_last_bar(bars: Sequence[Mapping[str, Any]]) -> bool:
    """判定最后一根 K 线是否触发 RS 领先回调信号（仅用历史前缀）。"""
    if len(bars) < int(PARAMS["min_bars"]):
        return False
    closes = [_close(bar) for bar in bars]
    if any(value is None for value in closes[-int(PARAMS["min_bars"]):]):
        return False
    closes_f = [float(v) for v in closes if v is not None]
    if len(closes_f) != len(bars):
        return False

    last = bars[-1]
    close = closes_f[-1]
    ma_days = int(PARAMS["trend_ma_days"])
    ma20 = _sma(closes_f, ma_days)
    lookback = int(PARAMS["trend_rise_lookback"])
    ma20_prev = _sma(closes_f[:-lookback], ma_days)
    if ma20 is None or ma20_prev is None:
        return False
    if close <= ma20 or ma20 <= ma20_prev:
        return False

    ret20 = close / closes_f[-21] - 1.0 if len(closes_f) >= 21 else None
    if ret20 is None or ret20 < float(PARAMS["leadership_ret20_min"]):
        return False

    high20 = max(closes_f[-ma_days:])
    drawdown = 1.0 - close / high20 if high20 > 0 else 0.0
    if not float(PARAMS["pullback_min"]) <= drawdown <= float(PARAMS["pullback_max"]):
        return False

    ma10 = _sma(closes_f, int(PARAMS["support_ma_days"]))
    try:
        low = float(last.get("low"))
        opened = float(last.get("open"))
    except (TypeError, ValueError):
        return False
    if ma10 is None or low > ma10 * float(PARAMS["support_tolerance"]):
        return False

    prev_close = closes_f[-2]
    return close > opened and close >= prev_close


def analyze(bars: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """chan_signal_backtest.extract_signal_events 的 analyzer 契约实现。"""
    bars = list(bars)
    if not signal_on_last_bar(bars):
        return {"signals": []}
    return {
        "signals": [{
            "strategy_id": STRATEGY_ID,
            "type": STRATEGY_ID,
            "idx": len(bars) - 1,
        }],
    }
