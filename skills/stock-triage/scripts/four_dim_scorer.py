#!/usr/bin/env python3
"""
四维打分引擎 — A股标的常规体检器
======================================
技术面 × 情绪面 × 催化面 × 深度面 → S/A/B/C 分级 + 买卖建议

⚠️ 定位说明（重要）：
  本引擎是**常规个股体检器**（趋势/估值/催化的综合体检），含估值(深度面 20%)维度，
  更贴合波段/中线持仓视角。它**不是打板引擎**——打板龙头的选股（连板梯队、封板质量、
  竞价封板、游资情绪周期）由 `hot-money-tactics` 负责。打板标的常处于"均线尚未多头排列、
  PE 高或无意义"的启动期，用本引擎打分会被技术/估值维度低估，请勿用本引擎给打板标的做
  最终决策。两个引擎职责分离：体检走这里，打板走 hot-money-tactics。

数据源（纯 urllib，cron-safe）：
- 腾讯 qt.gtimg.cn — 实时行情 + 历史K线
- SerpAPI — 新闻催化
- stock-analyst 技术指标模块 — numpy 计算

Usage:
  python3 four_dim_scorer.py 002156 通富微电
  python3 four_dim_scorer.py 002156 通富微电 --json
"""

import json
import sys
import os
import urllib.request
import urllib.parse
from datetime import datetime
from typing import Dict, Any, List, Tuple, Optional

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
from tradeability import assess_tradeability
from deep_research_cache import read_deep_research, decay_stale_score

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
from indicators import calc_ma, calc_ema, calc_macd, calc_rsi, calc_kdj


def fetch_tencent_realtime(code: str, market: str = "sz") -> Dict[str, Any]:
    """腾讯实时行情 — 委托 a_stock_http"""
    try:
        full_code = f"{market}{code}"
        result = _http_quote([full_code])
        if result and isinstance(result, dict):
            data = result.get(full_code)
            if data and isinstance(data, dict) and data.get("price") is not None:
                return data
        return {"error": "数据不完整"}
    except Exception as e:
        return {"error": str(e)}


def fetch_tencent_kline(code: str, market: str = "sz", days: int = 60, ktype: str = "day") -> List[Dict]:
    """腾讯历史K线 — 委托 a_stock_http"""
    try:
        return _http_kline(code, market, days, ktype)
    except Exception:
        return []


def fetch_serpapi_news(query: str, num: int = 5) -> Optional[List[Dict]]:
    """SerpAPI新闻。

    返回值区分"数据源不可用"与"可用但无新闻"：
      · None  → 无 API key 或请求出错（数据源不可用，不应据此判定"无催化"）
      · []    → 数据源可用但确实没有命中新闻（属于中性信息）
      · list  → 命中的新闻条目
    """
    api_key = os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        return None
    url = f"https://serpapi.com/search?engine=google_news&q={urllib.parse.quote(query)}&num={num}&api_key={api_key}&gl=cn&hl=zh-cn"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        return data.get("news_results", [])[:num]
    except Exception:
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
        return {"score": 5, "detail": "K线数据不足", "signals": []}

    closes = [k["close"] for k in klines]
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    volumes = [k["volume"] for k in klines]

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

    # 量能
    if vol_ma5[-1] and volumes[-1]:
        vol_ratio = volumes[-1] / vol_ma5[-1]
        if vol_ratio > 1.5:
            score += 0.5
            signals.append(f"放量({vol_ratio:.1f}x)")

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
        "price": rt.get("price"),
        "change_pct": rt.get("change_pct"),
        "detail": "; ".join(signals) if signals else "无明确信号",
    }


def score_sentiment(code: str, name: str, quote: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """情绪面评分（0-10）——基于板块热度和资金流向。quote 可注入复用。"""
    # 简化版：通过涨跌幅、换手率、连板数判断
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

    score = max(0, min(10, score))

    return {
        "score": round(score, 1),
        "change_pct": pct,
        "turnover": turnover,
        "amount_yi": round(amount / 1e8, 1) if amount else None,
        "detail": "; ".join(signals) if signals else "情绪中性",
    }


def score_catalyst(code: str, name: str) -> Dict[str, Any]:
    """催化面评分（0-10）——基于新闻和政策"""
    raw_news = fetch_serpapi_news(f"{name} {code} 政策 业绩 公告", num=5)
    source_available = raw_news is not None   # None=数据源不可用；[]=可用但无新闻
    news = raw_news or []

    score = 5.0
    signals = []
    catalysts = []

    # 关键词检测
    bullish_kw = ["业绩增长", "中标", "订单", "突破", "利好", "回购", "增持", "产能释放",
                  "政策支持", "国家战略", "补贴", "国产替代"]
    bearish_kw = ["减持", "亏损", "诉讼", "调查", "处罚", "利空", "暴雷", "退市",
                  "制裁", "限制", "产能过剩"]

    for item in news[:5]:
        title = item.get("title", "")
        snippet = item.get("snippet", "")
        text = title + " " + snippet
        catalysts.append({"title": title, "source": item.get("source", {}).get("name", ""),
                          "date": item.get("date", "")})

        for kw in bullish_kw:
            if kw in text:
                score += 0.8
                signals.append(f"利好催化: {title[:30]}...")
                break
        for kw in bearish_kw:
            if kw in text:
                score -= 0.8
                signals.append(f"⚠️ 利空信号: {title[:30]}...")
                break

    score = max(0, min(10, score))

    return {
        "score": round(score, 1),
        "news_count": len(news),
        "available": source_available,
        "catalysts": catalysts[:3],
        "detail": "; ".join(signals[:3]) if signals else "无显著催化",
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

# ========== 配置（从 scoring.yaml 加载）==========
import yaml

def _load_scoring_config():
    config_path = os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', 'config', 'scoring.yaml')
    try:
        with open(config_path) as f:
            return yaml.safe_load(f)
    except Exception:
        return None

_scoring_cfg = _load_scoring_config()

if _scoring_cfg:
    WEIGHTS = _scoring_cfg.get("scoring", {}).get("weights", {})
    RISK = _scoring_cfg.get("risk", {})
    _grades_cfg = _scoring_cfg.get("scoring", {}).get("grades", {})
    _confidence_cfg = _scoring_cfg.get("scoring", {}).get("confidence", {})
else:
    WEIGHTS = {"technical": 0.30, "sentiment": 0.25, "catalyst": 0.25, "deep": 0.20}
    RISK = {}
    _grades_cfg = {}
    _confidence_cfg = {}

# 从配置加载分级标准（按 min 值降序排列）
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
    key=lambda x: x["min"],
    reverse=True,
) if _grades_cfg else [
    {"name": "S", "min": 8.0, "emoji": "🟢🟢🟢", "advice": "强烈推荐 — 多维度共振看多"},
    {"name": "A", "min": 6.0, "emoji": "🟢🟢", "advice": "推荐 — 技术面偏多，有催化支撑"},
    {"name": "B", "min": 4.0, "emoji": "🟢", "advice": "观察 — 中性偏多，等待信号确认"},
    {"name": "C", "min": 2.0, "emoji": "🔶", "advice": "谨慎 — 偏空信号，控制仓位"},
    {"name": "D", "min": 0.0, "emoji": "🔴", "advice": "回避 — 多维度利空"},
]

# 从配置加载置信度规则（按顺序匹配）
CONFIDENCE_RULES = {}
if _confidence_cfg:
    for level in ["high", "medium", "low"]:
        cfg = _confidence_cfg.get(level)
        if cfg:
            CONFIDENCE_RULES[level] = {"requires": cfg.get("requires", [])}
if not CONFIDENCE_RULES:
    CONFIDENCE_RULES = {
        "high": {"requires": ["realtime", "kline", "news", "valuation"]},
        "medium": {"requires": ["realtime", "kline"]},
        "low": {"requires": ["realtime"]},
    }


def grade(weighted_score: float) -> Tuple[str, str, str]:
    """S/A/B/C 分级 — 从 GRADES 配置读取阈值"""
    for g in GRADES:
        if weighted_score >= g["min"]:
            return g["name"], g["emoji"], g["advice"]
    return "D", "🔴", "回避 — 多维度利空"


def score_stock(code: str, name: str, quote: Optional[Dict[str, Any]] = None,
                klines: Optional[List[Dict]] = None) -> Dict[str, Any]:
    """完整四维评分。quote/klines 可由批量调用方预取注入，同票只抓一次（4→1）。"""
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

    # 各维度数据是否真实可用（避免缺失维度以中性 5.0 静默稀释总分）
    available = {
        "technical": technical["ma5"] is not None,        # 有K线
        "sentiment": sentiment.get("change_pct") is not None,  # 有实时
        "catalyst": catalyst.get("available", catalyst["news_count"] > 0),  # 数据源可用（无新闻=中性，仍纳入）
        "deep": deep.get("source", "").startswith("serenity") or deep.get("pe") is not None,  # 有深研或估值
    }
    # 在可用维度上重新归一化权重；无可用维度时回退原始权重
    eff = {k: WEIGHTS[k] for k in WEIGHTS if available.get(k, True)}
    total_w = sum(eff.values())
    if total_w > 0:
        eff = {k: v / total_w for k, v in eff.items()}
    else:
        eff = dict(WEIGHTS)
    weighted = sum(scores[k]["score"] * eff.get(k, 0.0) for k in WEIGHTS)
    excluded = [k for k in WEIGHTS if not available.get(k, True)]

    g, emoji, advice = grade(weighted)

    # 计算置信度和数据覆盖率
    data_coverage = {
        "realtime": technical["price"] is not None,
        "kline": technical["ma5"] is not None,
        "news": catalyst["news_count"] > 0,
        "valuation": deep["pe"] is not None,
        "deep_research": deep.get("source", "").startswith("serenity") and not deep.get("stale", False),
    }

    # 按配置规则判定置信度（从高到低匹配）
    confidence = "low"
    for level in ["high", "medium", "low"]:
        required = CONFIDENCE_RULES[level]["requires"]
        if all(data_coverage.get(r, False) for r in required):
            confidence = level
            break

    # 低置信度时不给强烈买卖建议
    if confidence == "low":
        advice = "数据不足，无法给出方向性判断"
        emoji = "⚪"

    # 可成交性：封死一字板/停牌时，分数再高也无法买入，必须在建议中点明
    # 复用顶部预取的 quote，避免重复抓取
    trade = assess_tradeability(quote, code, name) if "error" not in quote else \
        {"tradeable": False, "status": "no_data", "reason": "行情缺失"}
    if trade.get("tradeable") is False and confidence != "low":
        advice = f"⛔ {trade['reason']}（{advice}）"

    return {
        "code": code,
        "name": name,
        "timestamp": datetime.now().isoformat(),
        "confidence": confidence,
        "data_coverage": data_coverage,
        "tradeability": trade,
        "scores": scores,
        "weighted": round(weighted, 1),
        "grade": g,
        "emoji": emoji,
        "advice": advice,
        "weights": {k: f"{v*100:.0f}%" for k, v in WEIGHTS.items()},
        "effective_weights": {k: f"{eff.get(k, 0)*100:.0f}%" for k in WEIGHTS},
        "excluded_dims": excluded,
        "deep_source": scores["deep"].get("source"),
    }


def format_report(result: Dict[str, Any]) -> str:
    """格式化报告"""
    s = result["scores"]
    lines = [
        f"🏆 **{result['name']}({result['code']})**",
        f"**加权总分：{result['weighted']}/10 | 等级：{result['grade']} | {result['emoji']} {result['advice']}**",
        "",
        "## 四维评分卡",
        "| 维度 | 权重 | 评分 | 关键发现 |",
        "|------|------|------|---------|",
        f"| 技术面 | {result['weights']['technical']} | {s['technical']['score']}/10 | {s['technical']['detail']} |",
        f"| 情绪面 | {result['weights']['sentiment']} | {s['sentiment']['score']}/10 | {s['sentiment']['detail']} |",
        f"| 催化面 | {result['weights']['catalyst']} | {s['catalyst']['score']}/10 | {s['catalyst']['detail']} |",
        f"| 深度面 | {result['weights']['deep']} | {s['deep']['score']}/10 | {s['deep']['detail']} |",
        "",
        f"**价格：** {s['technical'].get('price', 'N/A')} | "
        f"**涨跌：** {s['technical'].get('change_pct', 'N/A')}% | "
        f"**PE：** {s['deep'].get('pe', 'N/A')}",
    ]
    t = s['technical']
    if t.get('ma5') and t.get('ma20') and t.get('ma60'):
        lines.append(f"**MA5/20/60：** {t['ma5']} / {t['ma20']} / {t['ma60']}")
    if t.get('rsi6'):
        lines.append(f"**RSI(6)：** {t['rsi6']} | **RSI(14)：** {t.get('rsi14', 'N/A')}")

    return "\n".join(lines)


def score_short_term_entry(code: str, name: str) -> Dict:
    """短线入场时机判断（60分钟/30分钟级别）"""
    market = "sz" if code.startswith(("0", "3")) else "sh"
    rt = fetch_tencent_realtime(code, market)

    # 60分钟K线
    k60 = fetch_tencent_kline(code, market, 60, "60")
    # 30分钟K线
    k30 = fetch_tencent_kline(code, market, 60, "30")

    signals = []
    score = 5.0

    if k60 and len(k60) >= 20:
        closes_60 = [k["close"] for k in k60]
        ma20_60 = calc_ma(closes_60, 20)
        dif_60, dea_60, _ = calc_macd(closes_60, fast=12, slow=26, signal=9)

        if ma20_60[-1] and closes_60[-1] > ma20_60[-1]:
            score += 1.0
            signals.append("60分钟>MA20")
        if dif_60[-1] and dea_60[-1]:
            if dif_60[-1] > dea_60[-1]:
                if dif_60[-2] and dea_60[-2] and dif_60[-2] <= dea_60[-2]:
                    score += 2.0
                    signals.append("60分钟MACD金叉 ⭐")
            elif dif_60[-1] < dea_60[-1]:
                score -= 1.0
                signals.append("60分钟MACD空头")

    if k30 and len(k30) >= 20:
        closes_30 = [k["close"] for k in k30]
        rsi_30 = calc_rsi(closes_30, 14)
        if rsi_30[-1] is not None:
            if rsi_30[-1] < 30:
                score += 1.5
                signals.append(f"30分钟RSI超卖({rsi_30[-1]:.0f}) ⭐")
            elif rsi_30[-1] > 70:
                score -= 1.0
                signals.append(f"30分钟RSI超买({rsi_30[-1]:.0f})")

    # 缠论结构（60分钟级别，过 research_gate 才计权）
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

    suggestion = "可入场" if score >= 7 else ("等待" if score >= 5 else "暂不入场")

    return {
        "score": round(score, 1),
        "signals": signals,
        "suggestion": suggestion,
        "price": rt.get("price"),
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
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"⚡ **{args.name}({args.code}) 短线入场判断**")
            print(f"评分: {result['score']}/10 | 建议: **{result['suggestion']}**")
            print(f"现价: {result.get('price', 'N/A')}")
            print(f"信号: {result['detail']}")
    else:
        result = score_stock(args.code, args.name)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            print(format_report(result))
