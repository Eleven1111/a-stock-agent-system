#!/usr/bin/env python3
"""
四维打分引擎 — A股标的多维度自动评分
======================================
技术面 × 情绪面 × 催化面 × 深度面 → S/A/B/C 分级 + 买卖建议

数据源（统一共享 HTTP 层，cron-safe）：
- 腾讯 qt.gtimg.cn — 实时行情 + 历史K线
- Serper.dev — 新闻催化
- stock-analyst 技术指标模块 — numpy 计算

Usage:
  python3 four_dim_scorer.py 600519 贵州茅台
  python3 four_dim_scorer.py 600519 贵州茅台 --json
"""

import json
import sys
import os
from datetime import date, datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

# ========== 路径 ==========
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOCK_ANALYST = os.path.join(os.path.dirname(SKILL_DIR), "stock-analyst")
sys.path.insert(0, STOCK_ANALYST)

# ========== 数据抓取（统一走 a_stock_http）==========

import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

from a_stock_http import (
    fetch_tencent_quote as _http_quote,
    fetch_tencent_kline as _http_kline,
)
from data_provider import fetch_serper_news as _fetch_serper_news
from data_provider import _next_serper_key
from http_client import DataSourceError
from tradeability import assess_tradeability
from deep_research_cache import (
    read_deep_research,
    decay_stale_score,
    DEFAULT_MAX_AGE_DAYS as _DEEP_DEFAULT_MAX_AGE_DAYS,
)

# 缠论结构信号 + 策略闸门（过 research_gate 才计权，否则 display-only 标研究假设）
_CHANLUN_DIR = os.path.join(os.path.dirname(SKILL_DIR), "chanlun-backtest", "scripts")
if _CHANLUN_DIR not in sys.path:
    sys.path.insert(0, _CHANLUN_DIR)
try:
    import chan_structure as _chan
except Exception:  # noqa: BLE001
    _chan = None
try:
    import chan_nested as _chan_nested
except ImportError:
    _chan_nested = None
from strategy_registry import is_allowed_in_live as _chan_allowed

# 技术指标统一走 common/indicators.py（去重，数值与历史实现逐位一致）
from indicators import (
    calc_ma, calc_macd, calc_rsi, calc_kdj, calc_atr,
    calc_volume_ratio, calc_chip_concentration,
)

# 情绪周期确定性特征（研究信号，过 research_gate 才计权，否则 display-only 0权重）
from emotion_cycle_features import compute_emotion_features as _compute_emotion_features

_EMOTION_CYCLE_STRATEGY_ID = "emotion_cycle:v1"

_MARKET_TEMPERATURE_SENTIMENT_DELTA = {
    "冰点": -0.4,
    "修复": 0.2,
    "发酵": 0.6,
    "加速": 0.3,
    "极热": -0.5,
}


def _market_temperature_sentiment_overlay(
    signal_ctx: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Convert shared market temperature into a bounded sentiment delta."""
    if not signal_ctx:
        return {
            "delta": 0.0,
            "temperature": {"tier": None, "context_status": "unknown"},
            "notes": ["市场温度不可用，未计入情绪面"],
        }
    try:
        temperature = dict(temperature_from_context(signal_ctx))
    except (TypeError, ValueError, RuntimeError):
        temperature = {"tier": None, "context_status": "unknown"}

    explicit_status = signal_ctx.get("temperature_context_status") or signal_ctx.get("context_status")
    if explicit_status in {"stale", "unknown", "degraded"}:
        temperature["context_status"] = explicit_status
        temperature["context_fresh"] = False
    tier = str(temperature.get("tier") or "")
    status = str(temperature.get("context_status") or "")
    if status != "fresh" or tier not in _MARKET_TEMPERATURE_SENTIMENT_DELTA:
        return {
            "delta": 0.0,
            "temperature": temperature,
            "notes": ["市场温度不可用，未计入情绪面"],
        }
    delta = _MARKET_TEMPERATURE_SENTIMENT_DELTA[tier]
    return {
        "delta": delta,
        "temperature": temperature,
        "notes": [f"市场温度{tier}({delta:+.1f})"],
    }

# 大盘 context overlay（外围环境回流个股评分；无缓存则 no-op）
from market_context import read_market_context, apply_market_overlay

# 情绪上下文（连板梯队/板块赚钱效应/资金流回流情绪面；无缓存则回退历史逻辑）
from signal_context import read_signal_context, sentiment_boost
from market_temperature import temperature_from_context
from social_attention import sentiment_attention_overlay
from catalyst_context import read_catalyst_events
from config_registry import config_path
from scoring.catalyst import (  # noqa: F401
    CATALYST_TIERS,
    CLARIFICATION_RISK_TERMS,
    T1_WEIGHT as _T1_WEIGHT,
    freshness_factor,
    news_age_days as _news_age_days,
    news_catalyst_score,
)


def fetch_tencent_realtime(code: str, market: str = "sz") -> Dict[str, Any]:
    """腾讯实时行情 — 委托 a_stock_http。"""
    try:
        full_code = f"{market}{code}"
        result = _http_quote([full_code])
        data = result.get(full_code) if isinstance(result, dict) else None
        return data if isinstance(data, dict) and data.get("price") is not None else {"error": "数据不完整"}
    except Exception as e:
        return {"error": str(e)}


def fetch_tencent_kline(code: str, market: str = "sz", days: int = 60, ktype: str = "day") -> List[Dict]:
    """腾讯历史K线 — 委托 a_stock_http。"""
    try:
        return _http_kline(code, market, days, ktype)
    except Exception:
        return []


def fetch_serper_news(query: str, num: int = 5) -> Optional[List[Dict]]:
    """Serper.dev 新闻；None 表示源不可用，空列表表示可用但无结果。"""
    api_key = _next_serper_key()
    if not api_key:
        return None
    try:
        result = _fetch_serper_news(query, api_key, num)
        return [dict(item) for item in result.data]
    except (DataSourceError, AttributeError, TypeError):
        return None


_OBSERVABLE_PROXY_NAMES = (
    "up_down_volume_ratio",
    "vwap_hold_ratio",
    "obv_slope",
    "mfi",
    "consecutive_large_order_net",
    "lhb_institution_net_buy",
    "industry_fund_flow",
)


def _proxy_observation(value: Any = None, *, source: str = "unavailable",
                       asof: Any = None, asof_lag_days: Any = None) -> Dict[str, Any]:
    """Keep a proxy value inseparable from its provenance.

    These are observations, not an inference about an actor's intent.  In
    particular, missing source/as-of data remains unavailable instead of
    being filled with the current time.
    """
    # A numeric value without both provenance dimensions is not an
    # admissible observation; fail closed rather than implying freshness.
    if value is not None and (source == "unavailable" or asof is None):
        value = None
    result = {"value": value, "source": source, "asof": asof}
    if asof_lag_days is not None:
        result["asof_lag_days"] = asof_lag_days
    return result


def _proxy_bundle(values: Optional[Dict[str, Any]] = None,
                  observations: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    values = values or {}
    observations = observations or {
        name: _proxy_observation() for name in _OBSERVABLE_PROXY_NAMES
    }
    # Direct numeric fields keep the existing score payload easy to consume;
    # observable_proxies is the canonical value+provenance representation.
    return {
        **{name: values.get(name) for name in _OBSERVABLE_PROXY_NAMES},
        "observable_proxies": observations,
        "proxy_provenance": {
            name: {key: value for key, value in obs.items() if key != "value"}
            for name, obs in observations.items()
        },
    }


def _bar_asof(bar: Dict[str, Any]) -> Any:
    return (bar.get("asof") or bar.get("date") or bar.get("datetime")
            or bar.get("time"))


def _quote_proxy(quote: Optional[Dict[str, Any]], name: str,
                 aliases: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """Read an explicitly supplied quote proxy without deriving intent."""
    quote = quote if isinstance(quote, dict) else {}
    keys = (name,) + aliases
    raw = next((quote[key] for key in keys if key in quote), None)
    if isinstance(raw, dict):
        value = raw.get("value")
        source = raw.get("source") or quote.get("provider") or "quote"
        asof = raw.get("asof") or quote.get("asof") or quote.get("date")
        lag = raw.get("asof_lag_days")
    else:
        value = raw
        source = quote.get("provider") or "quote"
        asof = quote.get("asof") or quote.get("date")
        lag = None
    if value is None:
        return _proxy_observation()
    return _proxy_observation(value, source=source, asof=asof,
                              asof_lag_days=lag)


def _proxy_rows(bars: List[Dict[str, Any]]) -> List[tuple]:
    """(close, volume, high, low) for bars whose numbers are usable."""
    rows: List[tuple] = []
    for bar in bars:
        try:
            close = float(bar.get("close"))
            volume = float(bar.get("volume"))
            high = float(bar.get("high", close))
            low = float(bar.get("low", close))
        except (TypeError, ValueError):
            continue
        if close > 0 and volume >= 0:
            rows.append((close, volume, high, low))
    return rows


def _obv_slope(rows: List[tuple]) -> Optional[float]:
    """Least-squares slope of on-balance volume over the trailing 20 bars."""
    obv = [0.0]
    for (prev, _, _, _), (close, volume, _, _) in zip(rows, rows[1:]):
        obv.append(obv[-1] + (volume if close > prev else -volume if close < prev else 0))
    window = obv[-min(20, len(obv)):]
    if len(window) < 2:
        return None
    x_mean = (len(window) - 1) / 2
    y_mean = sum(window) / len(window)
    denom = sum((i - x_mean) ** 2 for i in range(len(window)))
    return sum((i - x_mean) * (value - y_mean) for i, value in enumerate(window)) / denom


def _money_flow_index(rows: List[tuple]) -> Optional[float]:
    period = min(14, len(rows) - 1)
    positive = negative = 0.0
    for previous, current in zip(rows[-period - 1:-1], rows[-period:]):
        typical = (current[0] + current[2] + current[3]) / 3.0
        money = typical * current[1]
        if typical > (previous[0] + previous[2] + previous[3]) / 3.0:
            positive += money
        elif typical < (previous[0] + previous[2] + previous[3]) / 3.0:
            negative += money
    return 100.0 if negative == 0 and positive else (
        100.0 - 100.0 / (1.0 + positive / negative) if negative else None
    )


def _bar_proxy_values(rows: List[tuple]) -> Dict[str, Optional[float]]:
    """Bar-level proxies; a key absent here stays an unavailable observation."""
    up_volume = sum(volume for (prev, _, _, _), (close, volume, _, _) in zip(rows, rows[1:])
                    if close > prev)
    down_volume = sum(volume for (prev, _, _, _), (close, volume, _, _) in zip(rows, rows[1:])
                      if close < prev)
    ratio = up_volume / down_volume if down_volume > 0 else None
    hold_count = sum(
        close >= (high + low + close) / 3.0
        for close, _volume, high, low in rows
    )
    values: Dict[str, Optional[float]] = {
        "up_down_volume_ratio": round(ratio, 6) if ratio is not None else None,
        "vwap_hold_ratio": round(hold_count / len(rows), 6),
    }
    slope = _obv_slope(rows)
    if slope is not None:
        values["obv_slope"] = round(slope, 6)
    mfi = _money_flow_index(rows)
    values["mfi"] = round(mfi, 6) if mfi is not None else None
    return values


def compute_observable_proxies(
    klines: Optional[List[Dict[str, Any]]] = None,
    quote: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute only reproducible price/volume proxies; never name an actor.

    Daily bars cannot observe intraday time above VWAP, so the VWAP field is a
    bar-level hold ratio (close >= typical-price VWAP proxy).  Actor-specific
    fields are accepted only when an upstream adapter explicitly supplies
    them, otherwise they remain unavailable.
    """
    bars = [bar for bar in (klines or []) if isinstance(bar, dict)]
    dated = [bar for bar in bars if _bar_asof(bar)]
    asof = _bar_asof(dated[-1]) if dated else None
    source = ((dated[-1].get("source") or dated[-1].get("provider"))
              if dated else None) or "tencent_kline"
    observations: Dict[str, Dict[str, Any]] = {
        name: _proxy_observation() for name in _OBSERVABLE_PROXY_NAMES
    }
    rows = _proxy_rows(bars) if len(bars) >= 2 else []
    if len(rows) >= 2:
        for name, value in _bar_proxy_values(rows).items():
            observations[name] = _proxy_observation(value, source=source, asof=asof)

    quote_fields = {
        "consecutive_large_order_net": ("large_order_net", "large_order_net_yi"),
        "lhb_institution_net_buy": ("institution_lhb_net_buy", "institution_lhb_net_wan"),
        "industry_fund_flow": ("sector_fund_flow", "industry_net_flow"),
    }
    for name, aliases in quote_fields.items():
        supplied = _quote_proxy(quote, name, aliases)
        if supplied["value"] is not None:
            observations[name] = supplied
    values = {name: obs.get("value") for name, obs in observations.items()}
    return _proxy_bundle(values, observations)
# ========== 技术指标 → 统一走 common/indicators.py ==========
# calc_ma/calc_ema/calc_macd/calc_rsi/calc_kdj 已在顶部从 indicators import，
# 数值与历史实现逐位一致，消除与 chan_structure / tech_analysis 的重复定义。


# ========== 四维评分 ==========

_CHAN_LABELS = {
    "third_buy": "缠论三买", "third_sell": "缠论三卖",
    "top_divergence": "顶背驰", "bottom_divergence": "底背驰",
}


def chan_adjustment(signals, recent_window, total_bars, allow_fn):
    """对最近的缠论结构信号计算技术面调整（纯函数，便于单测）。

    allow_fn(strategy_id)->bool 决定该信号是否过 research_gate 计权；未过闸只标
    "研究假设"、不改分（信号过闸才加权的红线执行点）。

    展示噪声收敛（T4 遗留）：strategy_id 为 None 的新谱系信号（bsp1p/bsp2/bsp2s，
    无 legacy 映射）不逐条追加备注，聚合成一条计数——legacy 四类型（strategy_id
    有值但未过闸）逐条备注的行为不变。返回 (delta, lock_max, notes)。
    """
    delta = 0.0
    lock_max = None
    notes = []
    new_lineage_count = 0
    for s in signals or []:
        idx = s.get("idx")
        if idx is None or idx < total_bars - recent_window:
            continue  # 信号不够新，忽略
        t = s.get("type")
        label = _CHAN_LABELS.get(t, t)
        strategy_id = s.get("strategy_id")
        if not allow_fn(strategy_id):
            if strategy_id is None:
                new_lineage_count += 1
            else:
                notes.append(f"[研究假设]{label}(未过闸·0权重)")
            continue
        if t == "third_buy":
            delta += 1.5
            notes.append(f"{label}✅+1.5")
        elif t == "bottom_divergence":
            delta += 1.0
            notes.append(f"{label}✅+1.0")
        elif t == "third_sell":
            delta -= 1.5
            lock_max = 6.0 if lock_max is None else min(lock_max, 6.0)
            notes.append(f"{label}⚠️-1.5锁分")
        elif t == "top_divergence":
            delta -= 1.5
            lock_max = 5.0 if lock_max is None else min(lock_max, 5.0)
            notes.append(f"{label}⚠️-1.5锁分")
    if new_lineage_count:
        notes.append(f"[研究假设]缠论新谱系信号×{new_lineage_count}(未过闸·0权重)")
    return delta, lock_max, notes


def score_technical(code: str, name: str, quote: Optional[Dict[str, Any]] = None,
                    klines: Optional[List[Dict]] = None) -> Dict[str, Any]:
    """技术面评分（0-10）。quote/klines 可由 score_stock 预取注入，避免同票重复抓取。"""
    market = "sz" if code.startswith(("0", "3")) else "sh"
    rt = quote if quote is not None else fetch_tencent_realtime(code, market)
    if klines is None:
        klines = fetch_tencent_kline(code, market, 60)
    proxies = compute_observable_proxies(klines, rt)

    if not klines:
        return {
            "score": 5,
            "ma5": None,
            "ma20": None,
            "ma60": None,
            "rsi6": None,
            "rsi14": None,
            "atr14": None,
            "volume_ratio": None,
            "chip_concentration": None,
            "price": rt.get("price"),
            "change_pct": rt.get("change_pct"),
            "detail": "K线数据不足",
            **proxies,
        }

    closes = [k.get("close") for k in klines if isinstance(k, dict)]
    closes = [c for c in closes if c is not None]
    highs = [k.get("high") for k in klines if isinstance(k, dict)]
    highs = [h for h in highs if h is not None]
    lows = [k.get("low") for k in klines if isinstance(k, dict)]
    lows = [lw for lw in lows if lw is not None]
    volumes = [k.get("volume") for k in klines if isinstance(k, dict)]
    volumes = [v for v in volumes if v is not None]

    if not closes:
        return {
            "score": 5,
            "ma5": None,
            "ma20": None,
            "ma60": None,
            "rsi6": None,
            "rsi14": None,
            "atr14": None,
            "volume_ratio": None,
            "chip_concentration": None,
            "price": rt.get("price"),
            "change_pct": rt.get("change_pct"),
            "detail": "K线close数据为空",
            **proxies,
        }

    ma5 = calc_ma(closes, 5)
    ma10 = calc_ma(closes, 10)
    ma20 = calc_ma(closes, 20)
    ma60 = calc_ma(closes, 60)
    dif, dea, macd_hist = calc_macd(closes)
    rsi6 = calc_rsi(closes, 6)
    rsi14 = calc_rsi(closes, 14)
    k, d, j = calc_kdj(highs, lows, closes)
    vol_ma5 = calc_ma(volumes, 5)

    score = 5.0
    signals = []

    # 趋势判断
    last_ma5, last_ma10 = ma5[-1], ma10[-1]
    last_ma20, last_ma60 = ma20[-1], ma60[-1]
    last_close = closes[-1]

    # 多头/空头排列
    if last_ma5 and last_ma10 and last_ma20 and last_ma60:
        if last_ma5 > last_ma10 > last_ma20 > last_ma60:
            score += 2.0
            signals.append("均线多头排列")
        elif last_ma5 < last_ma10 and last_ma10 < last_ma20:
            if last_ma20 < last_ma60:
                # 完全空头——锁上限
                score = min(score, -0.5)
                signals.append("⚠️ 均线空头排列，锁评分上限")
            else:
                score -= 1.5
                signals.append("短期均线空头")
    if last_close and last_ma20:
        if last_close > last_ma20:
            score += 0.5
            signals.append("价格站上MA20")
        else:
            score -= 0.5

    # MACD
    if dif[-1] is not None and dea[-1] is not None:
        if dif[-1] > dea[-1]:
            if dif[-2] and dea[-2] and dif[-2] <= dea[-2]:
                score += 1.5
                signals.append("MACD金叉")
            else:
                score += 0.5
        else:
            score -= 0.5

    # RSI
    if rsi6[-1] is not None:
        rsi = rsi6[-1]
        if rsi > 80:
            score -= 1.0
            signals.append(f"RSI超买({rsi:.0f})")
        elif rsi < 30:
            signals.append(f"RSI超卖({rsi:.0f})")
            if last_close > last_ma20 if last_ma20 else False:
                score += 1.0
            else:
                signals.append("⚠️ 价格<MA20，超卖不视为买入")

    # KDJ
    if j[-1] is not None:
        if j[-1] > 100:
            score -= 0.5
        elif j[-1] < 0 and last_close > (last_ma20 or 0):
            score += 0.5

    # 量能（升级：量比 + 传统量能）
    vol_r = calc_volume_ratio(volumes)
    if vol_r is not None:
        if vol_r >= 2.5:
            score += 1.0
            signals.append(f"量比极高({vol_r:.1f}x)")
        elif vol_r >= 1.5:
            score += 0.5
            signals.append(f"放量({vol_r:.1f}x)")
        elif vol_r <= 0.5:
            score -= 0.3
            signals.append(f"缩量({vol_r:.1f}x)")
    elif vol_ma5[-1] and volumes[-1]:
        vol_ratio = volumes[-1] / vol_ma5[-1]
        if vol_ratio > 1.5:
            score += 0.5
            signals.append(f"放量({vol_ratio:.1f}x)")

    # 筹码集中度（仅作为可观测分布代理，不推断参与者行为）
    chip_conc = calc_chip_concentration(closes, volumes)
    if chip_conc is not None:
        if chip_conc < 8:
            score += 0.8
            signals.append(f"筹码高度集中({chip_conc:.1f}%)")
        elif chip_conc < 15:
            score += 0.3
            signals.append(f"筹码较集中({chip_conc:.1f}%)")
        elif chip_conc > 30:
            score -= 0.3
            signals.append(f"筹码分散({chip_conc:.1f}%)")

    # ATR 波动率度量（供执行计划和报告使用，不直接加分）
    atr_values = calc_atr(highs, lows, closes, 14)
    last_atr = atr_values[-1] if atr_values and atr_values[-1] is not None else None

    # 缠论结构信号（过 research_gate 才计权，否则 display-only 标研究假设）
    chan_sigs = []
    if _chan is not None:
        try:
            chan_sigs = _chan.analyze(klines).get("signals", [])
        except Exception:  # noqa: BLE001
            chan_sigs = []
    chan_delta, chan_lock, chan_notes = chan_adjustment(
        chan_sigs, recent_window=10, total_bars=len(klines), allow_fn=_chan_allowed)
    score += chan_delta
    signals.extend(chan_notes)

    # 情绪周期确定性特征（研究信号，display-only，0权重直到过 research_gate）。
    # 过闸后的 delta 数值本次不实现——TODO(research_gate): 待 emotion_cycle:v1
    # 通过离线研究闸门并写入 strategy_registry 后，再设计计权规则，不得看实盘回拟合。
    emotion_features = _compute_emotion_features(klines)
    if not _chan_allowed(_EMOTION_CYCLE_STRATEGY_ID):
        signals.append("[研究假设]情绪周期(未过闸·0权重)")

    score = max(-3, min(10, score))
    if chan_lock is not None:
        score = min(score, chan_lock)

    return {
        "score": round(score, 1),
        "ma5": round(last_ma5, 2) if last_ma5 else None,
        "ma20": round(last_ma20, 2) if last_ma20 else None,
        "ma60": round(last_ma60, 2) if last_ma60 else None,
        "rsi6": round(rsi6[-1], 1) if rsi6[-1] is not None else None,
        "rsi14": round(rsi14[-1], 1) if rsi14[-1] is not None else None,
        "atr14": round(last_atr, 3) if last_atr is not None else None,
        "volume_ratio": vol_r,
        "chip_concentration": chip_conc,
        "price": rt.get("price"),
        "change_pct": rt.get("change_pct"),
        "detail": "; ".join(signals) if signals else "无明确信号",
        "emotion_cycle": emotion_features,
        **proxies,
    }


def score_sentiment(code: str, name: str, quote: Optional[Dict[str, Any]] = None,
                    signal_ctx: Optional[Dict[str, Any]] = None,
                    sector: Optional[str] = None) -> Dict[str, Any]:
    """情绪面评分（0-10）——个股量价 + 连板梯队/板块赚钱效应/资金流上下文。

    本系统核心玩法是抓赚钱效应板块做打板/高成长，情绪面是主战场：
    基础分仍由涨跌幅+换手率给出（向后兼容），其上叠加 signal_context 的
    连板梯队在册/封板质量/板块涨停集群/资金流与北向观测加成；上下文缺失时
    行为与历史完全一致。
    """
    market = "sz" if code.startswith(("0", "3")) else "sh"
    rt = quote if quote is not None else fetch_tencent_realtime(code, market)

    score = 5.0
    signals = []

    pct = rt.get("change_pct")
    turnover = rt.get("turnover")
    amount = rt.get("amount")

    if pct is not None:
        if pct >= 9.9:
            score += 3.0
            signals.append("涨停")
        elif pct >= 7:
            score += 2.0
            signals.append("大涨")
        elif pct >= 3:
            score += 1.0
        elif pct <= -9.9:
            score -= 3.0
            signals.append("跌停")
        elif pct <= -5:
            score -= 2.0
            signals.append("大跌")

    if turnover is not None:
        if turnover > 20:
            score += 1.5
            signals.append(f"超高换手({turnover:.1f}%)")
        elif turnover > 10:
            score += 0.5
        elif turnover < 1:
            score -= 0.5

    # 板块赚钱效应/连板梯队/资金流上下文（缺失→0 加成，行为同历史）
    ctx = signal_ctx if signal_ctx is not None else read_signal_context()
    boost = sentiment_boost(code, ctx, sector=sector)
    score += boost["delta"]
    signals.extend(boost["notes"])
    social = sentiment_attention_overlay(code, ctx)
    score += social["delta"]
    signals.extend(social["notes"])
    temperature_overlay = _market_temperature_sentiment_overlay(ctx)
    score += temperature_overlay["delta"]
    signals.extend(temperature_overlay["notes"])

    score = max(0, min(10, score))

    return {
        "score": round(score, 1),
        "change_pct": pct,
        "turnover": turnover,
        "amount_yi": round(amount / 1e8, 1) if amount else None,
        "context_boost": boost["delta"],
        "social_attention_delta": social["delta"],
        "social_attention": social["record"],
        "market_temperature_delta": temperature_overlay["delta"],
        "market_temperature": temperature_overlay["temperature"],
        "sector": boost.get("sector"),
        "detail": "; ".join(signals) if signals else "情绪中性",
    }


def score_catalyst(code: str, name: str) -> Dict[str, Any]:
    """催化面评分（0-10）——分级关键词权重 × 新闻新鲜度衰减。

    升级说明：旧版关键词平权 ±0.8 且不看新闻日期——一个月前的"利好"和今早的
    "国家战略"同分。现按 T1/T2/T3 分级（政策战略>订单业绩>泛利好），并按
    新闻时间衰减（≤3天全额，>30天两折），更贴合打板/高成长对催化时效的要求。
    """
    cached_news = read_catalyst_events(code)
    raw_news = fetch_serper_news(
        f"{name} {code} 公告 澄清 风险提示 政策 业绩",
        num=5,
    )
    live_available = raw_news is not None   # None=数据源不可用；[]=可用但无新闻
    source_available = live_available or bool(cached_news)

    news = []
    seen = set()
    for item in [*(raw_news or []), *cached_news]:
        key = item.get("link") or item.get("title")
        if key in seen:
            continue
        seen.add(key)
        news.append(item)

    catalysts = [{"title": item.get("title", ""),
                  "source": (item.get("source", {}) or {}).get("name", "")
                            if isinstance(item.get("source", {}), dict)
                            else item.get("source", ""),
                  "date": item.get("date", "")} for item in news[:5]]

    scored = news_catalyst_score(news)
    base = 5.0 if source_available else 4.5
    score = max(0, min(10, base + scored["delta"]))
    if live_available and cached_news:
        source_status = "live_and_cache"
    elif live_available:
        source_status = "live"
    elif cached_news:
        source_status = "cache_only"
    else:
        source_status = "unavailable"

    return {
        "score": round(score, 1),
        "news_count": len(news),
        "available": source_available,
        "source_status": source_status,
        "cache_count": len(cached_news),
        "catalysts": catalysts[:3],
        "detail": (
            "; ".join(scored["signals"][:3])
            if scored["signals"]
            else ("催化源不可用" if not source_available else "无显著催化")
        ),
    }


def _pe_snapshot_score(pe: Optional[float]) -> float:
    """PE 估值快照分（0-10）——深研缓存缺失时的回退基准。"""
    score = 5.0
    if pe is not None:
        if 0 < pe < 15:
            score += 2.0
        elif 15 <= pe < 30:
            score += 1.0
        elif pe > 100:
            score -= 1.5
        elif pe < 0:
            score -= 1.0
    return max(0, min(10, score))


def _fetch_eps_consensus(code: str) -> dict[str, Any]:
    """Fetch EPS consensus from eastmoney_intelligence, return key fields."""
    try:
        from eastmoney_intelligence import extract_consensus_eps
        eps = extract_consensus_eps(code)
        return {
            "eps_consensus_current": eps.get("eps_consensus_current"),
            "eps_consensus_next": eps.get("eps_consensus_next"),
            "eps_consensus_after_next": eps.get("eps_consensus_after_next"),
            "eps_coverage_count": eps.get("coverage_count", 0),
            "eps_latest_report_date": eps.get("latest_report_date"),
        }
    except Exception:  # noqa: BLE001 — graceful fallback
        return {
            "eps_consensus_current": None,
            "eps_consensus_next": None,
            "eps_consensus_after_next": None,
            "eps_coverage_count": 0,
            "eps_latest_report_date": None,
        }


def _eps_score(eps_current: float | None, eps_next: float | None) -> float:
    """EPS一致预期评分（0~2分附加分）。

    加分逻辑：
    - 有当年EPS预期且>0 → +0.5
    - 有次年EPS预期且同比增长 → +1.0
    - 有次年EPS预期且同比降幅<20% → +0.5
    - 覆盖机构>=3 → +0.5
    """
    score = 0.0
    if eps_current and eps_current > 0:
        score += 0.5
    if eps_next and eps_current:
        growth = (eps_next - eps_current) / abs(eps_current)
        if growth > 0.05:
            score += 1.0
        elif growth > -0.2:
            score += 0.5
    elif eps_next and eps_next > 0:
        score += 0.5
    return round(min(score, 2.0), 2)


def _excluded_deep_payload(deep: Dict[str, Any], raw: float, age: int,
                           pe: Optional[float], cap: Optional[float]) -> Dict[str, Any]:
    """重度过期深研的返回体：标 excluded，交由 score_stock 移出加权合成。

    分值原样带出（不衰减）仅供展示与排查；`excluded=True` 是唯一的消费契约，
    下游一律以它为准判断该维度是否有效，不得再读 score 参与任何判定。
    """
    up = deep.get("valuation_upside_pct")
    up_str = f", 中性赔率{up:+.0f}%" if isinstance(up, (int, float)) else ""
    pe_str = f"; PE={pe:.1f}" if pe is not None else ""
    return {
        "score": round(raw, 1),
        "source": "serenity_deep_excluded",
        "stale": True,
        "excluded": True,
        "asof": deep.get("asof"),
        "age_days": age,
        "scorecard_total": deep.get("scorecard_total"),
        "rating": deep.get("rating"),
        "valuation_upside_pct": up,
        "dimensions": deep.get("dimensions", {}),
        "pe": pe,
        "market_cap_yi": round(cap, 1) if cap else None,
        "detail": f"⚠️深研严重过期({age}天)已退出加权 投研{deep.get('rating') or ''}"
                  f"({deep.get('scorecard_total')}/100→{raw}){up_str}{pe_str}",
    }


def score_deep(code: str, name: str, quote: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """深度面评分（0-10）——优先读 Serenity 投研缓存，过期衰减，缺失回退 PE 快照。

    升级：新增长期 EPS 一致预期作为基本面前瞻指标，附加 0~2 分。
    当 PE 估值与 EPS 预期给出一致方向时提升置信度。
    """
    market = "sz" if code.startswith(("0", "3")) else "sh"
    rt = quote if quote is not None else fetch_tencent_realtime(code, market)
    pe = rt.get("pe")
    cap = rt.get("market_cap")
    pe_score = _pe_snapshot_score(pe)

    # ── EPS 一致预期（前瞻基本面）──
    eps_data = _fetch_eps_consensus(code)
    eps_bonus = _eps_score(eps_data["eps_consensus_current"], eps_data["eps_consensus_next"])

    deep = read_deep_research(code)
    if deep and deep.get("deep_score") is not None:
        raw = deep["deep_score"]
        age = deep.get("age_days") or 0
        # fail-closed 过期治理阶梯：新鲜（stale=False）满权重；轻度过期（stale=True 但
        # 未超过 max_age_days + exclude_after_extra_days）沿用衰减、仍满权重参与合成；
        # 重度过期（超过阶梯上限）deep 维度退出合成加权，见 score_stock 的 scoring_available。
        severe_threshold = _DEEP_DEFAULT_MAX_AGE_DAYS + DEEP_STALENESS_EXCLUDE_AFTER_EXTRA_DAYS
        if bool(deep.get("stale")) and age > severe_threshold:
            return _excluded_deep_payload(deep, raw, age, pe, cap)
        if deep.get("stale"):
            score = decay_stale_score(raw, pe_score, age)
            source = "serenity_deep_stale"
            tag = f"⚠️过期{age}天 "
        else:
            score = raw
            source = "serenity_deep"
            tag = ""
        # EPS bonus 叠加
        score = round(min(score + eps_bonus, 10.0), 1)
        up = deep.get("valuation_upside_pct")
        up_str = f", 中性赔率{up:+.0f}%" if isinstance(up, (int, float)) else ""
        pe_str = f"; PE={pe:.1f}" if pe is not None else ""
        eps_str = f"; EPS预期{eps_data['eps_consensus_current']}/{eps_data['eps_consensus_next']}" if eps_data['eps_consensus_current'] else ""
        return {
            "score": round(score, 1),
            "source": source,
            "stale": bool(deep.get("stale")),
            "asof": deep.get("asof"),
            "age_days": age,
            "scorecard_total": deep.get("scorecard_total"),
            "rating": deep.get("rating"),
            "valuation_upside_pct": up,
            "dimensions": deep.get("dimensions", {}),
            "pe": pe,
            "market_cap_yi": round(cap, 1) if cap else None,
            "eps_bonus": eps_bonus,
            **eps_data,
            "detail": f"{tag}投研{deep.get('rating') or ''}"
                      f"({deep.get('scorecard_total')}/100→{raw}){up_str}{pe_str}{eps_str}",
        }

    # 回退：PE 估值快照 + EPS 预期
    signals_detail = f"PE={pe:.1f}" if pe is not None else "PE缺失"
    if cap:
        signals_detail += f", 市值={cap:.0f}亿"
    score = round(min(pe_score + eps_bonus, 10.0), 1)
    eps_str = f"; 预期EPS={eps_data['eps_consensus_current']}/{eps_data['eps_consensus_next']}" if eps_data['eps_consensus_current'] else ""
    signals_detail += eps_str
    signals_detail += "（无深研缓存，估值快照）"
    return {
        "score": score,
        "source": "valuation_snapshot",
        "stale": False,
        "pe": pe,
        "market_cap_yi": round(cap, 1) if cap else None,
        "eps_bonus": eps_bonus,
        **eps_data,
        "detail": signals_detail,
    }


# ========== 汇总 ==========

import yaml


def _load_scoring_config() -> Optional[Dict[str, Any]]:
    try:
        with open(config_path("scoring"), encoding="utf-8") as file:
            return yaml.safe_load(file)
    except Exception:
        return None


# 深研过期治理阶梯默认值——与 config/scoring.yaml 的 deep_staleness.exclude_after_extra_days
# 保持一致；配置缺失或解析失败时 fail-closed 回退到该默认值，不放宽阶梯。
_DEFAULT_DEEP_STALENESS_EXCLUDE_AFTER_EXTRA_DAYS = 30

_scoring_cfg = _load_scoring_config()
if _scoring_cfg:
    _weights_cfg = _scoring_cfg.get("scoring", {}).get("weights", {})
    if isinstance(_weights_cfg, dict) and "default" in _weights_cfg:
        WEIGHTS_BY_LANE = _weights_cfg
        WEIGHTS = _weights_cfg["default"]
    else:
        WEIGHTS = _weights_cfg if _weights_cfg else {"technical": 0.30, "sentiment": 0.15, "catalyst": 0.30, "deep": 0.25}
        WEIGHTS_BY_LANE = {"default": WEIGHTS}
    _temp_overlay_cfg = _scoring_cfg.get("scoring", {}).get("temperature_overlay", {})
    _grades_cfg = _scoring_cfg.get("scoring", {}).get("grades", {})
    _confidence_cfg = _scoring_cfg.get("scoring", {}).get("confidence", {})
    _risk_cfg = _scoring_cfg.get("risk", {})
    try:
        DEEP_STALENESS_EXCLUDE_AFTER_EXTRA_DAYS = int(
            _scoring_cfg.get("scoring", {}).get("deep_staleness", {}).get(
                "exclude_after_extra_days", _DEFAULT_DEEP_STALENESS_EXCLUDE_AFTER_EXTRA_DAYS
            )
        )
    except (TypeError, ValueError):
        DEEP_STALENESS_EXCLUDE_AFTER_EXTRA_DAYS = _DEFAULT_DEEP_STALENESS_EXCLUDE_AFTER_EXTRA_DAYS
else:
    WEIGHTS = {"technical": 0.30, "sentiment": 0.15, "catalyst": 0.30, "deep": 0.25}
    WEIGHTS_BY_LANE = {"default": WEIGHTS}
    _temp_overlay_cfg = {}
    _grades_cfg = {}
    _confidence_cfg = {}
    _risk_cfg = {}
    DEEP_STALENESS_EXCLUDE_AFTER_EXTRA_DAYS = _DEFAULT_DEEP_STALENESS_EXCLUDE_AFTER_EXTRA_DAYS


def resolve_weights(strategy_id: str = "four_dim",
                    temperature_tier: Optional[str] = None) -> Dict[str, float]:
    """按策略通道 + 温度档位解析最终权重。"""
    lane = "default"
    sid = str(strategy_id or "")
    if sid.startswith("daban") and "daban" in WEIGHTS_BY_LANE:
        lane = "daban"
    elif sid.startswith("trend") and "trend" in WEIGHTS_BY_LANE:
        lane = "trend"
    base = dict(WEIGHTS_BY_LANE.get(lane, WEIGHTS))
    overlay = _temp_overlay_cfg.get(temperature_tier, {}) if temperature_tier else {}
    for dim, delta in overlay.items():
        if dim in base and isinstance(delta, (int, float)):
            base[dim] = max(0.05, base[dim] + delta)
    total = sum(base.values())
    if total > 0:
        base = {k: v / total for k, v in base.items()}
    return base

GRADES = sorted(
    [
        {
            "name": name,
            "min": cfg.get("min", 0),
            "emoji": cfg.get("emoji", ""),
            "advice": cfg.get("advice", ""),
        }
        for name, cfg in _grades_cfg.items()
    ],
    key=lambda item: item["min"],
    reverse=True,
) if _grades_cfg else [
    {"name": "S", "min": 8.0, "emoji": "🟢🟢🟢", "advice": "研究高分 — 多维度共振，等待完整政策复核"},
    {"name": "A", "min": 6.0, "emoji": "🟢🟢", "advice": "研究较高分 — 技术与催化偏强，等待完整政策复核"},
    {"name": "B", "min": 4.0, "emoji": "🟢", "advice": "研究中性 — 保持观察，不构成方向建议"},
    {"name": "C", "min": 2.0, "emoji": "🔶", "advice": "研究偏弱 — 仅作风险观察"},
    {"name": "D", "min": 0.0, "emoji": "🔴", "advice": "研究低分 — 仅作风险观察"},
]

CONFIDENCE_RULES = {
    level: {"requires": (_confidence_cfg.get(level) or {}).get("requires", [])}
    for level in ("high", "medium", "low")
}
if not any(item["requires"] for item in CONFIDENCE_RULES.values()):
    CONFIDENCE_RULES = {
        "high": {"requires": ["realtime", "kline", "news", "valuation"]},
        "medium": {"requires": ["realtime", "kline"]},
        "low": {"requires": ["realtime"]},
    }


def grade(weighted_score: float) -> Tuple[str, str, str]:
    """S/A/B/C 分级 — 从配置读取阈值。"""
    for item in GRADES:
        if weighted_score >= item["min"]:
            return item["name"], item["emoji"], item["advice"]
    return "D", "🔴", "回避 — 多维度利空"


def _grade_by_name(name: str) -> Tuple[str, str, str]:
    for item in GRADES:
        if item["name"] == name:
            return item["name"], item["emoji"], item["advice"]
    return grade(0)


def _detect_coherence(scores: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """信号一致性检测：多空维度矛盾/共振判定（纯函数）。

    深研严重过期被判 excluded 后不只是退出加权合成——作废的证据在这里同样不得投票：
    既不计入多空维度计数（否则数月前的高分仍能凑出「多维共振」+0.5），也不参与
    以 deep 为条件的冲突判定。
    """
    tech_score = scores["technical"]["score"]
    sent_score = scores["sentiment"]["score"]
    cata_score = scores["catalyst"]["score"]
    deep_score = scores["deep"]["score"]
    deep_excluded = bool(scores["deep"].get("excluded"))

    voting = [tech_score, sent_score, cata_score]
    if not deep_excluded:
        voting.append(deep_score)
    bullish_dims = sum(1 for s in voting if s >= 6.5)
    bearish_dims = sum(1 for s in voting if s <= 3.5)
    conflicts = []
    tags = []
    delta = 0.0

    if bullish_dims >= 3:
        tags.append("多维共振看多")
        delta += 0.5
    elif bearish_dims >= 3:
        tags.append("多维共振看空")
        delta -= 0.5

    if cata_score >= 7 and tech_score <= 4:
        conflicts.append("催化利好但技术偏空")
        tags.append("消息面博弈·高波动风险")
    if cata_score >= 7 and sent_score <= 4:
        conflicts.append("催化利好但情绪低迷")
        tags.append("催化未被市场认可")
        delta -= 0.3
    if sent_score >= 7 and tech_score <= 4:
        conflicts.append("情绪主导超越技术")
        tags.append("短线博弈型")
    if not deep_excluded and tech_score >= 7 and deep_score <= 3:
        conflicts.append("技术走强但基本面差")
        tags.append("投机性反弹风险")

    return {
        "bullish_dims": bullish_dims,
        "bearish_dims": bearish_dims,
        "conflicts": conflicts,
        "tags": tags,
        "delta": round(delta, 2),
    }


def _record_signal_date(record: Dict[str, Any]) -> Optional[date]:
    raw = record.get("signal_date") or record.get("date") or record.get("created_at")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _win_rate(records: List[Dict[str, Any]]) -> Optional[float]:
    if not records:
        return None
    wins = [r for r in records if str(r.get("outcome", "")).startswith("win")]
    return round(len(wins) / len(records) * 100, 1)


def _load_historical_reference(
    grade: str,
    strategy_id: str,
    sector: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """从 performance_tracker 读取近30日历史胜率参考（静默失败）。"""
    try:
        from state_store import read_json
        from paths import data_file
        hist_file = data_file("stock-triage", "signal_history.json")
        records = read_json(hist_file, [])
        if not isinstance(records, list) or not records:
            return None
        current = (now or datetime.now()).date()
        cutoff = current - timedelta(days=30)
        closed = [
            r for r in records
            if r.get("t1_close_ret") is not None
            and (d := _record_signal_date(r)) is not None
            and cutoff <= d <= current
        ]
        if not closed:
            return None
        grade_closed = [r for r in closed if r.get("grade") == grade]
        sid = str(strategy_id or "default")
        strategy_closed = [r for r in closed if r.get("strategy_id", "default") == sid]
        sector_closed = [
            r for r in closed
            if sector and str(r.get("sector") or "") == str(sector)
        ]
        recent_pool = [
            r for r in closed
            if r.get("outcome") and r["outcome"] != "pending"
            and (r.get("grade") == grade or r.get("strategy_id", "default") == sid)
        ]
        recent = recent_pool[-5:]
        return {
            "window_days": 30,
            "grade_win_rate": _win_rate(grade_closed),
            "grade_samples": len(grade_closed),
            "strategy_win_rate": _win_rate(strategy_closed),
            "strategy_samples": len(strategy_closed),
            "sector": sector,
            "sector_win_rate": _win_rate(sector_closed),
            "sector_samples": len(sector_closed),
            "recent_signals": [
                {"code": r["code"], "name": r.get("name", ""), "grade": r.get("grade", ""),
                 "outcome": r.get("outcome", ""), "t1_ret": r.get("t1_close_ret")}
                for r in recent
            ],
        }
    except Exception:  # noqa: BLE001
        return None


def score_stock(code: str, name: str, quote: Optional[Dict[str, Any]] = None,
                klines: Optional[List[Dict]] = None,
                market_ctx: Optional[Dict[str, Any]] = None,
                strategy_id: str = "four_dim",
                temperature_tier: Optional[str] = None,
                sector: Optional[str] = None) -> Dict[str, Any]:
    """完整四维评分。quote/klines 可由批量调用方预取注入，同票只抓一次（4→1）；
    market_ctx 为大盘上下文，缺省时自读缓存，用于出分后叠加大盘 overlay。
    strategy_id 决定权重通道（default/daban/trend），temperature_tier 叠加情绪偏移。"""
    print(f"🔍 正在分析 {name}({code})...", file=sys.stderr)
    market = "sz" if code.startswith(("0", "3")) else "sh"
    if quote is None:
        quote = fetch_tencent_realtime(code, market)
    if klines is None:
        klines = fetch_tencent_kline(code, market, 60)

    technical = score_technical(code, name, quote=quote, klines=klines)
    print(f"  技术面: {technical['score']}/10", file=sys.stderr)

    sentiment = score_sentiment(code, name, quote=quote)
    print(f"  情绪面: {sentiment['score']}/10", file=sys.stderr)

    catalyst = score_catalyst(code, name)
    print(f"  催化面: {catalyst['score']}/10", file=sys.stderr)

    deep = score_deep(code, name, quote=quote)
    print(f"  深度面: {deep['score']}/10", file=sys.stderr)

    scores = {"technical": technical, "sentiment": sentiment,
              "catalyst": catalyst, "deep": deep}

    # 动态权重：按策略通道 + 温度档位解析
    active_weights = resolve_weights(strategy_id, temperature_tier)

    # 各维度数据是否真实可用；催化源不可用时保留保守权重，避免被动放大情绪面。
    available = {
        "technical": technical["ma5"] is not None,
        "sentiment": sentiment.get("change_pct") is not None,
        "catalyst": catalyst.get("available", catalyst["news_count"] > 0),
        "deep": deep.get("source", "").startswith("serenity") or deep.get("pe") is not None,
    }
    scoring_available = dict(available)
    if not available["catalyst"] and catalyst.get("score") is not None:
        scoring_available["catalyst"] = True
    # 深研严重过期：退出合成加权（fail-closed 治理阶梯），其余三维按比例归一化。
    deep_excluded = bool(deep.get("excluded"))
    if deep_excluded:
        scoring_available["deep"] = False
    eff = {k: active_weights[k] for k in active_weights if scoring_available.get(k, True)}
    total_w = sum(eff.values())
    if total_w > 0:
        eff = {k: v / total_w for k, v in eff.items()}
    else:
        eff = dict(active_weights)
    weighted = sum(scores[k]["score"] * eff.get(k, 0.0) for k in active_weights)
    excluded = [k for k in active_weights if not scoring_available.get(k, True)]
    degraded = [k for k in active_weights if scoring_available.get(k, True) and not available.get(k, True)]

    # 信号共振检测
    coherence = _detect_coherence(scores)
    weighted = round(weighted + coherence["delta"], 1)

    g, emoji, advice = grade(weighted)
    score_gates = []
    # deep 严重过期被排除时 fail-closed：没有有效深研证据就不能认定 S 档
    # （AGENTS.md：外部数据缺失不得被当作中性证据或无风险）。此处不读它的分值，
    # 否则一份数月前的高分会替 S 档背书。
    if (
        g == "S"
        and not str(strategy_id or "").startswith("daban")
        and (catalyst["score"] < 5.5 or deep_excluded or deep["score"] < 6.0)
    ):
        score_gates.append("insufficient_catalyst_or_deep_for_s")
        g, emoji, advice = _grade_by_name("A")

    # 追涨停护栏：daban 通道对当日已涨停票=「涨停后追入」，2 年全市场 OOS 证伪其在可成交
    # 口径(open_close)下负期望(-0.6%~-1%/笔，issue #28)。定位=打板只做涨停前预判，
    # 故抑制追涨停推荐：标 gate 并把 S/A 压到 B（trend/default 通道不受影响）。
    chase_limitup = (
        str(strategy_id or "").startswith("daban")
        and (sentiment.get("change_pct") or 0) >= 9.9
    )
    if chase_limitup:
        score_gates.append("chase_limitup_negative_ev")
        if g in ("S", "A"):
            g, emoji, advice = _grade_by_name("B")

    data_coverage = {
        "realtime": technical["price"] is not None,
        "kline": technical["ma5"] is not None,
        "news": catalyst["news_count"] > 0,
        "valuation": deep["pe"] is not None,
        "deep_research": deep.get("source", "").startswith("serenity") and not deep.get("stale", False),
    }

    confidence = "low"
    for level in ["high", "medium", "low"]:
        required = CONFIDENCE_RULES[level]["requires"]
        if all(data_coverage.get(r, False) for r in required):
            confidence = level
            break

    if confidence == "low":
        advice = "数据不足，无法给出方向性判断"
        emoji = "⚪"

    trade = assess_tradeability(quote, code, name) if "error" not in quote else \
        {"tradeable": False, "status": "no_data", "reason": "行情缺失"}
    if trade.get("tradeable") is False and confidence != "low":
        advice = f"⛔ {trade['reason']}（{advice}）"
    if "chase_limitup_negative_ev" in score_gates and confidence != "low":
        advice = f"⚠️已涨停·追入次日历史负期望(2年OOS)，宜等回调/埋伏下一只｜{advice}"

    # 历史胜率参考
    hist_ref = _load_historical_reference(g, strategy_id, sector=sector)

    # 策略通道标注
    lane = "default"
    sid = str(strategy_id or "")
    if sid.startswith("daban"):
        lane = "daban"
    elif sid.startswith("trend"):
        lane = "trend"

    result = {
        "code": code,
        "name": name,
        "timestamp": datetime.now().isoformat(),
        "strategy_id": strategy_id,
        "strategy_lane": lane,
        "confidence": confidence,
        "data_coverage": data_coverage,
        "tradeability": trade,
        "scores": {
            "technical": technical,
            "sentiment": sentiment,
            "catalyst": catalyst,
            "deep": deep,
        },
        "coherence": coherence,
        "weighted": round(weighted, 1),
        "grade": g,
        "emoji": emoji,
        "advice": advice,
        # A factor score is research evidence, not a directional policy decision.
        # Only the canonical recommendation-quality/policy path may set this true.
        "directional_ready": False,
        "execution_action": "none",
        "policy_status": "not_evaluated",
        "weights": {k: f"{v*100:.0f}%" for k, v in active_weights.items()},
        "effective_weights": {k: f"{eff.get(k, 0)*100:.0f}%" for k in active_weights},
        "excluded_dims": excluded,
        "degraded_dims": degraded,
        "deep_excluded": deep_excluded,
        "score_gates": score_gates,
        "deep_source": scores["deep"].get("source"),
        "historical_reference": hist_ref,
        "temperature_tier": temperature_tier,
        "sector": sector,
    }

    ctx = market_ctx if market_ctx is not None else read_market_context()
    return apply_market_overlay(result, ctx)


def format_report(result: Dict[str, Any]) -> str:
    """格式化四维研究报告；原始评分不产生方向性执行建议。"""
    s = result["scores"]
    lane = result.get("strategy_lane", "default")
    lane_label = {"daban": "打板", "trend": "趋势", "default": "综合"}.get(lane, lane)
    lines = [
        f"🏆 **{result['name']}({result['code']})** [{lane_label}通道]",
        f"**加权总分：{result['weighted']}/10 | 等级：{result['grade']} | {result['emoji']} {result['advice']}**",
        "",
    ]

    # 1. 四维评分卡
    lines.extend([
        "## 四维评分卡",
        "| 维度 | 权重 | 评分 | 关键发现 |",
        "|------|------|------|---------|",
        f"| 技术面 | {result['weights']['technical']} | {s['technical']['score']}/10 | {s['technical']['detail']} |",
        f"| 情绪面 | {result['weights']['sentiment']} | {s['sentiment']['score']}/10 | {s['sentiment']['detail']} |",
        f"| 催化面 | {result['weights']['catalyst']} | {s['catalyst']['score']}/10 | {s['catalyst']['detail']} |",
        f"| 深度面 | {result['weights']['deep']} | {s['deep']['score']}/10 | {s['deep']['detail']} |",
        "",
    ])

    # 2. 核心论据
    coherence = result.get("coherence", {})
    if coherence.get("tags") or coherence.get("conflicts"):
        lines.append("## 信号共振分析")
        if coherence.get("tags"):
            lines.append(f"**标签：** {' | '.join(coherence['tags'])}")
        if coherence.get("conflicts"):
            for c in coherence["conflicts"]:
                lines.append(f"  ⚠️ {c}")
        lines.append("")

    # 3. 技术指标明细
    t = s['technical']
    lines.append("## 技术指标")
    price_line = f"**价格：** {t.get('price', 'N/A')} | **涨跌：** {t.get('change_pct', 'N/A')}%"
    if t.get('atr14'):
        price_line += f" | **ATR(14)：** {t['atr14']}"
    lines.append(price_line)
    if t.get('ma5') and t.get('ma20') and t.get('ma60'):
        lines.append(f"**MA5/20/60：** {t['ma5']} / {t['ma20']} / {t['ma60']}")
    if t.get('rsi6'):
        lines.append(f"**RSI(6)：** {t['rsi6']} | **RSI(14)：** {t.get('rsi14', 'N/A')}")
    extra = []
    if t.get('volume_ratio') is not None:
        extra.append(f"量比={t['volume_ratio']:.1f}")
    if t.get('chip_concentration') is not None:
        extra.append(f"筹码集中度={t['chip_concentration']:.1f}%")
    proxy_values = t.get("observable_proxies") or {}
    proxy_labels = {
        "up_down_volume_ratio": "涨跌量比",
        "vwap_hold_ratio": "VWAP保持率",
        "obv_slope": "OBV斜率",
        "mfi": "MFI",
    }
    proxy_text = [
        f"{label}={proxy_values[key]['value']}"
        for key, label in proxy_labels.items()
        if proxy_values.get(key, {}).get("value") is not None
    ]
    if proxy_text:
        extra.extend(proxy_text)
    if extra:
        lines.append(f"**微结构：** {' | '.join(extra)}")
    if proxy_values:
        rendered_proxies = "; ".join(
            f"{key}={obs.get('value')} (source={obs.get('source')}, asof={obs.get('asof')})"
            for key, obs in proxy_values.items()
            if obs.get("value") is not None
        )
        lines.append("**可观测资金代理（仅条件证据）：** " +
                     (rendered_proxies or "数据不可用（source=unavailable, asof=None）"))
    lines.append(f"**PE：** {s['deep'].get('pe', 'N/A')}")
    lines.append("")

    # 4. ATR 仅作为研究态波动率信息；执行价位必须由完整政策链生成。
    if t.get("price") and t.get("atr14"):
        atr = float(t["atr14"])
        lines.extend([
            "## 波动率研究参考",
            f"**ATR(14)：** {atr:.2f}",
            "**政策状态：** 未执行公告、数据质量、可交易性、价格计划与组合风险全链检查；不得据此交易。",
            "",
        ])

    # 5. 风险清单
    risks = []
    if s["catalyst"].get("detail") and "澄清" in s["catalyst"]["detail"]:
        risks.append(f"公告风险：{s['catalyst']['detail']}")
    if s["deep"].get("excluded"):
        risks.append(f"深研严重过期({s['deep'].get('age_days', '?')}天)，已退出加权")
    elif s["deep"].get("stale"):
        risks.append(f"深研过期：{s['deep'].get('age_days', '?')}天未更新")
    if result.get("market_overlay", {}).get("regime") == "risk_off":
        risks.append(f"大盘承压：{result['market_overlay'].get('reason', '')}")
    temp = result.get("temperature_tier")
    if temp in ("冰点", "极热"):
        risks.append(f"情绪温度：{temp}")
    if risks:
        lines.append("## 风险清单")
        for r in risks:
            lines.append(f"  ⚠️ {r}")
        lines.append("")

    # 6. 历史胜率参考
    hist = result.get("historical_reference")
    if hist:
        lines.append("## 历史参考")
        if hist.get("window_days"):
            lines.append(f"  统计窗口: 近{hist['window_days']}日")
        if hist.get("grade_win_rate") is not None:
            n = hist["grade_samples"]
            note = "" if n >= 10 else " (样本不足，参考价值有限)"
            lines.append(f"  {result['grade']}级近期胜率: **{hist['grade_win_rate']}%** (N={n}){note}")
        if hist.get("strategy_win_rate") is not None:
            n = hist["strategy_samples"]
            note = "" if n >= 10 else " (样本不足)"
            lines.append(f"  策略({result['strategy_id']})胜率: **{hist['strategy_win_rate']}%** (N={n}){note}")
        if hist.get("sector_win_rate") is not None:
            n = hist["sector_samples"]
            note = "" if n >= 10 else " (样本不足)"
            lines.append(f"  板块({hist.get('sector')})胜率: **{hist['sector_win_rate']}%** (N={n}){note}")
        if hist.get("recent_signals"):
            lines.append("  近5次信号:")
            for sig in hist["recent_signals"]:
                icon = "✅" if str(sig.get("outcome", "")).startswith("win") else "❌"
                ret = f"{sig['t1_ret']:+.1f}%" if sig.get("t1_ret") is not None else "?"
                lines.append(f"    {icon} {sig['name']}({sig['code']}) {sig['grade']}级 → {ret}")
        lines.append("")

    return "\n".join(lines)


def score_short_term_entry(code: str, name: str) -> Dict:
    """短线研究观察（60分钟/30分钟级别），不产生执行指令。"""
    market = "sz" if code.startswith(("0", "3")) else "sh"
    rt = fetch_tencent_realtime(code, market)
    daily_klines = fetch_tencent_kline(code, market, 60, "day")

    k60 = fetch_tencent_kline(code, market, 60, "60")
    k30 = fetch_tencent_kline(code, market, 60, "30")

    signals = []
    score = 5.0
    entry_conditions = {}
    has_macd_cross = False
    above_ma20 = False
    rsi_oversold = False

    if k60 and len(k60) >= 20:
        closes_60 = [k["close"] for k in k60]
        ma20_60 = calc_ma(closes_60, 20)
        dif_60, dea_60, _ = calc_macd(closes_60, fast=12, slow=26, signal=9)

        if ma20_60[-1] and closes_60[-1] > ma20_60[-1]:
            score += 1.0
            above_ma20 = True
            signals.append("60分钟>MA20")
        if dif_60[-1] and dea_60[-1]:
            if dif_60[-1] > dea_60[-1]:
                if dif_60[-2] and dea_60[-2] and dif_60[-2] <= dea_60[-2]:
                    score += 2.0
                    has_macd_cross = True
                    signals.append("60分钟MACD金叉 ⭐")
            elif dif_60[-1] < dea_60[-1]:
                score -= 1.0
                signals.append("60分钟MACD空头")

    vol_r = None
    if k30 and len(k30) >= 20:
        closes_30 = [k["close"] for k in k30]
        volumes_30 = [k.get("volume", 0) for k in k30]
        rsi_30 = calc_rsi(closes_30, 14)
        vol_r = calc_volume_ratio(volumes_30)
        if rsi_30[-1] is not None:
            if rsi_30[-1] < 30:
                score += 1.5
                rsi_oversold = True
                signals.append(f"30分钟RSI超卖({rsi_30[-1]:.0f}) ⭐")
            elif rsi_30[-1] > 70:
                score -= 1.0
                signals.append(f"30分钟RSI超买({rsi_30[-1]:.0f})")

    c_lock = None
    chan_sigs60 = []
    if _chan is not None and k60 and len(k60) >= 20:
        try:
            chan_sigs60 = _chan.analyze(k60).get("signals", [])
        except Exception:  # noqa: BLE001
            chan_sigs60 = []
        c_delta, c_lock, c_notes = chan_adjustment(
            chan_sigs60, recent_window=8, total_bars=len(k60), allow_fn=_chan_allowed)
        score += c_delta
        signals.extend(c_notes)

    # 区间套证据（T5，verdict B 遗留）：日线×60m 同向确定买卖点共现，纯展示、0 权重，
    # 不新增网络请求（daily_klines 已在函数开头取过，供入场价位计算复用）。
    if (_chan is not None and _chan_nested is not None
            and daily_klines and len(daily_klines) >= 20 and k60 and len(k60) >= 20):
        try:
            chan_sigs_daily = _chan.analyze(daily_klines).get("signals", [])
            nested_records = _chan_nested.find_nested_confirmations(
                chan_sigs_daily, len(daily_klines), chan_sigs60, len(k60))
            signals.extend(_chan_nested.format_nested_notes(nested_records))
        except (ValueError, KeyError, TypeError, IndexError, AttributeError, ZeroDivisionError):
            pass

    score = max(0, min(10, score))
    if c_lock is not None:
        score = min(score, c_lock)

    # ATR 用于计算入场条件的价位区间
    atr_val = None
    ma20_val = None
    prev_high = None
    if daily_klines and len(daily_klines) >= 20:
        d_closes = [k["close"] for k in daily_klines]
        d_highs = [k["high"] for k in daily_klines]
        d_lows = [k["low"] for k in daily_klines]
        atr_list = calc_atr(d_highs, d_lows, d_closes, 14)
        atr_val = atr_list[-1] if atr_list and atr_list[-1] else None
        ma20_list = calc_ma(d_closes, 20)
        ma20_val = ma20_list[-1]
        prev_high = max(d_highs[-10:]) if len(d_highs) >= 10 else None

    price = rt.get("price")
    # 研究观察条件；执行条件必须由完整政策链生成。
    vol_ok = vol_r is not None and vol_r >= 1.5
    if has_macd_cross and vol_ok and rt.get("change_pct", 0) < 3:
        entry_conditions["momentum_observation"] = {
            "label": "动量条件满足，等待政策复核",
            "conditions": ["60分钟MACD金叉", f"量比≥1.5({vol_r})", "高开<3%"],
        }
    if ma20_val and price and atr_val:
        pullback_zone = round(ma20_val, 2)
        if price > ma20_val:
            entry_conditions["pullback_observation"] = {
                "label": "回调条件观察，等待政策复核",
                "trigger_price": pullback_zone,
                "conditions": ["价格回踩MA20支撑", "RSI从超卖反弹", "量能萎缩"],
            }
    if prev_high and price and atr_val:
        entry_conditions["breakout_observation"] = {
            "label": "突破条件观察，等待政策复核",
            "trigger_price": round(prev_high, 2),
            "conditions": [f"突破近10日高点{prev_high:.2f}", "放量>1.5倍", "板块共振"],
        }

    suggestion = "等待完整政策复核" if score >= 5 else "研究评分不足"

    return {
        "score": round(score, 1),
        "signals": signals,
        "suggestion": suggestion,
        "directional_ready": False,
        "execution_action": "none",
        "policy_status": "not_evaluated",
        "entry_conditions": entry_conditions,
        "price": price,
        "atr14": atr_val,
        "volume_ratio": vol_r,
        "detail": "; ".join(signals) if signals else "短线无明确信号",
    }


# ========== 入口 ==========

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="四维打分引擎")
    parser.add_argument("code", help="股票代码")
    parser.add_argument("name", help="股票名称")
    parser.add_argument("--json", action="store_true", help="JSON输出")
    parser.add_argument("--timeframe", choices=["day","week","60","30"], default="day",
                        help="分析时间框架")
    args = parser.parse_args()

    if args.timeframe in ("60", "30"):
        result = score_short_term_entry(args.code, args.name)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str))
        else:
            print(f"⚡ **{args.name}({args.code}) 短线研究观察**")
            print(f"评分: {result['score']}/10 | 建议: **{result['suggestion']}**")
            print(f"现价: {result.get('price', 'N/A')}")
            print(f"信号: {result['detail']}")
    else:
        result = score_stock(args.code, args.name)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str))
        else:
            print(format_report(result))