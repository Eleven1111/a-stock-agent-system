#!/usr/bin/env python3
"""
四维打分引擎 — A股标的多维度自动评分
======================================
技术面 × 情绪面 × 催化面 × 深度面 → S/A/B/C 分级 + 买卖建议

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
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

# ========== 路径 ==========
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOCK_ANALYST = os.path.join(os.path.dirname(SKILL_DIR), "stock-analyst")
sys.path.insert(0, STOCK_ANALYST)

# ========== 数据抓取 ==========

def fetch_tencent_realtime(code: str, market: str = "sz") -> Dict[str, Any]:
    """腾讯实时行情"""
    url = f"http://qt.gtimg.cn/q={market}{code}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("gbk")
        parts = raw.split("=")[1].strip().strip('"').split("~")
        if len(parts) < 40:
            return {"error": "数据不完整"}
        return {
            "price": float(parts[3]) if parts[3] else None,
            "prev_close": float(parts[4]) if parts[4] else None,
            "open": float(parts[5]) if parts[5] else None,
            "high": float(parts[33]) if parts[33] else None,
            "low": float(parts[34]) if parts[34] else None,
            "change_pct": float(parts[32]) if parts[32] else None,
            "volume": float(parts[6]) if parts[6] else None,
            "amount": float(parts[37]) * 10000 if parts[37] else None,
            "turnover": float(parts[38]) if parts[38] else None,
            "pe": float(parts[39]) if parts[39] else None,
            "market_cap": float(parts[45]) if parts[45] else None,
        }
    except Exception as e:
        return {"error": str(e)}


def fetch_tencent_kline(code: str, market: str = "sz", days: int = 60, ktype: str = "day") -> List[Dict]:
    """腾讯历史K线 (day/week/month/60/30)"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={market}{code},{ktype},,,{days},qfq"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        key_map = {"day": "qfqday", "week": "qfqweek", "month": "qfqmonth",
                   "60": "qfq60", "30": "qfq30"}
        key = key_map.get(ktype, "qfqday")
        fallback = ktype if ktype in ("day","week","month") else "day"
        klines = (data.get("data", {}).get(f"{market}{code}", {}).get(key, []) or
                  data.get("data", {}).get(f"{market}{code}", {}).get(fallback, []))
        result = []
        for k in klines[-days:]:
            result.append({
                "date": k[0], "open": float(k[1]), "close": float(k[2]),
                "high": float(k[3]), "low": float(k[4]), "volume": float(k[5]),
            })
        return result
    except Exception:
        return []


def fetch_serpapi_news(query: str, num: int = 5) -> List[Dict]:
    """SerpAPI新闻"""
    api_key = os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        return []
    url = f"https://serpapi.com/search?engine=google_news&q={urllib.parse.quote(query)}&num={num}&api_key={api_key}&gl=cn&hl=zh-cn"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        return data.get("news_results", [])[:num]
    except Exception:
        return []


# ========== 技术指标（纯numpy） ==========

def calc_ma(closes: List[float], period: int) -> List[float]:
    """简单移动平均"""
    if len(closes) < period:
        return [None] * len(closes)
    result = [None] * (period - 1)
    for i in range(period - 1, len(closes)):
        result.append(sum(closes[i-period+1:i+1]) / period)
    return result


def calc_ema(closes: List[float], period: int) -> List[float]:
    """指数移动平均"""
    if len(closes) < 2:
        return [None] * len(closes)
    k = 2 / (period + 1)
    result = [closes[0]]
    for i in range(1, len(closes)):
        result.append(closes[i] * k + result[-1] * (1 - k))
    return result


def calc_macd(closes: List[float], fast=12, slow=26, signal=9) -> Tuple[List, List, List]:
    """MACD: DIF, DEA, MACD柱"""
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    dif = [f - s if f and s else None for f, s in zip(ema_fast, ema_slow)]
    dea = calc_ema([d for d in dif if d is not None], signal)
    # pad dea to match length
    dea_padded = [None] * (len(dif) - len(dea)) + dea
    macd_hist = [(d - de) * 2 if d is not None and de is not None else None
                 for d, de in zip(dif, dea_padded)]
    return dif, dea_padded, macd_hist


def calc_rsi(closes: List[float], period: int = 14) -> List[float]:
    """RSI"""
    if len(closes) < period + 1:
        return [None] * len(closes)
    result = [None] * period
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(diff if diff > 0 else 0)
        losses.append(-diff if diff < 0 else 0)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        if avg_loss == 0:
            result.append(100)
        else:
            rs = avg_gain / avg_loss
            result.append(100 - 100 / (1 + rs))
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    return result


def calc_kdj(highs, lows, closes, period=9):
    """KDJ"""
    n = len(closes)
    k_vals, d_vals, j_vals = [None]*n, [None]*n, [None]*n
    for i in range(period-1, n):
        hh = max(highs[i-period+1:i+1])
        ll = min(lows[i-period+1:i+1])
        rsv = (closes[i] - ll) / (hh - ll) * 100 if hh != ll else 50
        if k_vals[i-1] is None:
            k_vals[i] = 50
            d_vals[i] = 50
        else:
            k_vals[i] = 2/3 * k_vals[i-1] + 1/3 * rsv
            d_vals[i] = 2/3 * d_vals[i-1] + 1/3 * k_vals[i]
        j_vals[i] = 3 * k_vals[i] - 2 * d_vals[i]
    return k_vals, d_vals, j_vals


# ========== 四维评分 ==========

def score_technical(code: str, name: str) -> Dict[str, Any]:
    """技术面评分（0-10）"""
    market = "sz" if code.startswith(("0", "3")) else "sh"
    rt = fetch_tencent_realtime(code, market)
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

    score = max(-3, min(10, score))

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


def score_sentiment(code: str, name: str) -> Dict[str, Any]:
    """情绪面评分（0-10）——基于板块热度和资金流向"""
    # 简化版：通过涨跌幅、换手率、连板数判断
    market = "sz" if code.startswith(("0", "3")) else "sh"
    rt = fetch_tencent_realtime(code, market)

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
    news = fetch_serpapi_news(f"{name} {code} 政策 业绩 公告", num=5)

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
        "catalysts": catalysts[:3],
        "detail": "; ".join(signals[:3]) if signals else "无显著催化",
    }


def score_deep(code: str, name: str) -> Dict[str, Any]:
    """深度面评分（0-10）——估值、基本面快照"""
    market = "sz" if code.startswith(("0", "3")) else "sh"
    rt = fetch_tencent_realtime(code, market)

    score = 5.0
    pe = rt.get("pe")
    cap = rt.get("market_cap")

    if pe is not None:
        if 0 < pe < 15:
            score += 2.0
        elif 15 <= pe < 30:
            score += 1.0
        elif pe > 100:
            score -= 1.5
        elif pe < 0:
            score -= 1.0
            signals_detail = f"PE为负({pe:.1f})"
        else:
            signals_detail = f"PE={pe:.1f}"

    signals_detail = f"PE={pe:.1f}" if pe is not None else "PE缺失"
    if cap:
        signals_detail += f", 市值={cap:.0f}亿"

    score = max(0, min(10, score))

    return {
        "score": round(score, 1),
        "pe": pe,
        "market_cap_yi": round(cap, 1) if cap else None,
        "detail": signals_detail,
    }


# ========== 汇总 ==========

# ========== 配置 ==========
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
else:
    WEIGHTS = {"technical": 0.30, "sentiment": 0.25, "catalyst": 0.25, "deep": 0.20}
    RISK = {}

CONFIDENCE_RULES = {
    "high": {"requires": ["realtime", "kline", "news", "valuation"]},
    "medium": {"requires": ["realtime", "kline"]},
    "low": {"requires": ["realtime"]},
}


def grade(weighted_score: float) -> Tuple[str, str, str]:
    """S/A/B/C 分级"""
    if weighted_score >= 8.0:
        return "S", "🟢🟢🟢", "强烈推荐 — 多维度共振看多"
    elif weighted_score >= 6.0:
        return "A", "🟢🟢", "推荐 — 技术面偏多，有催化支撑"
    elif weighted_score >= 4.0:
        return "B", "🟢", "观察 — 中性偏多，等待信号确认"
    elif weighted_score >= 2.0:
        return "C", "🔶", "谨慎 — 偏空信号，控制仓位"
    else:
        return "D", "🔴", "回避 — 多维度利空"


def score_stock(code: str, name: str) -> Dict[str, Any]:
    """完整四维评分"""
    print(f"🔍 正在分析 {name}({code})...", file=sys.stderr)

    technical = score_technical(code, name)
    print(f"  技术面: {technical['score']}/10", file=sys.stderr)

    sentiment = score_sentiment(code, name)
    print(f"  情绪面: {sentiment['score']}/10", file=sys.stderr)

    catalyst = score_catalyst(code, name)
    print(f"  催化面: {catalyst['score']}/10", file=sys.stderr)

    deep = score_deep(code, name)
    print(f"  深度面: {deep['score']}/10", file=sys.stderr)

    weighted = (
        technical["score"] * WEIGHTS["technical"] +
        sentiment["score"] * WEIGHTS["sentiment"] +
        catalyst["score"] * WEIGHTS["catalyst"] +
        deep["score"] * WEIGHTS["deep"]
    )

    g, emoji, advice = grade(weighted)
    # 汇总
    # 计算置信度和数据覆盖率
    data_coverage = {
        "realtime": technical["price"] is not None,
        "kline": technical["ma5"] is not None,
        "news": catalyst["news_count"] > 0,
        "valuation": deep["pe"] is not None,
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

    return {
        "code": code,
        "name": name,
        "timestamp": datetime.now().isoformat(),
        "confidence": confidence,
        "data_coverage": data_coverage,
        "scores": {
            "technical": technical,
            "sentiment": sentiment,
            "catalyst": catalyst,
            "deep": deep,
        },
        "weighted": round(weighted, 1),
        "grade": g,
        "emoji": emoji,
        "advice": advice,
        "weights": {k: f"{v*100:.0f}%" for k, v in WEIGHTS.items()},
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

    score = max(0, min(10, score))

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
