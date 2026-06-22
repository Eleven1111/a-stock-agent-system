"""
股票新闻模块
数据源：serper.dev Google News（需要 SERPER_API_KEY 环境变量）
"""

import os
import re
import sys
from typing import Optional, List, Dict

_COMMON_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "common")
if _COMMON_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(_COMMON_DIR))
from paths import env_file
from data_provider import fetch_serper_news as _fetch_serper_news
from http_client import DataSourceError

# serper.dev key
SERPER_KEY = os.environ.get("SERPER_API_KEY") or ""
if not SERPER_KEY:
    try:
        with open(env_file()) as f:
            for line in f:
                if line.startswith("SERPER_API_KEY="):
                    SERPER_KEY = line.split("=", 1)[1].strip().strip("'").strip('"')
                    break
    except Exception:
        pass

# 自动加载 NO_PROXY，绕过 Clash 代理的 DNS 劫持
if not os.environ.get("NO_PROXY"):
    try:
        with open(env_file()) as f:
            for line in f:
                if line.startswith("NO_PROXY="):
                    os.environ["NO_PROXY"] = line.split("=", 1)[1].strip().strip("'").strip('"')
                    break
    except Exception:
        pass


def _serper_request(params: dict) -> Optional[Dict]:
    """通用 serper.dev 请求"""
    if not SERPER_KEY:
        return {"error": "SERPER_API_KEY 未配置"}
    try:
        limit = int(params.get("num", 10))
        result = _fetch_serper_news(str(params.get("q", "")), SERPER_KEY, limit)
        return {
            "news_results": [
                {
                    "title": item.get("title", ""),
                    "snippet": item.get("snippet", ""),
                    "source": {"name": item.get("source", "")},
                    "date": item.get("date", ""),
                    "link": item.get("link"),
                }
                for item in result.data
            ]
        }
    except (DataSourceError, AttributeError, TypeError, ValueError) as e:
        return {"error": str(e)}


def search_stock_news(code: str = "", name: str = "", max_results=8) -> List[Dict]:
    """搜索个股新闻"""
    query = f"{name} {code} A股 2026" if name else f"{code} A股"
    data = _serper_request({"q": query, "num": max_results})
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
    data = _serper_request({"q": query, "num": max_results})
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
    data = _serper_request({"q": "A股 行情 热点 2026", "num": max_results})
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
            if len(groups) == 4:
                signals.append({
                    "text": m.group(0),
                    "direction": "卖出" if "卖出" in groups[1] else ("流入" if "流入" in groups[1] else groups[1]),
                    "amount": groups[3] + ("亿" if "亿" in m.group(0) else "万"),
                    "label": groups[0],
                })
            elif len(groups) == 2:
                signals.append({
                    "text": m.group(0),
                    "direction": "卖出" if "卖出" in groups[0] else ("流入" if "流入" in groups[0] else groups[0]),
                    "amount": groups[1] + ("亿" if "亿" in m.group(0) else "万"),
                    "label": "资金",
                })
            elif len(groups) == 3:
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


def format_news_with_fundflow(news_list: List[Dict], title: str = "最新新闻") -> str:
    """格式化新闻输出（含资金流向分析）"""
    lines = [f"\n📰 {title}"]
    lines.append("=" * 60)
    ff = analyze_news_fund_flow(news_list)
    if ff['buy'] or ff['sell']:
        lines.append("\n💰 资金信号（从新闻提取）:")
        for s in ff['buy']:
            lines.append(f"   🟢 {s['signal']}")
        for s in ff['sell']:
            lines.append(f"   🔴 {s['signal']}")
        lines.append("")
    for i, n in enumerate(news_list, 1):
        lines.append(f"\n{i}. {n['title']}")
        if n.get('source'):
            lines.append(f"   来源: {n['source']} | {n.get('date', '')}")
        if n.get('snippet'):
            lines.append(f"   {n['snippet'][:120]}")
    return "\n".join(lines)


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
    if len(sys.argv) < 2:
        print("用法:")
        print("  python3 news.py <股票代码|名称>  # 个股新闻")
        print("  python3 news.py sector <板块名>  # 板块新闻")
        print("  python3 news.py market         # 大盘新闻")
        sys.exit(0)
    cmd = sys.argv[1]
    if cmd == "sector" and len(sys.argv) > 2:
        news = search_sector_news(sys.argv[2])
        print(format_news(news, f"{sys.argv[2]}板块新闻"))
    elif cmd == "market":
        news = search_market_news()
        print(format_news(news, "A股大盘新闻"))
    else:
        news = search_stock_news(cmd, sys.argv[2] if len(sys.argv) > 2 else "")
        print(format_news(news, f"{sys.argv[2] or cmd} 最新新闻"))
