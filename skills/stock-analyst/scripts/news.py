"""
股票新闻模块
数据源：SerpAPI Google News（需要 SERPAPI_API_KEY 环境变量）
"""

import os
import json
import re
import urllib.request
import urllib.parse
from datetime import datetime
from typing import Optional, List, Dict

# SerpAPI multi-key rotation
SERPAPI_KEYS = []
_keys_str = os.environ.get("SERPAPI_KEYS") or ""
if _keys_str:
    SERPAPI_KEYS = [k.strip() for k in _keys_str.split(",") if k.strip()]
# 也尝试从 .env 文件读取
if not SERPAPI_KEYS:
    try:
        with open(os.path.expanduser("~/.hermes/.env")) as f:
            for line in f:
                if line.startswith("SERPAPI_KEYS="):
                    _keys_str = line.split("=", 1)[1].strip().strip("'").strip('"')
                    SERPAPI_KEYS = [k.strip() for k in _keys_str.split(",") if k.strip()]
                    break
    except:
        pass

_KEY_INDEX = 0

# 自动加载 NO_PROXY，绕过 Clash 代理的 DNS 劫持
if not os.environ.get("NO_PROXY"):
    try:
        with open(os.path.expanduser("~/.hermes/.env")) as f:
            for line in f:
                if line.startswith("NO_PROXY="):
                    os.environ["NO_PROXY"] = line.split("=", 1)[1].strip().strip("'").strip('"')
                    break
    except:
        pass


def _get_next_key() -> str:
    """轮询获取下一个 SerpAPI key"""
    global _KEY_INDEX
    if not SERPAPI_KEYS:
        return ""
    key = SERPAPI_KEYS[_KEY_INDEX % len(SERPAPI_KEYS)]
    _KEY_INDEX += 1
    return key


def _serpapi_request(params: dict) -> Optional[Dict]:
    """通用 SerpAPI 请求（自动轮询多 key）"""
    api_key = _get_next_key()
    if not api_key:
        return {"error": "SERPAPI_KEYS 未配置"}
    
    params["api_key"] = api_key
    params["hl"] = "zh-CN"
    params["gl"] = "cn"
    
    url = f"https://serpapi.com/search.json?{urllib.parse.urlencode(params)}"
    
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}


def search_stock_news(code: str = "", name: str = "", max_results=8) -> List[Dict]:
    """搜索个股新闻"""
    query = f"{name} {code} A股 2026" if name else f"{code} A股"
    
    data = _serpapi_request({
        "engine": "google_news",
        "q": query,
        "num": max_results,
    })
    
    if not data or "error" in data:
        return [{"title": f"新闻获取失败: {data.get('error', '未知错误')}", "source": "", "date": "", "url": ""}]
    
    news = data.get("news_results", [])
    results = []
    for n in news[:max_results]:
        results.append({
            "title": n.get("title", ""),
            "source": n.get("source", {}).get("name", ""),
            "date": n.get("date", ""),
            "url": n.get("link", n.get("url", "")),
            "snippet": n.get("snippet", ""),
        })
    return results


def search_sector_news(sector: str, max_results=6) -> List[Dict]:
    """搜索板块新闻"""
    query = f"{sector}板块 A股"
    
    data = _serpapi_request({
        "engine": "google_news",
        "q": query,
        "num": max_results,
    })
    
    if not data or "error" in data:
        return []
    
    news = data.get("news_results", [])
    results = []
    for n in news[:max_results]:
        results.append({
            "title": n.get("title", ""),
            "source": n.get("source", {}).get("name", ""),
            "date": n.get("date", ""),
            "url": n.get("link", n.get("url", "")),
            "snippet": n.get("snippet", ""),
        })
    return results


def search_market_news(max_results=10) -> List[Dict]:
    """搜索A股大盘新闻"""
    data = _serpapi_request({
        "engine": "google_news",
        "q": "A股 行情 热点 2026",
        "num": max_results,
    })
    
    if not data or "error" in data:
        return []
    
    news = data.get("news_results", [])
    results = []
    for n in news[:max_results]:
        results.append({
            "title": n.get("title", ""),
            "source": n.get("source", {}).get("name", ""),
            "date": n.get("date", ""),
            "url": n.get("link", n.get("url", "")),
            "snippet": n.get("snippet", ""),
        })
    return results


# ─── 资金流向信号提取（从新闻摘要中） ───

FUND_FLOW_PATTERNS = [
    (r'(主力|机构|北向|外资|融资)(?:资金)?(净买入|净流出|净卖出|净流入)([^，。]*?)(\d+\.?\d*)\s*(?:亿元|万元)', None),
    (r'(净买入|净卖出|净流入|净流出)[^，。]*?(\d+\.?\d*)\s*(?:亿元|万元)', None),
    (r'(融资).*?(加仓|减仓)(\d+)', None),
    (r'(北向|外资).*?(净买入|净卖出|净流入|净流出).*?(\d+\.?\d*)\s*(?:亿元|万元)', None),
]

def extract_fund_flow(text: str) -> list:
    """从文本中提取资金流向信号"""
    signals = []
    for pattern, _ in FUND_FLOW_PATTERNS:
        for m in re.finditer(pattern, text):
            groups = m.groups()
            if len(groups) == 4:  # 主力资金净卖出13.95亿元
                signals.append({
                    "text": m.group(0),
                    "direction": "卖出" if "卖出" in groups[1] else ("流入" if "流入" in groups[1] else groups[1]),
                    "amount": groups[3] + ("亿" if "亿" in m.group(0) else "万"),
                    "label": groups[0],
                })
            elif len(groups) == 2:  # 净卖出13.95亿元
                signals.append({
                    "text": m.group(0),
                    "direction": "卖出" if "卖出" in groups[0] else ("流入" if "流入" in groups[0] else groups[0]),
                    "amount": groups[1] + ("亿" if "亿" in m.group(0) else "万"),
                    "label": "资金",
                })
            elif len(groups) == 3:  # 融资加仓2股
                signals.append({
                    "text": m.group(0),
                    "direction": "买入" if "加仓" in groups[1] else "卖出",
                    "amount": groups[2],
                    "label": groups[0],
                })
    return signals


def analyze_news_fund_flow(news_list: List[Dict]) -> Dict:
    """分析新闻列表中的资金流向信号"""
    buy_signals = []
    sell_signals = []
    
    for n in news_list:
        combined = n['title'] + " " + n.get('snippet', '')
        signals = extract_fund_flow(combined)
        for s in signals:
            sig_text = f"{s['label']} {s['direction']} {s['amount']}"
            if '买入' in s['direction'] or '流入' in s['direction'] or '加仓' in s['direction']:
                buy_signals.append({"news": n['title'][:40], "signal": sig_text})
            else:
                sell_signals.append({"news": n['title'][:40], "signal": sig_text})
    
    return {
        "buy": buy_signals[:5],
        "sell": sell_signals[:5],
        "total_buy": len(buy_signals),
        "total_sell": len(sell_signals),
    }


# ─── 增强输出格式 ───

def format_news_with_fundflow(news_list: List[Dict], title: str = "最新新闻") -> str:
    """格式化新闻输出（含资金流向分析）"""
    lines = [f"\n📰 {title}"]
    lines.append("=" * 60)
    
    # 资金流向分析
    ff = analyze_news_fund_flow(news_list)
    if ff['buy'] or ff['sell']:
        lines.append(f"\n💰 资金信号（从新闻提取）:")
        for s in ff['buy']:
            lines.append(f"   🟢 {s['signal']}")
        for s in ff['sell']:
            lines.append(f"   🔴 {s['signal']}")
        lines.append("")
    
    # 新闻列表
    for i, n in enumerate(news_list, 1):
        lines.append(f"\n{i}. {n['title']}")
        if n.get('source'):
            lines.append(f"   来源: {n['source']} | {n.get('date', '')}")
        if n.get('snippet'):
            snippet = n['snippet'][:120]
            # 高亮资金信号
            lines.append(f"   {snippet}")
    
    return "\n".join(lines)

def get_trends(keyword: str) -> Optional[Dict]:
    """获取关键词的Google Trends数据"""
    data = _serpapi_request({
        "engine": "google_trends",
        "q": keyword,
    })
    
    if not data or "error" in data:
        return None
    
    timeline = data.get("interest_over_time", {})
    if not timeline:
        return None
    
    # 提取最近趋势
    results = []
    for item in timeline.get("timeline_data", [])[-10:]:
        results.append({
            "date": item.get("date", ""),
            "value": item.get("values", [{}])[0].get("value", 0),
        })
    
    return {
        "keyword": keyword,
        "trend": results,
        "current": int(results[-1]["value"]) if results else 0,
        "peak": max(int(r["value"]) for r in results) if results else 0,
    }


def format_news(news_list: List[Dict], title: str = "最新新闻") -> str:
    """格式化新闻输出"""
    if not news_list:
        return "暂无新闻"
    
    lines = [f"\n📰 {title}"]
    lines.append("=" * 60)
    
    for i, n in enumerate(news_list, 1):
        lines.append(f"\n{i}. {n['title']}")
        if n.get('source'):
            lines.append(f"   来源: {n['source']} | {n.get('date', '')}")
        if n.get('snippet'):
            lines.append(f"   {n['snippet'][:120]}")
    
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("用法:")
        print("  python3 news.py <股票代码|名称>  # 个股新闻")
        print("  python3 news.py sector <板块名>  # 板块新闻")
        print("  python3 news.py market         # 大盘新闻")
        print("  python3 news.py trend <关键词>  # 搜索热度趋势")
        sys.exit(0)
    
    cmd = sys.argv[1]
    
    if cmd == "sector" and len(sys.argv) > 2:
        news = search_sector_news(sys.argv[2])
        print(format_news(news, f"{sys.argv[2]}板块新闻"))
    elif cmd == "market":
        news = search_market_news()
        print(format_news(news, "A股大盘新闻"))
    elif cmd == "trend" and len(sys.argv) > 2:
        t = get_trends(sys.argv[2])
        if t:
            print(f"\n📈 {t['keyword']} 搜索热度")
            print(f"   当前: {t['current']}/100 | 峰值: {t['peak']}/100")
            for r in t['trend'][-5:]:
                bar = "█" * max(1, int(r['value']) // 5)
                print(f"   {r['date']}: {r['value']:>3} {bar}")
    else:
        # 默认：当做股票代码或名称搜索
        news = search_stock_news(cmd, sys.argv[2] if len(sys.argv) > 2 else "")
        print(format_news(news, f"{sys.argv[2] or cmd} 最新新闻"))
