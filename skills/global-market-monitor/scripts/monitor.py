#!/usr/bin/env python3
"""
Global Market Monitor — 全球市场数据采集引擎
=============================================
抓取美股、全球期货、外汇、VIX、国债收益率、关键个股、中国ADR、
全球指数、重大新闻，产出结构化 JSON + A股影响评估。

Usage:
  python3 monitor.py                  # 输出 JSON 到 stdout
  python3 monitor.py --json           # 同默认
  python3 monitor.py --summary        # 人类可读摘要
  python3 monitor.py --news           # 额外抓取新闻（需 Serper.dev）
  python3 monitor.py --all            # 全部数据 + 新闻

Cron-safe: 使用共享 http_client / yfinance（requests），不依赖 shell 命令。
"""

import json
import sys
import os
import subprocess
from datetime import datetime
from typing import Optional, Dict, Any, List

# ========== 配置 ==========
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path
from a_stock_http import load_hermes_env
from data_provider import (
    _next_serper_key,
    fetch_serper_news as _fetch_serper_news,
    provider_client,
)
from data_access_config import global_market_settings
import delivery_output
from paths import cache_dir as _cache_dir

CACHE_DIR = _cache_dir("global-market-monitor")
os.makedirs(CACHE_DIR, exist_ok=True)

MARKET_CONFIG = global_market_settings()
USE_YFINANCE = MARKET_CONFIG["switches"]["yfinance"]
USE_SINA = MARKET_CONFIG["switches"]["sina"]
USE_SERPER = MARKET_CONFIG["switches"].get("serper", True)
US_INDICES = MARKET_CONFIG["us_indices"]
US_SECTOR_ETFS = MARKET_CONFIG["us_sector_etfs"]
GLOBAL_INDICES = MARKET_CONFIG["global_indices"]
COMMODITIES = MARKET_CONFIG["commodities"]
FX = MARKET_CONFIG["fx"]
KEY_STOCKS = MARKET_CONFIG["key_stocks"]
CHINA_ADRS = MARKET_CONFIG["china_adrs"]
VIX_TICKER = MARKET_CONFIG["vix_ticker"]
TREASURY_TICKERS = MARKET_CONFIG["treasury_tickers"]
THRESHOLDS = MARKET_CONFIG["thresholds"]
# Global factors only generate a watchlist; stock-level QC remains mandatory.
A_SHARE_SECTOR_STOCK_MAP = MARKET_CONFIG["a_share_sector_stock_map"]


# ========== 数据采集 ==========

_YFINANCE_DISABLED_REASON: Optional[str] = None


def _is_yfinance_rate_limited(error: str) -> bool:
    """Yahoo/yfinance 限流识别。限流后本轮采集应快速降级，避免逐 ticker 阻塞。"""
    text = error.lower()
    return "429" in text or "too many requests" in text or "rate limit" in text


def _yfinance_error_payload(tickers: List[str], reason: str) -> Dict[str, Dict]:
    payload = {t: {"error": reason} for t in tickers}
    payload["_error"] = reason
    return payload


def fetch_yfinance_batch(tickers: List[str]) -> Dict[str, Dict]:
    """批量拉取 yfinance 数据"""
    global _YFINANCE_DISABLED_REASON
    if _YFINANCE_DISABLED_REASON:
        return _yfinance_error_payload(tickers, _YFINANCE_DISABLED_REASON)

    try:
        import yfinance as yf
        if hasattr(yf, "download"):
            try:
                worker = r'''
import json
import math
import sys
import yfinance as yf

tickers = json.loads(sys.stdin.read())
frame = yf.download(
    tickers=tickers,
    period="5d",
    interval="1d",
    group_by="ticker",
    auto_adjust=False,
    progress=False,
    threads=True,
    timeout=8,
)

def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None

results = {}
for ticker in tickers:
    try:
        if len(tickers) == 1:
            ticker_frame = frame[tickers[0]]
        elif ticker in frame.columns.get_level_values(0):
            ticker_frame = frame[ticker]
        else:
            ticker_frame = frame.xs(ticker, axis=1, level=1)
        closes = ticker_frame["Close"].dropna()
        if closes.empty:
            results[ticker] = {"error": "yfinance returned no prices"}
            continue
        price = number(closes.iloc[-1])
        previous = number(closes.iloc[-2]) if len(closes) > 1 else price
        latest = ticker_frame.loc[closes.index[-1]]
        results[ticker] = {
            "price": price,
            "prev_close": previous,
            "change_pct": ((price / previous) - 1) * 100 if price is not None and previous else None,
            "day_high": number(latest.get("High")),
            "day_low": number(latest.get("Low")),
            "name": ticker,
        }
    except Exception as exc:
        results[ticker] = {"error": str(exc)}
print(json.dumps(results))
'''
                # yfinance 访问 Yahoo Finance，不需要走中国数据源的 NO_PROXY 绕过
                yf_env = dict(os.environ)
                yf_env.pop("NO_PROXY", None)
                yf_env.pop("no_proxy", None)
                completed = subprocess.run(
                    [sys.executable, "-c", worker],
                    input=json.dumps(tickers),
                    capture_output=True,
                    text=True,
                    timeout=12,
                    env=yf_env,
                )
                if completed.returncode != 0 or not completed.stdout.strip():
                    raise RuntimeError(completed.stderr.strip() or "yfinance worker returned no data")
                results = json.loads(completed.stdout.strip().splitlines()[-1])
                if any(item.get("price") for item in results.values()):
                    return results
                _YFINANCE_DISABLED_REASON = "yfinance returned no usable batch data"
                return _yfinance_error_payload(tickers, _YFINANCE_DISABLED_REASON)
            except subprocess.TimeoutExpired:
                _YFINANCE_DISABLED_REASON = "yfinance batch timed out after 12s"
                return _yfinance_error_payload(tickers, _YFINANCE_DISABLED_REASON)
            except Exception as exc:
                error = str(exc)
                if _is_yfinance_rate_limited(error):
                    _YFINANCE_DISABLED_REASON = f"yfinance rate limited: {error[:160]}"
                else:
                    _YFINANCE_DISABLED_REASON = f"yfinance batch failed: {error[:160]}"
                return _yfinance_error_payload(tickers, _YFINANCE_DISABLED_REASON)

        # Lightweight test doubles may only implement Ticker.info.
        results = {}
        for idx, t in enumerate(tickers):
            try:
                tk = yf.Ticker(t)
                info = tk.info
                results[t] = {
                    "price": info.get("regularMarketPrice") or info.get("previousClose"),
                    "prev_close": info.get("regularMarketPreviousClose") or info.get("previousClose"),
                    "change_pct": info.get("regularMarketChangePercent"),
                    "day_high": info.get("regularMarketDayHigh"),
                    "day_low": info.get("regularMarketDayLow"),
                    "name": info.get("shortName") or info.get("longName"),
                }
            except Exception as e:
                err = str(e)
                results[t] = {"error": err}
                if _is_yfinance_rate_limited(err):
                    _YFINANCE_DISABLED_REASON = f"yfinance rate limited: {err[:160]}"
                    for remaining in tickers[idx + 1:]:
                        results[remaining] = {"error": _YFINANCE_DISABLED_REASON}
                    results["_error"] = _YFINANCE_DISABLED_REASON
                    break
        return results
    except ImportError:
        return _yfinance_error_payload(tickers, "yfinance not installed")


def fetch_sina_us_indices() -> Dict[str, Dict]:
    """新浪财经美股指数备用数据源"""
    def safe_float(value: str) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    sina_map = {
        config["sina_code"]: code
        for code, config in US_INDICES.items()
        if config.get("sina_code")
    }
    codes = ",".join(sina_map.keys())
    url = f"https://hq.sinajs.cn/list={codes}"
    try:
        raw = provider_client("sina").request_text(
            url,
            encoding="gbk",
            headers={
                "Referer": "https://finance.sina.com.cn",
                "User-Agent": "Mozilla/5.0",
            },
        ).data
    except Exception as e:
        return {"_error": f"Sina fetch failed: {e}"}

    results = {}
    for line in raw.strip().split("\n"):
        if "=" not in line:
            continue
        parts = line.split("=")
        raw_code = parts[0].strip().removeprefix("var hq_str_")
        code = sina_map.get(raw_code)
        if code is None:
            continue
        data = parts[1].strip().strip('";').split(",") if len(parts) > 1 else []
        if len(data) < 3:
            continue
        results[code] = {
            "name": data[0],
            "price": safe_float(data[1]),
            "change_pct": safe_float(data[2]),
            "change": safe_float(data[3]) if len(data) > 3 else None,
        }
    return results


def fetch_serper_news(query: str = "global market breaking news financial", num: int = 5) -> List[Dict]:
    """通过 serper.dev 抓取重大新闻"""
    api_key = _next_serper_key()
    if not api_key:
        return [{"error": "SERPER_API_KEY not set"}]
    try:
        result = _fetch_serper_news(query, api_key, num)
        return [
            {
                "title": item.get("title"),
                "source": item.get("source"),
                "date": item.get("date"),
                "snippet": item.get("snippet"),
                "link": item.get("link"),
            }
            for item in result.data
        ]
    except Exception as e:
        return [{"error": f"serper fetch failed: {e}"}]


def fetch_geopolitical_news() -> List[Dict]:
    """抓取地缘政治相关新闻"""
    queries = [
        "geopolitical conflict sanctions trade war",
        "natural disaster earthquake flood hurricane supply chain",
        "OPEC oil production cut increase",
        "Federal Reserve interest rate policy",
    ]
    all_news = []
    for q in queries:
        news = fetch_serper_news(q, num=2)
        all_news.extend(news)
    return all_news


def fetch_natural_disasters() -> List[Dict]:
    """检查全球重大自然灾害（USGS地震 + GDACS）"""
    disasters = []

    # 1. USGS 地震 API（过去24小时 ≥4.5级）
    try:
        url = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
        eq_data = provider_client("usgs").request_json(
            url,
            headers={"User-Agent": "GlobalMarketMonitor/1.0"},
        ).data
        for eq in eq_data.get("features", []):
            mag = eq["properties"]["mag"]
            if mag >= 6.0:
                place = eq["properties"]["place"]
                disasters.append({
                    "type": "地震",
                    "magnitude": mag,
                    "location": place,
                    "time": eq["properties"]["time"],
                    "a_impact": _earthquake_impact(place),
                    "alert_level": "🔴" if mag >= 7.0 else "🟡",
                })
    except Exception:
        pass

    # 2. GDACS (Global Disaster Alert and Coordination System) RSS
    try:
        url = "https://www.gdacs.org/xml/rss.xml"
        raw = provider_client("gdacs").request_text(
            url,
            encoding="utf-8",
            headers={"User-Agent": "GlobalMarketMonitor/1.0"},
        ).data.lower()
        for keyword, disaster_type, impact in [
            ("tropical cyclone", "飓风/气旋", ["石油", "石化", "保险"]),
            ("volcano", "火山喷发", ["航空", "保险"]),
            ("flood", "洪水", ["农业", "保险", "建材"]),
            ("tsunami", "海啸", ["保险", "核电", "航运"]),
            ("drought", "干旱", ["农业", "食品", "电力"]),
        ]:
            if keyword in raw:
                disasters.append({
                    "type": disaster_type,
                    "source": "GDACS",
                    "a_impact": impact,
                    "alert_level": "🟡",
                })
    except Exception:
        pass

    return disasters


def _earthquake_impact(place: str) -> List[str]:
    """根据地震位置判断对A股板块影响"""
    p = place.lower()
    if any(w in p for w in ["japan", "taiwan", "日本", "台湾"]):
        return ["半导体", "消费电子"]  # 台日半导体重镇
    elif any(w in p for w in ["chile", "peru", "智利", "秘鲁"]):
        return ["有色"]  # 铜矿重镇
    elif any(w in p for w in ["indonesia", "malaysia", "印度尼西亚", "马来西亚"]):
        return ["橡胶", "有色", "农业"]
    elif any(w in p for w in ["gulf", "mexico", "texas", "墨西哥湾"]):
        return ["石油", "石化"]
    elif any(w in p for w in ["china", "中国", "sichuan", "yunnan", "xinjiang"]):
        return ["保险", "建材", "机械"]
    return ["保险"]


# ========== 分析引擎 ==========

def compute_deviation(current: Optional[float], prev: Optional[float]) -> Optional[Dict]:
    """计算偏离度"""
    if current is None or prev is None or prev == 0:
        return None
    pct = ((current - prev) / prev) * 100
    return {"pct": round(pct, 2), "abs": round(current - prev, 2)}


def assess_impact(data: Dict[str, Any]) -> Dict[str, Any]:
    """评估全球市场对A股的影响"""
    alerts = []
    sector_impact = {}  # sector -> impact_score

    # 1. VIX 恐慌指数
    vix = data.get("vix", {})
    vix_price = vix.get("price")
    if vix_price:
        if vix_price >= THRESHOLDS["vix_extreme"]:
            alerts.append({"level": "🔴 高", "msg": f"VIX={vix_price:.1f}，极度恐慌，外资风险偏好可能下降，A股成长风格承压",
                           "sectors": ["全市场"], "action": "减仓观望，等待VIX回落"})
            for s in ["AI算力", "半导体", "消费电子", "券商金融"]:
                sector_impact[s] = sector_impact.get(s, 0) - 3
        elif vix_price >= THRESHOLDS["vix_fear"]:
            alerts.append({"level": "🟡 中", "msg": f"VIX={vix_price:.1f}，恐慌情绪升温，成长股承压",
                           "sectors": ["AI算力", "半导体", "消费电子"], "action": "控制仓位，关注防御板块"})
            for s in ["AI算力", "半导体", "消费电子"]:
                sector_impact[s] = sector_impact.get(s, 0) - 2
            for s in ["电力", "公用事业", "黄金"]:
                sector_impact[s] = sector_impact.get(s, 0) + 1

    # 2. 美股指数
    for idx_key, label in [("^GSPC", "标普500"), ("^IXIC", "纳斯达克"), ("^DJI", "道琼斯")]:
        idx = data.get("us_indices", {}).get(idx_key, {})
        pct = idx.get("change_pct")
        if pct is not None:
            if abs(pct) >= THRESHOLDS["index_move_major"]:
                direction = "暴跌" if pct < 0 else "暴涨"
                alerts.append({"level": "🔴 高", "msg": f"{label}{direction}{abs(pct):.1f}%，风险偏好可能向A股开盘传导",
                               "sectors": ["全市场"], "action": "关注开盘情绪"})
                for s in ["AI算力", "半导体", "消费电子", "券商金融"]:
                    sector_impact[s] = sector_impact.get(s, 0) + (3 if pct > 0 else -3)
            elif abs(pct) >= THRESHOLDS["index_move_notable"]:
                direction = "跌" if pct < 0 else "涨"
                alerts.append({"level": "🟡 中", "msg": f"{label}{direction}{abs(pct):.1f}%",
                               "sectors": ["AI算力", "半导体"]})
                for s in ["AI算力", "半导体"]:
                    sector_impact[s] = sector_impact.get(s, 0) + (1 if pct > 0 else -1)

    # 3. 纳指 vs 道指 分化（科技 vs 传统轮动信号）
    nasdaq = data.get("us_indices", {}).get("^IXIC", {}).get("change_pct")
    dow = data.get("us_indices", {}).get("^DJI", {}).get("change_pct")
    if nasdaq is not None and dow is not None:
        spread = nasdaq - dow
        if spread > THRESHOLDS["technology_spread_notable"]:
            alerts.append({"level": "ℹ️", "msg": f"科技强于传统（纳指-道指={spread:.1f}%），A股科技板块或受益",
                           "sectors": ["AI算力", "半导体", "消费电子"]})
            for s in ["AI算力", "半导体", "消费电子"]:
                sector_impact[s] = sector_impact.get(s, 0) + 1
        elif spread < -THRESHOLDS["technology_spread_notable"]:
            alerts.append({"level": "ℹ️", "msg": f"传统强于科技（纳指-道指={spread:.1f}%），A股防御板块或受益",
                           "sectors": ["电力", "银行", "消费"]})
            for s in ["电力", "银行", "消费"]:
                sector_impact[s] = sector_impact.get(s, 0) + 1

    # 4. 美债收益率
    tnx = data.get("treasuries", {}).get("^TNX", {})
    tnx_price = tnx.get("price")
    tnx_change = tnx.get("change_pct")
    if tnx_price is not None and tnx_change is not None:
        if tnx_change > THRESHOLDS["yield_move_notable"] / 100:  # 收益率飙升
            alerts.append({"level": "🟡 中", "msg": f"10Y美债收益率飙升{tnx_change*100:.0f}bp至{tnx_price:.2f}%，高估值板块承压",
                           "sectors": ["AI算力", "半导体", "新能源"], "action": "关注成长股估值回调"})
            for s in ["AI算力", "半导体", "新能源"]:
                sector_impact[s] = sector_impact.get(s, 0) - 2
            for s in ["银行", "金融"]:
                sector_impact[s] = sector_impact.get(s, 0) + 1

    # 5. 美元/人民币
    cny = data.get("fx", {}).get("CNY=X", {})
    cny_price = cny.get("price")
    cny_change = cny.get("change_pct")
    if cny_price is not None and cny_change is not None:
        if cny_change > THRESHOLDS["fx_move_notable"]:  # 人民币贬值
            alerts.append({"level": "🟡 中", "msg": f"人民币贬值至{cny_price:.2f}（+{cny_change:.2f}%），外资风险偏好可能承压",
                           "sectors": ["外贸", "家电", "纺织"], "action": "关注外资风险偏好与开盘资金承接"})
            for s in ["外贸", "家电", "纺织"]:
                sector_impact[s] = sector_impact.get(s, 0) + 1
            for s in ["航空", "造纸"]:
                sector_impact[s] = sector_impact.get(s, 0) - 1
        elif cny_change < -THRESHOLDS["fx_move_notable"]:  # 人民币升值
            alerts.append({"level": "ℹ️", "msg": f"人民币升值至{cny_price:.2f}（{cny_change:.2f}%），外资风险偏好可能改善",
                           "sectors": ["航空", "造纸", "金融"]})
            for s in ["航空", "造纸", "金融"]:
                sector_impact[s] = sector_impact.get(s, 0) + 1

    # 6. 原油
    oil = data.get("commodities", {}).get("CL=F", {})
    oil_pct = oil.get("change_pct")
    if oil_pct is not None:
        if oil_pct > THRESHOLDS["oil_move_notable"]:
            alerts.append({"level": "🟡 中", "msg": f"原油暴涨{oil_pct:.1f}%，关注石油/石化板块，利空航空/交运",
                           "sectors": ["石油", "石化"]})
            for s in ["石油", "石化", "煤炭"]:
                sector_impact[s] = sector_impact.get(s, 0) + 2
            for s in ["航空", "交运"]:
                sector_impact[s] = sector_impact.get(s, 0) - 2
        elif oil_pct < -THRESHOLDS["oil_move_notable"]:
            alerts.append({"level": "ℹ️", "msg": f"原油大跌{abs(oil_pct):.1f}%，利好航空/交运/化工",
                           "sectors": ["航空", "交运", "化工"]})
            for s in ["航空", "交运", "化工"]:
                sector_impact[s] = sector_impact.get(s, 0) + 2
            for s in ["石油", "石化"]:
                sector_impact[s] = sector_impact.get(s, 0) - 1

    # 7. 黄金
    gold = data.get("commodities", {}).get("GC=F", {})
    gold_pct = gold.get("change_pct")
    if gold_pct is not None:
        if gold_pct > THRESHOLDS["gold_move_notable"]:
            alerts.append({"level": "🟡 中", "msg": f"黄金大涨{gold_pct:.1f}%，避险情绪升温，黄金股受益",
                           "sectors": ["黄金", "贵金属"]})
            for s in ["黄金", "贵金属"]:
                sector_impact[s] = sector_impact.get(s, 0) + 2

    # 8. 中概股ADR
    adr_alerts = []
    for code, cfg in CHINA_ADRS.items():
        adr = data.get("china_adrs", {}).get(code, {})
        pct = adr.get("change_pct")
        if pct is not None and abs(pct) >= THRESHOLDS["adr_move_notable"]:
            direction = "跌" if pct < 0 else "涨"
            adr_alerts.append(f"{cfg['name']}({code}){direction}{abs(pct):.1f}%")
            for sector in cfg["a_impact"]:
                sector_impact[sector] = sector_impact.get(sector, 0) + (2 if pct > 0 else -2)
    if adr_alerts:
        alerts.append({"level": "🟡 中", "msg": f"中概股异动：{', '.join(adr_alerts)}",
                       "sectors": ["互联网", "电商", "新能源车"], "action": "关注外资对中概股态度"})

    # 9. 关键科技股
    for code, cfg in KEY_STOCKS.items():
        stock = data.get("key_stocks", {}).get(code, {})
        pct = stock.get("change_pct")
        if pct is not None and abs(pct) >= THRESHOLDS["key_stock_move_notable"]:
            direction = "跌" if pct < 0 else "涨"
            alerts.append({"level": "ℹ️", "msg": f"{cfg['name']}({code}){direction}{abs(pct):.1f}%，可能影响A股{', '.join(cfg['a_impact'])}",
                           "sectors": cfg["a_impact"]})
            for sector in cfg["a_impact"]:
                sector_impact[sector] = sector_impact.get(sector, 0) + (2 if pct > 0 else -2)

    # 10. 大宗商品综合
    copper = data.get("commodities", {}).get("HG=F", {})
    copper_pct = copper.get("change_pct")
    if copper_pct is not None and abs(copper_pct) >= THRESHOLDS["copper_move_notable"]:
        direction = "跌" if copper_pct < 0 else "涨"
        alerts.append({"level": "ℹ️", "msg": f"铜价{direction}{abs(copper_pct):.1f}%（铜博士），反映全球需求预期",
                       "sectors": ["有色", "电网", "新能源"]})
        for sector in ["有色", "电网", "新能源"]:
            sector_impact[sector] = sector_impact.get(sector, 0) + (1 if copper_pct > 0 else -1)

    # 11. 美股行业ETF → A股板块联动
    for code, cfg in US_SECTOR_ETFS.items():
        etf = data.get("us_sectors", {}).get(code, {})
        pct = etf.get("change_pct")
        if pct is not None and abs(pct) >= THRESHOLDS["sector_etf_move_notable"]:
            direction = "涨" if pct > 0 else "跌"
            impact = cfg["a_impact"]
            alerts.append({"level": "🟡" if abs(pct) >= THRESHOLDS["sector_etf_move_major"] else "ℹ️",
                           "msg": f"美股{cfg['name']}板块{direction}{abs(pct):.1f}%，联动A股{', '.join(impact)}",
                           "sectors": impact})
            for s in impact:
                sector_impact[s] = sector_impact.get(s, 0) + (2 if pct > 0 else -2)

    # 12. 全球指数联动
    for code, cfg in GLOBAL_INDICES.items():
        idx = data.get("global_indices", {}).get(code, {})
        pct = idx.get("change_pct")
        if pct is not None and abs(pct) >= THRESHOLDS["global_index_move_notable"]:
            direction = "涨" if pct > 0 else "跌"
            name = cfg["name"]
            if code == "^HSI":
                alerts.append({"level": "🟡 中", "msg": f"恒生指数{direction}{abs(pct):.1f}%，可能影响A股开盘风险偏好",
                               "sectors": ["全市场"]})
            elif code in ("^N225", "^KS11"):
                alerts.append({"level": "ℹ️", "msg": f"{name}{direction}{abs(pct):.1f}%，亚太情绪传导"})
            else:
                alerts.append({"level": "ℹ️", "msg": f"{name}{direction}{abs(pct):.1f}%，欧洲市场信号"})

    # 13. 自然灾害影响
    disasters = data.get("disasters", [])
    for d in disasters:
        mag_str = f" {d['magnitude']}级" if d.get("magnitude") else ""
        alerts.append({
            "level": d.get("alert_level", "🟡"),
            "msg": f"{d['type']}{mag_str}于{d.get('location', d.get('source', ''))}，可能影响A股{', '.join(d.get('a_impact', []))}",
            "sectors": d.get("a_impact", []),
            "action": "关注供应链扰动和保险赔付"
        })
        for s in d.get("a_impact", []):
            sector_impact[s] = sector_impact.get(s, 0) + 1

    # 14. 重大新闻快速扫描
    news_items = data.get("news", [])
    if news_items:
        # 关键词触发：美联储/加息/降息/制裁/冲突/关税
        fed_keywords = ["fed", "federal reserve", "rate cut", "rate hike", "interest rate",
                        "美联储", "加息", "降息", "利率"]
        sanctions_keywords = ["sanction", "tariff", "trade war", "export control",
                              "制裁", "关税", "贸易战", "出口管制"]
        conflict_keywords = ["war", "conflict", "missile", "invasion", "military",
                             "战争", "冲突", "导弹", "军事"]
        for item in news_items[:8]:
            title = (item.get("title") or "").lower()
            snippet = (item.get("snippet") or "").lower()
            text = title + " " + snippet
            if any(kw in text for kw in conflict_keywords):
                alerts.append({"level": "🔴 高",
                               "msg": f"地缘政治风险：{item.get('title', '')}",
                               "sectors": ["军工", "黄金", "石油"], "action": "关注局势升级"})
                for s in ["军工", "黄金", "石油"]:
                    sector_impact[s] = sector_impact.get(s, 0) + 3
                break  # 只报一次
        for item in news_items[:8]:
            title = (item.get("title") or "").lower()
            snippet = (item.get("snippet") or "").lower()
            text = title + " " + snippet
            if any(kw in text for kw in fed_keywords):
                alerts.append({"level": "🟡 中",
                               "msg": f"美联储动态：{item.get('title', '')}",
                               "sectors": ["AI算力", "半导体", "券商金融"],
                               "action": "关注利率预期变化对成长股的影响"})
                break

    # 汇总
    return {
        "alerts": alerts,
        "sector_impact": sector_impact,
        "a_share_analysis": build_a_share_analysis(sector_impact, alerts),
        "summary": generate_summary(alerts, sector_impact),
    }


def build_a_share_analysis(
    sector_impact: Dict[str, float],
    alerts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    ordered = sorted(
        ((sector, score) for sector, score in sector_impact.items() if score),
        key=lambda item: (-abs(item[1]), item[0]),
    )
    sector_views = [
        {
            "sector": sector,
            "direction": "bullish" if score > 0 else "bearish",
            "impact_score": score,
            "confidence": "high" if abs(score) >= 3 else "medium",
            "evidence": [
                alert.get("msg")
                for alert in alerts
                if sector in (alert.get("sectors") or [])
            ][:3],
        }
        for sector, score in ordered[:8]
    ]

    stock_watchlist = []
    seen = set()
    for view in sector_views:
        for code, name in A_SHARE_SECTOR_STOCK_MAP.get(view["sector"], []):
            if code in seen:
                continue
            seen.add(code)
            stock_watchlist.append({
                "code": code,
                "name": name,
                "sector": view["sector"],
                "direction": view["direction"],
                "impact_score": view["impact_score"],
                "advice": "watch_only_pending_stock_qc",
                "reason": f"全球因子映射至{view['sector']}，尚未完成个股公告/可成交性质检",
            })

    return {
        "schema": "global_to_a_share_analysis_v1",
        "sector_views": sector_views,
        "stock_watchlist": stock_watchlist[:12],
        "risk_note": "全球联动只生成观察名单，不可直接转成买入建议；个股需通过09:35质检。",
    }


def generate_summary(alerts: List[Dict], sector_impact: Dict[str, float]) -> str:
    """生成一句话影响摘要"""
    if not alerts:
        return "全球市场平静，无明显异动信号"

    high_alerts = [a for a in alerts if "🔴" in a.get("level", "")]
    mid_alerts = [a for a in alerts if "🟡" in a.get("level", "")]

    if high_alerts:
        return f"⚠️ {len(high_alerts)}个高风险信号：{high_alerts[0]['msg'][:50]}..."

    if mid_alerts:
        top_positive = sorted(sector_impact.items(), key=lambda x: x[1], reverse=True)[:3]
        top_negative = sorted(sector_impact.items(), key=lambda x: x[1])[:3]
        pos_str = "、".join([f"{s}(+{v})" for s, v in top_positive if v > 0])
        neg_str = "、".join([f"{s}({v})" for s, v in top_negative if v < 0])
        parts = []
        if pos_str:
            parts.append(f"利好：{pos_str}")
        if neg_str:
            parts.append(f"利空：{neg_str}")
        return "；".join(parts) if parts else "全球市场小幅波动，无明显方向性信号"

    return "全球市场小幅波动，无明显方向性信号"


# ========== 主流程 ==========

def collect_all_data(include_news: bool = False) -> Dict[str, Any]:
    """采集全部数据"""
    load_hermes_env()
    global _YFINANCE_DISABLED_REASON
    _YFINANCE_DISABLED_REASON = None  # 每次 run 重新尝试，不跨 run 禁用
    result = {
        "timestamp": datetime.now().isoformat(),
        "timezone": "Asia/Shanghai",
        "source_health": {},
        "us_indices": {},
        "vix": {},
        "treasuries": {},
        "us_sectors": {},
        "global_indices": {},
        "commodities": {},
        "fx": {},
        "key_stocks": {},
        "china_adrs": {},
        "news": [],
        "geopolitical_news": [],
        "disasters": [],
        "impact": {},
    }
    source_health: Dict[str, Dict[str, Any]] = {}
    yf_data: Dict[str, Dict[str, Any]] = {}

    # 1. 美股指数（yfinance 主 + Sina 备用）
    if USE_YFINANCE:
        try:
            import yfinance as yf  # noqa: F401
            bootstrap_indices = [
                code
                for code, config in US_INDICES.items()
                if config.get("weight") == "major"
            ][:2]
            yf_data = fetch_yfinance_batch(bootstrap_indices)
            yf_error = yf_data.get("_error")
            market_values = [
                d for k, d in yf_data.items()
                if k != "_error" and isinstance(d, dict)
            ]
            ok_count = sum(1 for d in market_values if d.get("price"))
            if ok_count >= 2:
                remaining_indices = [code for code in US_INDICES if code not in yf_data]
                if remaining_indices:
                    yf_data.update(fetch_yfinance_batch(remaining_indices))
                source_health["yfinance"] = {"status": "ok", "indices_ok": ok_count}
            elif ok_count > 0:
                source_health["yfinance"] = {"status": "degraded", "indices_ok": ok_count}
            elif yf_error:
                source_health["yfinance"] = {"status": "failed", "error": yf_error}
            else:
                _YFINANCE_DISABLED_REASON = "yfinance returned no usable index data"
                source_health["yfinance"] = {"status": "failed", "error": _YFINANCE_DISABLED_REASON}
        except ImportError:
            source_health["yfinance"] = {"status": "failed", "error": "yfinance not installed"}
            yf_data = {}
        except Exception as e:
            source_health["yfinance"] = {"status": "failed", "error": str(e)}
            yf_data = {}

        for code, cfg in US_INDICES.items():
            d = yf_data.get(code, {})
            if d.get("error"):
                result["us_indices"][code] = {"name": cfg["name"], "error": d["error"]}
            else:
                result["us_indices"][code] = {
                    "name": cfg["name"],
                    "price": d.get("price"),
                    "prev_close": d.get("prev_close"),
                    "change_pct": d.get("change_pct"),
                    "day_high": d.get("day_high"),
                    "day_low": d.get("day_low"),
                }
    else:
        source_health["yfinance"] = {"status": "disabled"}

    missing_indices = [
        code
        for code in US_INDICES
        if not result["us_indices"].get(code, {}).get("price")
    ]
    if USE_SINA and missing_indices:
        sina_data = fetch_sina_us_indices()
        sina_error = sina_data.get("_error")
        sina_ok = 0
        for code in missing_indices:
            value = sina_data.get(code, {})
            if not value.get("price"):
                continue
            sina_ok += 1
            result["us_indices"][code] = {
                "name": US_INDICES[code]["name"],
                "price": value.get("price"),
                "prev_close": None,
                "change_pct": value.get("change_pct"),
                "day_high": None,
                "day_low": None,
            }
        if sina_ok:
            source_health["sina"] = {"status": "ok", "indices_ok": sina_ok}
        else:
            source_health["sina"] = {
                "status": "failed",
                "error": sina_error or "Sina returned no usable index data",
            }
    elif not USE_SINA:
        source_health["sina"] = {"status": "disabled"}

    # 2. VIX
    if USE_YFINANCE:
        vix_data = fetch_yfinance_batch([VIX_TICKER])
        v = vix_data.get(VIX_TICKER, {})
        result["vix"] = {
            "price": v.get("price"),
            "prev_close": v.get("prev_close"),
            "change_pct": v.get("change_pct"),
        }

    # 3. 美债
    if USE_YFINANCE:
        long_treasury = "^TNX"
        yf_treasury = fetch_yfinance_batch([long_treasury])
        t = yf_treasury.get(long_treasury, {})
        result["treasuries"][long_treasury] = {
            "name": TREASURY_TICKERS[long_treasury]["name"],
            "price": t.get("price"),
            "change_pct": t.get("change_pct"),
        }
        # 2Y via special format
        short_treasury = TREASURY_TICKERS["2YY"]["format"]
        t2y = fetch_yfinance_batch([short_treasury])
        t2 = t2y.get(short_treasury, {})
        if t2.get("price"):
            result["treasuries"]["2YY"] = {
                "name": TREASURY_TICKERS["2YY"]["name"],
                "price": t2["price"],
                "change_pct": t2.get("change_pct"),
            }

    # 4. 行业ETF
    if USE_YFINANCE:
        etf_data = fetch_yfinance_batch(list(US_SECTOR_ETFS.keys()))
        for code, cfg in US_SECTOR_ETFS.items():
            d = etf_data.get(code, {})
            if d.get("price"):
                result["us_sectors"][code] = {
                    "name": cfg["name"],
                    "price": d.get("price"),
                    "change_pct": d.get("change_pct"),
                    "a_impact": cfg["a_impact"],
                }

    # 5. 全球指数
    if USE_YFINANCE:
        gl_data = fetch_yfinance_batch(list(GLOBAL_INDICES.keys()))
        for code, cfg in GLOBAL_INDICES.items():
            d = gl_data.get(code, {})
            if d.get("price"):
                result["global_indices"][code] = {
                    "name": cfg["name"],
                    "region": cfg["region"],
                    "price": d.get("price"),
                    "change_pct": d.get("change_pct"),
                }

    # 6. 大宗商品
    if USE_YFINANCE:
        comm_data = fetch_yfinance_batch(list(COMMODITIES.keys()))
        for code, cfg in COMMODITIES.items():
            d = comm_data.get(code, {})
            if d.get("price"):
                result["commodities"][code] = {
                    "name": cfg["name"],
                    "unit": cfg["unit"],
                    "price": d.get("price"),
                    "change_pct": d.get("change_pct"),
                    "a_impact": cfg.get("a_impact", []),
                }

    # 7. 外汇
    if USE_YFINANCE:
        fx_data = fetch_yfinance_batch(list(FX.keys()))
        for code, cfg in FX.items():
            d = fx_data.get(code, {})
            if d.get("price"):
                result["fx"][code] = {
                    "name": cfg["name"],
                    "price": d.get("price"),
                    "change_pct": d.get("change_pct"),
                }

    # 8. 关键科技股
    if USE_YFINANCE:
        key_data = fetch_yfinance_batch(list(KEY_STOCKS.keys()))
        for code, cfg in KEY_STOCKS.items():
            d = key_data.get(code, {})
            if d.get("price"):
                result["key_stocks"][code] = {
                    "name": cfg["name"],
                    "price": d.get("price"),
                    "change_pct": d.get("change_pct"),
                    "a_impact": cfg["a_impact"],
                }

    # 9. 中概股ADR
    if USE_YFINANCE:
        adr_data = fetch_yfinance_batch(list(CHINA_ADRS.keys()))
        for code, cfg in CHINA_ADRS.items():
            d = adr_data.get(code, {})
            if d.get("price"):
                result["china_adrs"][code] = {
                    "name": cfg["name"],
                    "price": d.get("price"),
                    "change_pct": d.get("change_pct"),
                    "a_impact": cfg["a_impact"],
                }

    # 10. 新闻（默认启用）
    if USE_SERPER:
        try:
            result["news"] = fetch_serper_news()
            result["geopolitical_news"] = fetch_geopolitical_news()
            news_ok = bool(result["news"]) and not any(
                isinstance(item, dict) and "error" in item for item in result["news"]
            )
            if news_ok:
                source_health["serper"] = {"status": "ok"}
            elif not _next_serper_key():
                source_health["serper"] = {"status": "failed", "error": "SERPER_API_KEY not set"}
            else:
                source_health["serper"] = {"status": "failed", "error": "no news results"}
        except Exception as exc:
            source_health["serper"] = {"status": "failed", "error": str(exc)}
    else:
        source_health["serper"] = {"status": "disabled"}

    # 11. 自然灾害（免费API，始终启用）
    try:
        result["disasters"] = fetch_natural_disasters()
        source_health["usgs"] = {"status": "ok"}
    except Exception as exc:
        source_health["usgs"] = {"status": "failed", "error": str(exc)}

    # 12. 数据质量门禁：关键市场数据不足时禁止方向性判断
    yf_status = source_health.get("yfinance", {}).get("status", "unknown")
    sina_status = source_health.get("sina", {}).get("status", "unknown")
    us_idx_count = sum(1 for value in result["us_indices"].values() if value.get("price"))
    vix_ok = bool(result.get("vix", {}).get("price"))
    index_source_ok = yf_status in {"ok", "degraded"} or sina_status == "ok"
    critical_ok = (index_source_ok and us_idx_count >= 2) or (us_idx_count >= 1 and vix_ok)
    result["source_health"] = source_health
    if not critical_ok:
        result["impact"] = {
            "status": "insufficient_data",
            "alerts": [],
            "sector_impact": {},
            "summary": "关键市场数据不足：美股指数/VIX/美债不可用，禁止输出方向性A股判断",
        }
        return result

    # 13. 影响评估
    result["impact"] = assess_impact(result)

    return result


def print_summary(data: Dict[str, Any]):
    """人类可读摘要输出"""
    print("═" * 60)
    print("🌍 全球市场监控")
    print(f"⏰ 更新时间：{data['timestamp']}")
    print("═" * 60)

    # 美股指数
    print("\n📊 美股指数")
    for code, idx in data.get("us_indices", {}).items():
        pct = idx.get("change_pct")
        emoji = "🟢" if pct and pct > 0 else ("🔴" if pct and pct < 0 else "⚪")
        pct_str = f"{pct:+.2f}%" if pct is not None else "N/A"
        print(f"  {emoji} {idx.get('name', code)}: {idx.get('price', 'N/A')} ({pct_str})")

    # VIX
    vix = data.get("vix", {})
    print(f"\n😱 VIX恐慌指数: {vix.get('price', 'N/A')}")
    if vix.get("price"):
        if vix["price"] >= THRESHOLDS["vix_extreme"]:
            print("  ⚠️ 极度恐慌！")
        elif vix["price"] >= THRESHOLDS["vix_fear"]:
            print("  ⚡ 恐慌情绪升温")

    # 美债
    print("\n🏦 美债收益率")
    for code, t in data.get("treasuries", {}).items():
        print(f"  {t.get('name', code)}: {t.get('price', 'N/A')}%")

    # 大宗商品
    print("\n🛢️ 大宗商品")
    for code, c in data.get("commodities", {}).items():
        pct = c.get("change_pct")
        pct_str = f"({pct:+.2f}%)" if pct is not None else ""
        print(f"  {c.get('name', code)}: {c.get('price', 'N/A')} {c.get('unit', '')} {pct_str}")

    # 外汇
    print("\n💱 外汇")
    for code, fx in data.get("fx", {}).items():
        pct = fx.get("change_pct")
        pct_str = f"({pct:+.2f}%)" if pct is not None else ""
        print(f"  {fx.get('name', code)}: {fx.get('price', 'N/A')} {pct_str}")

    # 中概股
    print("\n🇨🇳 中概股ADR")
    for code, adr in data.get("china_adrs", {}).items():
        pct = adr.get("change_pct")
        emoji = "🟢" if pct and pct > 0 else ("🔴" if pct and pct < 0 else "⚪")
        pct_str = f"{pct:+.2f}%" if pct is not None else "N/A"
        print(f"  {emoji} {adr.get('name', code)}: {adr.get('price', 'N/A')} ({pct_str})")

    # 关键科技股
    print("\n🔧 关键科技股")
    for code, s in data.get("key_stocks", {}).items():
        pct = s.get("change_pct")
        emoji = "🟢" if pct and pct > 0 else ("🔴" if pct and pct < 0 else "⚪")
        pct_str = f"{pct:+.2f}%" if pct is not None else "N/A"
        print(f"  {emoji} {s.get('name', code)}: {s.get('price', 'N/A')} ({pct_str})")

    # 美股行业ETF
    sectors = data.get("us_sectors", {})
    if sectors:
        notable = {
            k: v
            for k, v in sectors.items()
            if v.get("change_pct")
            and abs(v["change_pct"]) >= THRESHOLDS["summary_move_notable"]
        }
        if notable:
            print("\n🇺🇸 美股行业ETF（异动>1%）")
            for code, s in sorted(notable.items(), key=lambda x: abs(x[1]["change_pct"] or 0), reverse=True)[:5]:
                pct = s.get("change_pct")
                emoji = "🟢" if pct and pct > 0 else "🔴"
                print(f"  {emoji} {s.get('name', code)}: {pct:+.2f}%")

    # 全球指数
    gl = data.get("global_indices", {})
    if gl:
        notable_gl = {
            k: v
            for k, v in gl.items()
            if v.get("change_pct")
            and abs(v["change_pct"]) >= THRESHOLDS["summary_move_notable"]
        }
        if notable_gl:
            print("\n🌏 全球指数（异动>1%）")
            for code, idx in notable_gl.items():
                pct = idx.get("change_pct")
                emoji = "🟢" if pct and pct > 0 else "🔴"
                print(f"  {emoji} {idx.get('name', code)} ({idx.get('region', '')}): {pct:+.2f}%")

    # 自然灾害
    disasters = data.get("disasters", [])
    if disasters:
        print("\n🌪️ 重大自然灾害")
        for d in disasters:
            mag_str = f" {d['magnitude']}级" if d.get("magnitude") else ""
            loc = d.get("location") or d.get("source", "")
            print(f"  {d.get('alert_level', '🟡')} {d['type']}{mag_str} — {loc}")
            if d.get("a_impact"):
                print(f"     → 可能影响A股：{', '.join(d['a_impact'])}")

    # 影响评估
    impact = data.get("impact", {})
    if impact.get("alerts"):
        print("\n⚡ A股影响评估")
        for alert in impact["alerts"]:
            print(f"  {alert['level']} {alert['msg']}")

    a_share_analysis = impact.get("a_share_analysis") or {}
    if a_share_analysis.get("sector_views"):
        print("\n🎯 A股板块传导")
        for view in a_share_analysis["sector_views"][:6]:
            print(
                f"  {view['sector']}: {view['direction']} "
                f"(影响分 {view['impact_score']:+.1f}, {view['confidence']})"
            )
    if a_share_analysis.get("stock_watchlist"):
        print("\n🔎 个股观察映射（待个股质检）")
        for stock in a_share_analysis["stock_watchlist"][:8]:
            print(
                f"  {stock['name']}({stock['code']}) | {stock['sector']} | "
                f"{stock['direction']} | 仅观察"
            )

    if impact.get("summary"):
        print(f"\n📝 一句话总结：{impact['summary']}")

    print("\n" + "═" * 60)


def has_delivery_anomaly(data: Dict[str, Any]) -> bool:
    impact = data.get("impact") or {}
    return impact.get("status") == "insufficient_data" or bool(impact.get("alerts"))


def delivery_summary_payload(data: Dict[str, Any], job_id: str) -> Dict[str, Any]:
    impact = data.get("impact") or {}
    vix = ((data.get("data") or {}).get("vix") or {}).get("^VIX") or {}
    fx = ((data.get("data") or {}).get("fx") or {}).get("USDCNH=X") or {}
    return {
        "schema": "delivery_summary_v1",
        "job_id": job_id,
        "status": impact.get("status") or "ready",
        "summary": (
            f"全球盘前 {str(data.get('timestamp') or '')[:10]}："
            f"VIX={vix.get('price', 'NA')}；离岸人民币={fx.get('price', 'NA')}；"
            "无跨市场异常。"
        ),
        "alerts": [],
    }


# ========== 入口 ==========

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Global Market Monitor")
    parser.add_argument("--json", action="store_true", default=True, help="JSON output (default)")
    parser.add_argument("--summary", action="store_true", help="Human-readable summary")
    parser.add_argument("--news", action="store_true", help="Include news fetching")
    parser.add_argument("--all", action="store_true", help="Full data + news")
    parser.add_argument("--cache", action="store_true",
                        help="把大盘影响落入共享缓存，供 four_dim 个股评分 overlay")
    parser.add_argument("--delivery-job-id", help="启用该 cron job 的无异常摘要投递")
    args = parser.parse_args()

    include_news = args.news or args.all

    if args.all:
        USE_SERPER = True

    data = collect_all_data(include_news=include_news)

    if args.cache:
        impact = data.get("impact")
        if isinstance(impact, dict) and impact.get("status") != "insufficient_data":
            try:
                from market_context import write_market_context
                write_market_context(impact)
            except Exception as _e:  # noqa: BLE001
                print(f"market_context 写入失败: {_e}", file=__import__("sys").stderr)

    if args.summary or args.all:
        if args.delivery_job_id:
            import io
            from contextlib import redirect_stdout

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                print_summary(data)
            print(delivery_output.maybe_summarize_text(
                buffer.getvalue(),
                delivery_summary_payload(data, args.delivery_job_id)["summary"],
                job_id=args.delivery_job_id,
                has_anomaly=has_delivery_anomaly(data),
            ))
        else:
            print_summary(data)
    else:
        if args.delivery_job_id:
            print(delivery_output.maybe_summarize_json(
                data,
                delivery_summary_payload(data, args.delivery_job_id),
                job_id=args.delivery_job_id,
                has_anomaly=has_delivery_anomaly(data),
            ))
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
