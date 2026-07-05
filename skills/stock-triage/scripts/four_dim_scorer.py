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

import sys as _sys
_COMMON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                           "common")
if _COMMON_DIR not in _sys.path:
    _sys.path.insert(0, _COMMON_DIR)

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
from strategy_registry import is_allowed_in_live as _chan_allowed

# 技术指标统一走 common/indicators.py（去重，数值与历史实现逐位一致）
from indicators import (
    calc_ma, calc_macd, calc_rsi, calc_kdj, calc_atr,
    calc_volume_ratio, calc_chip_concentration,
)

# 情绪周期确定性特征（研究信号，过 research_gate 才计权，否则 display-only 0权重）
from emotion_cycle_features import compute_emotion_features as _compute_emotion_features

_EMOTION_CYCLE_STRATEGY_ID = "emotion_cycle:v1"

# 大盘 context overlay（外围环境回流个股评分；无缓存则 no-op）
from market_context import read_market_context, apply_market_overlay

# 情绪上下文（连板梯队/板块赚钱效应/资金流回流情绪面；无缓存则回退历史逻辑）
from signal_context import read_signal_context, sentiment_boost
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
    "研究假设"、不改分（信号过闸才加权的红线执行点）。返回 (delta, lock_max, notes)。
    """
    delta = 0.0
    lock_max = None
    notes = []
    for s in signals or []:
        idx = s.get("idx")
        if idx is None or idx < total_bars - recent_window:
            continue  # 信号不够新，忽略
        t = s.get("type")
        label = _CHAN_LABELS.get(t, t)
        if not allow_fn(s.get("strategy_id")):
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
    return delta, lock_max, notes


def score_technical(code: str, name: str, quote: Optional[Dict[str, Any]] = None,
                    klines: Optional[List[Dict]] = None) -> Dict[str, Any]:
    """技术面评分（0-10）。quote/klines 可由 score_stock 预取注入，避免同票重复抓取。"""
    market = "sz" if code.startswith(("0", "3")) else "sh"
    rt = quote if quote is not None else fetch_tencent_realtime(code, market)
    if klines is None:
        klines = fetch_tencent_kline(code, market, 60)

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

    # 筹码集中度（新增：主力控盘/建仓信号）
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
    }


def score_sentiment(code: str, name: str, quote: Optional[Dict[str, Any]] = None,
                    signal_ctx: Optional[Dict[str, Any]] = None,
                    sector: Optional[str] = None) -> Dict[str, Any]:
    """情绪面评分（0-10）——个股量价 + 连板梯队/板块赚钱效应/资金流上下文。

    本系统核心玩法是抓赚钱效应板块做打板/高成长，情绪面是主战场：
    基础分仍由涨跌幅+换手率给出（向后兼容），其上叠加 signal_context 的
    连板梯队在册/封板质量/板块涨停集群/主力与北向资金加成；上下文缺失时
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

    score = max(0, min(10, score))

    return {
        "score": round(score, 1),
        "change_pct": pct,
        "turnover": turnover,
        "amount_yi": round(amount / 1e8, 1) if amount else None,
        "context_boost": boost["delta"],
        "social_attention_delta": social["delta"],
        "social_attention": social["record"],
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


def score_deep(code: str, name: str, quote: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """深度面评分（0-10）——优先读 Serenity 投研缓存，过期衰减，缺失回退 PE 快照。

    断点修复：原实现仅做 PE 分桶，让"深度面 20%"形同虚设。现在优先消费
    serenity-investment-research 产出的六维 scorecard（经 deep_research_cache 映射），
    深研一次、日评复用；缓存过期则按新鲜度向 PE 快照线性回归。
    """
    market = "sz" if code.startswith(("0", "3")) else "sh"
    rt = quote if quote is not None else fetch_tencent_realtime(code, market)
    pe = rt.get("pe")
    cap = rt.get("market_cap")
    pe_score = _pe_snapshot_score(pe)

    deep = read_deep_research(code)
    if deep and deep.get("deep_score") is not None:
        raw = deep["deep_score"]
        age = deep.get("age_days") or 0
        # fail-closed 过期治理阶梯：新鲜（stale=False）满权重；轻度过期（stale=True 但
        # 未超过 max_age_days + exclude_after_extra_days）沿用衰减、仍满权重参与合成；
        # 重度过期（超过阶梯上限）deep 维度退出合成加权，见 score_stock 的 scoring_available。
        severe_threshold = _DEEP_DEFAULT_MAX_AGE_DAYS + DEEP_STALENESS_EXCLUDE_AFTER_EXTRA_DAYS
        severe = bool(deep.get("stale")) and age > severe_threshold
        if severe:
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
        if deep.get("stale"):
            score = decay_stale_score(raw, pe_score, age)
            source = "serenity_deep_stale"
            tag = f"⚠️过期{age}天 "
        else:
            score = raw
            source = "serenity_deep"
            tag = ""
        up = deep.get("valuation_upside_pct")
        up_str = f", 中性赔率{up:+.0f}%" if isinstance(up, (int, float)) else ""
        pe_str = f"; PE={pe:.1f}" if pe is not None else ""
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
            "detail": f"{tag}投研{deep.get('rating') or ''}"
                      f"({deep.get('scorecard_total')}/100→{raw}){up_str}{pe_str}",
        }

    # 回退：PE 估值快照
    signals_detail = f"PE={pe:.1f}" if pe is not None else "PE缺失"
    if cap:
        signals_detail += f", 市值={cap:.0f}亿"
    signals_detail += "（无深研缓存，估值快照）"
    return {
        "score": round(pe_score, 1),
        "source": "valuation_snapshot",
        "stale": False,
        "pe": pe,
        "market_cap_yi": round(cap, 1) if cap else None,
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
    {"name": "S", "min": 8.0, "emoji": "🟢🟢🟢", "advice": "强烈推荐 — 多维度共振看多"},
    {"name": "A", "min": 6.0, "emoji": "🟢🟢", "advice": "推荐 — 技术面偏多，有催化支撑"},
    {"name": "B", "min": 4.0, "emoji": "🟢", "advice": "观察 — 中性偏多，等待信号确认"},
    {"name": "C", "min": 2.0, "emoji": "🔶", "advice": "谨慎 — 偏空信号，控制仓位"},
    {"name": "D", "min": 0.0, "emoji": "🔴", "advice": "回避 — 多维度利空"},
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
    """信号一致性检测：多空维度矛盾/共振判定（纯函数）。"""
    tech_score = scores["technical"]["score"]
    sent_score = scores["sentiment"]["score"]
    cata_score = scores["catalyst"]["score"]
    deep_score = scores["deep"]["score"]

    bullish_dims = sum(1 for s in [tech_score, sent_score, cata_score, deep_score] if s >= 6.5)
    bearish_dims = sum(1 for s in [tech_score, sent_score, cata_score, deep_score] if s <= 3.5)
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
    if tech_score >= 7 and deep_score <= 3:
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
    if (
        g == "S"
        and not str(strategy_id or "").startswith("daban")
        and (catalyst["score"] < 5.5 or deep["score"] < 6.0)
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
    """格式化完整推荐报告 v2。"""
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
    if extra:
        lines.append(f"**微结构：** {' | '.join(extra)}")
    lines.append(f"**PE：** {s['deep'].get('pe', 'N/A')}")
    lines.append("")

    # 4. ATR 自适应执行参考
    if t.get("price") and t.get("atr14"):
        price = float(t["price"])
        atr = float(t["atr14"])
        is_daban = lane == "daban"
        stop_mult = float(_risk_cfg.get("stop_loss_atr_mult_daban", 1.2)) if is_daban else float(_risk_cfg.get("stop_loss_atr_mult", 2.0))
        tp1_mult = float(_risk_cfg.get("take_profit_atr_mult", 3.0))
        tp2_mult = float(_risk_cfg.get("take_profit_atr_mult_2", 5.0))
        lines.extend([
            "## 执行参考（ATR自适应）",
            "| 项目 | 价位 | 距现价 |",
            "|------|------|--------|",
            f"| 止损 | {price - stop_mult * atr:.2f} | -{stop_mult * atr:.2f} ({stop_mult}×ATR) |",
            f"| 目标1 | {price + tp1_mult * atr:.2f} | +{tp1_mult * atr:.2f} ({tp1_mult}×ATR) |",
            f"| 目标2 | {price + tp2_mult * atr:.2f} | +{tp2_mult * atr:.2f} ({tp2_mult}×ATR) |",
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
    """短线入场时机判断（60分钟/30分钟级别）→ 结构化入场条件矩阵。"""
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
    if _chan is not None and k60 and len(k60) >= 20:
        try:
            chan_sigs60 = _chan.analyze(k60).get("signals", [])
        except Exception:  # noqa: BLE001
            chan_sigs60 = []
        c_delta, c_lock, c_notes = chan_adjustment(
            chan_sigs60, recent_window=8, total_bars=len(k60), allow_fn=_chan_allowed)
        score += c_delta
        signals.extend(c_notes)

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
    # 入场条件矩阵
    vol_ok = vol_r is not None and vol_r >= 1.5
    if has_macd_cross and vol_ok and rt.get("change_pct", 0) < 3:
        entry_conditions["immediate_buy"] = {
            "label": "满足条件立即执行",
            "conditions": ["60分钟MACD金叉", f"量比≥1.5({vol_r})", "高开<3%"],
        }
    if ma20_val and price and atr_val:
        pullback_zone = round(ma20_val, 2)
        if price > ma20_val:
            entry_conditions["pullback_buy"] = {
                "label": "回调到位再执行",
                "trigger_price": pullback_zone,
                "conditions": ["价格回踩MA20支撑", "RSI从超卖反弹", "量能萎缩"],
            }
    if prev_high and price and atr_val:
        entry_conditions["breakout_buy"] = {
            "label": "突破确认再执行",
            "trigger_price": round(prev_high, 2),
            "conditions": [f"突破近10日高点{prev_high:.2f}", "放量>1.5倍", "板块共振"],
        }

    if score >= 7:
        suggestion = "可入场"
        if "immediate_buy" in entry_conditions:
            suggestion = "立即入场"
    elif score >= 5:
        suggestion = "等待"
        if "pullback_buy" in entry_conditions:
            suggestion = f"等回调至{entry_conditions['pullback_buy'].get('trigger_price', 'MA20')}"
    else:
        suggestion = "暂不入场"

    return {
        "score": round(score, 1),
        "signals": signals,
        "suggestion": suggestion,
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
            print(f"⚡ **{args.name}({args.code}) 短线入场判断**")
            print(f"评分: {result['score']}/10 | 建议: **{result['suggestion']}**")
            print(f"现价: {result.get('price', 'N/A')}")
            print(f"信号: {result['detail']}")
    else:
        result = score_stock(args.code, args.name)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str))
        else:
            print(format_report(result))
