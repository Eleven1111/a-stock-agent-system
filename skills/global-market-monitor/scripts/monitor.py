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
  python3 monitor.py --news           # 额外抓取新闻（需 SerpAPI）
  python3 monitor.py --all            # 全部数据 + 新闻

Cron-safe: 使用 urllib / yfinance（requests），不依赖 shell 命令。
"""

import json
import sys
import os
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

# ========== 配置 ==========
CACHE_DIR = os.path.expanduser("~/.hermes/skills/global-market-monitor/cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# 数据源开关
USE_YFINANCE = True      # yfinance（美股/期货/外汇主要来源）
USE_SINA = True           # 新浪财经（美股指数备用）
USE_SERPAPI = True        # SerpAPI新闻（默认启用）

# ========== 监控标的定义 ==========
US_INDICES = {
    "^GSPC":  {"name": "标普500",    "sina_code": "gb_$spx",   "weight": "major"},
    "^IXIC":  {"name": "纳斯达克",   "sina_code": "gb_$ixic",  "weight": "major"},
    "^DJI":   {"name": "道琼斯",     "sina_code": "gb_$dji",   "weight": "major"},
    "^RUT":   {"name": "罗素2000",   "sina_code": None,         "weight": "minor"},
}

US_SECTOR_ETFS = {
    "XLK":  {"name": "科技",         "a_impact": ["AI算力", "半导体", "消费电子"]},
    "XLF":  {"name": "金融",         "a_impact": ["券商金融", "银行"]},
    "XLE":  {"name": "能源",         "a_impact": ["石油", "煤炭", "新能源"]},
    "XLV":  {"name": "医疗健康",     "a_impact": ["医药", "医疗"]},
    "XLI":  {"name": "工业",         "a_impact": ["军工航天", "机械", "汽车"]},
    "XLY":  {"name": "可选消费",     "a_impact": ["汽车", "家电", "消费"]},
    "XLP":  {"name": "必需消费",     "a_impact": ["食品饮料", "农业"]},
    "XLB":  {"name": "原材料",       "a_impact": ["有色", "化工", "钢铁"]},
    "XLRE": {"name": "房地产",       "a_impact": ["地产", "建材"]},
    "XLU":  {"name": "公用事业",     "a_impact": ["电力", "电网"]},
}

GLOBAL_INDICES = {
    "^N225":  {"name": "日经225",     "region": "asia"},
    "^KS11":  {"name": "韩国KOSPI",   "region": "asia"},
    "^HSI":   {"name": "恒生指数",    "region": "asia"},
    "^GDAXI": {"name": "德国DAX",     "region": "europe"},
    "^FTSE":  {"name": "英国富时100", "region": "europe"},
    "^FCHI":  {"name": "法国CAC40",   "region": "europe"},
}

COMMODITIES = {
    "GC=F":  {"name": "黄金",   "unit": "美元/盎司", "a_impact": ["黄金", "贵金属"]},
    "SI=F":  {"name": "白银",   "unit": "美元/盎司", "a_impact": ["贵金属", "有色"]},
    "HG=F":  {"name": "铜",     "unit": "美元/磅",   "a_impact": ["有色", "电网", "新能源"]},
    "CL=F":  {"name": "原油WTI","unit": "美元/桶",   "a_impact": ["石油", "石化", "航空", "交运"]},
    "NG=F":  {"name": "天然气", "unit": "美元/MMBtu", "a_impact": ["天然气", "化工"]},
    "ZC=F":  {"name": "玉米",   "unit": "美分/蒲式耳","a_impact": ["农业", "养殖"]},
    "ZS=F":  {"name": "大豆",   "unit": "美分/蒲式耳","a_impact": ["农业", "养殖"]},
    "ZW=F":  {"name": "小麦",   "unit": "美分/蒲式耳","a_impact": ["农业", "食品"]},
}

FX = {
    "CNY=X":  {"name": "美元/人民币", "a_impact_up": ["外贸", "家电", "纺织"], "a_impact_down": ["航空", "造纸"]},
    "DX-Y.NYB": {"name": "美元指数",   "a_impact": ["有色", "黄金", "大宗商品"]},
}

KEY_STOCKS = {
    "NVDA":  {"name": "英伟达",    "a_impact": ["AI算力", "半导体"]},
    "AAPL":  {"name": "苹果",      "a_impact": ["消费电子", "果链"]},
    "MSFT":  {"name": "微软",      "a_impact": ["AI算力", "云计算"]},
    "TSLA":  {"name": "特斯拉",    "a_impact": ["新能源车", "汽车零部件"]},
    "AMD":   {"name": "AMD",       "a_impact": ["半导体", "AI算力"]},
    "SMCI":  {"name": "超微电脑",  "a_impact": ["AI算力", "服务器"]},
}

CHINA_ADRS = {
    "BABA": {"name": "阿里巴巴", "a_impact": ["互联网", "电商", "云计算"]},
    "JD":   {"name": "京东",     "a_impact": ["电商", "物流"]},
    "PDD":  {"name": "拼多多",   "a_impact": ["电商", "消费"]},
    "BIDU": {"name": "百度",     "a_impact": ["AI", "互联网"]},
    "NIO":  {"name": "蔚来",     "a_impact": ["新能源车"]},
}

VIX_TICKER = "^VIX"
TREASURY_TICKERS = {"^TNX": {"name": "10年期美债", "a_impact": ["科技", "成长股", "金融"]},
                    "2YY":   {"name": "2年期美债", "format": "2YY=F"}}

# 影响阈值
THRESHOLDS = {
    "vix_fear": 25,          # VIX 超过此值视为恐慌
    "vix_extreme": 30,       # VIX 超过此值触发警报
    "index_move_notable": 1.0,  # 指数涨跌超过此%视为值得关注
    "index_move_major": 2.0,    # 指数涨跌超过此%视为重大
    "oil_move_notable": 3.0,    # 油价涨跌超过此%视为值得关注
    "gold_move_notable": 2.0,   # 金价涨跌超过此%视为值得关注
    "yield_move_notable": 5,    # 收益率变动超过此bp视为值得关注 (0.05%)
    "fx_move_notable": 0.5,     # 汇率变动超过此%视为值得关注
    "adr_move_notable": 3.0,    # 中概股ADR变动超过此%视为值得关注
}


# ========== 数据采集 ==========

def fetch_yfinance_batch(tickers: List[str]) -> Dict[str, Dict]:
    """批量拉取 yfinance 数据"""
    try:
        import yfinance as yf
        results = {}
        for t in tickers:
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
                results[t] = {"error": str(e)}
        return results
    except ImportError:
        return {"_error": "yfinance not installed"}


def fetch_sina_us_indices() -> Dict[str, Dict]:
    """新浪财经美股指数备用数据源"""
    sina_map = {
        "gb_$dji":  "^DJI",
        "gb_$ixic": "^IXIC",
        "gb_$spx":  "^GSPC",
    }
    codes = ",".join(sina_map.keys())
    url = f"https://hq.sinajs.cn/list={codes}"
    req = urllib.request.Request(url, headers={
        "Referer": "https://finance.sina.com.cn",
        "User-Agent": "Mozilla/5.0"
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("gbk")
    except Exception as e:
        return {"_error": f"Sina fetch failed: {e}"}

    results = {}
    for line in raw.strip().split("\n"):
        if "=" not in line:
            continue
        parts = line.split("=")
        code = parts[0].strip().split("_")[-1] if "hq_str_gb_" in parts[0] else parts[0].strip()
        data = parts[1].strip('"').split(",") if len(parts) > 1 else []
        if len(data) < 4:
            continue
        results[code] = {
            "name": data[0],
            "price": float(data[1]) if data[1] else None,
            "change_pct": float(data[2]) if data[2] else None,
            "change": float(data[3]) if data[3] else None,
        }
    return results


def fetch_serpapi_news(query: str = "global market breaking news financial", num: int = 5) -> List[Dict]:
    """通过 SerpAPI 抓取重大新闻"""
    api_key = os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        return [{"error": "SERPAPI_API_KEY not set"}]

    url = f"https://serpapi.com/search?engine=google_news&q={urllib.parse.quote(query)}&num={num}&api_key={api_key}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        return [{"error": f"SerpAPI fetch failed: {e}"}]

    news = []
    for item in data.get("news_results", [])[:num]:
        news.append({
            "title": item.get("title"),
            "source": item.get("source", {}).get("name"),
            "date": item.get("date"),
            "snippet": item.get("snippet"),
            "link": item.get("link"),
        })
    return news


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
        news = fetch_serpapi_news(q, num=2)
        all_news.extend(news)
    return all_news


def fetch_natural_disasters() -> List[Dict]:
    """检查全球重大自然灾害（USGS地震 + GDACS）"""
    disasters = []

    # 1. USGS 地震 API（过去24小时 ≥4.5级）
    try:
        url = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
        req = urllib.request.Request(url, headers={"User-Agent": "GlobalMarketMonitor/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            eq_data = json.loads(resp.read().decode())
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
        req = urllib.request.Request(url, headers={"User-Agent": "GlobalMarketMonitor/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode().lower()
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
            alerts.append({"level": "🔴 高", "msg": f"VIX={vix_price:.1f}，极度恐慌！外资大概率流出，A股全面承压",
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
                alerts.append({"level": "🔴 高", "msg": f"{label}{direction}{abs(pct):.1f}%！A股明日大概率跟跌/跟涨",
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
        if spread > 1.0:
            alerts.append({"level": "ℹ️", "msg": f"科技强于传统（纳指-道指={spread:.1f}%），A股科技板块或受益",
                           "sectors": ["AI算力", "半导体", "消费电子"]})
            for s in ["AI算力", "半导体", "消费电子"]:
                sector_impact[s] = sector_impact.get(s, 0) + 1
        elif spread < -1.0:
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
            alerts.append({"level": "🟡 中", "msg": f"人民币贬值至{cny_price:.2f}（+{cny_change:.2f}%），北向资金可能流出",
                           "sectors": ["外贸", "家电", "纺织"], "action": "关注北向资金动向"})
            for s in ["外贸", "家电", "纺织"]:
                sector_impact[s] = sector_impact.get(s, 0) + 1
            for s in ["航空", "造纸"]:
                sector_impact[s] = sector_impact.get(s, 0) - 1
        elif cny_change < -THRESHOLDS["fx_move_notable"]:  # 人民币升值
            alerts.append({"level": "ℹ️", "msg": f"人民币升值至{cny_price:.2f}（{cny_change:.2f}%），北向资金可能流入",
                           "sectors": ["航空", "造纸", "金融"]})

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
    if adr_alerts:
        alerts.append({"level": "🟡 中", "msg": f"中概股异动：{', '.join(adr_alerts)}",
                       "sectors": ["互联网", "电商", "新能源车"], "action": "关注外资对中概股态度"})

    # 9. 关键科技股
    for code, cfg in KEY_STOCKS.items():
        stock = data.get("key_stocks", {}).get(code, {})
        pct = stock.get("change_pct")
        if pct is not None and abs(pct) >= 5.0:
            direction = "跌" if pct < 0 else "涨"
            alerts.append({"level": "ℹ️", "msg": f"{cfg['name']}({code}){direction}{abs(pct):.1f}%，可能影响A股{', '.join(cfg['a_impact'])}",
                           "sectors": cfg["a_impact"]})

    # 10. 大宗商品综合
    copper = data.get("commodities", {}).get("HG=F", {})
    copper_pct = copper.get("change_pct")
    if copper_pct is not None and abs(copper_pct) >= 2.0:
        direction = "跌" if copper_pct < 0 else "涨"
        alerts.append({"level": "ℹ️", "msg": f"铜价{direction}{abs(copper_pct):.1f}%（铜博士），反映全球需求预期",
                       "sectors": ["有色", "电网", "新能源"]})

    # 11. 美股行业ETF → A股板块联动
    for code, cfg in US_SECTOR_ETFS.items():
        etf = data.get("us_sectors", {}).get(code, {})
        pct = etf.get("change_pct")
        if pct is not None and abs(pct) >= 2.0:
            direction = "涨" if pct > 0 else "跌"
            impact = cfg["a_impact"]
            alerts.append({"level": "🟡" if abs(pct) >= 3.0 else "ℹ️",
                           "msg": f"美股{cfg['name']}板块{direction}{abs(pct):.1f}%，联动A股{', '.join(impact)}",
                           "sectors": impact})
            for s in impact:
                sector_impact[s] = sector_impact.get(s, 0) + (2 if pct > 0 else -2)

    # 12. 全球指数联动
    for code, cfg in GLOBAL_INDICES.items():
        idx = data.get("global_indices", {}).get(code, {})
        pct = idx.get("change_pct")
        if pct is not None and abs(pct) >= 2.0:
            direction = "涨" if pct > 0 else "跌"
            name = cfg["name"]
            if code == "^HSI":
                alerts.append({"level": "🟡 中", "msg": f"恒生指数{direction}{abs(pct):.1f}%，A股大概率同向",
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
        "summary": generate_summary(alerts, sector_impact),
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
    result = {
        "timestamp": datetime.now().isoformat(),
        "timezone": "Asia/Shanghai",
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

    # 1. 美股指数（yfinance 主 + Sina 备用）
    if USE_YFINANCE:
        yf_data = fetch_yfinance_batch(list(US_INDICES.keys()))
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
        yf_treasury = fetch_yfinance_batch(["^TNX"])
        t = yf_treasury.get("^TNX", {})
        result["treasuries"]["^TNX"] = {
            "name": "10年期美债收益率",
            "price": t.get("price"),
            "change_pct": t.get("change_pct"),
        }
        # 2Y via special format
        t2y = fetch_yfinance_batch(["2YY=F"])
        t2 = t2y.get("2YY=F", {})
        if t2.get("price"):
            result["treasuries"]["2YY"] = {
                "name": "2年期美债收益率",
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
    if USE_SERPAPI:
        result["news"] = fetch_serpapi_news()
        result["geopolitical_news"] = fetch_geopolitical_news()

    # 11. 自然灾害（免费API，始终启用）
    result["disasters"] = fetch_natural_disasters()

    # 12. 影响评估
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
        if vix["price"] >= 30:
            print("  ⚠️ 极度恐慌！")
        elif vix["price"] >= 25:
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
        notable = {k: v for k, v in sectors.items() if v.get("change_pct") and abs(v["change_pct"]) >= 1.0}
        if notable:
            print("\n🇺🇸 美股行业ETF（异动>1%）")
            for code, s in sorted(notable.items(), key=lambda x: abs(x[1]["change_pct"] or 0), reverse=True)[:5]:
                pct = s.get("change_pct")
                emoji = "🟢" if pct and pct > 0 else "🔴"
                print(f"  {emoji} {s.get('name', code)}: {pct:+.2f}%")

    # 全球指数
    gl = data.get("global_indices", {})
    if gl:
        notable_gl = {k: v for k, v in gl.items() if v.get("change_pct") and abs(v["change_pct"]) >= 1.0}
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
        print(f"\n⚡ A股影响评估")
        for alert in impact["alerts"]:
            print(f"  {alert['level']} {alert['msg']}")

    if impact.get("summary"):
        print(f"\n📝 一句话总结：{impact['summary']}")

    print("\n" + "═" * 60)


# ========== 入口 ==========

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Global Market Monitor")
    parser.add_argument("--json", action="store_true", default=True, help="JSON output (default)")
    parser.add_argument("--summary", action="store_true", help="Human-readable summary")
    parser.add_argument("--news", action="store_true", help="Include news fetching")
    parser.add_argument("--all", action="store_true", help="Full data + news")
    args = parser.parse_args()

    include_news = args.news or args.all

    if args.all:
        USE_SERPAPI = True

    data = collect_all_data(include_news=include_news)

    if args.summary or args.all:
        print_summary(data)
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))