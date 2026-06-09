#!/usr/bin/env python3
"""
港A联动监控 — AH溢价/港股异动/南北向资金情绪
============================================
监控 AH 两地上市股票的溢价率、港股通资金流向、
恒生指数与上证综指的联动背离。

数据源：腾讯 qt.gtimg.cn（A股+港股实时行情）
Usage:
  python3 hk_a_linkage.py
  python3 hk_a_linkage.py --json
"""

import json
import urllib.request
from datetime import datetime
from typing import Dict, Any

# ========== AH配对股 ==========
# 选市值最大、流动性最好的 AH 股
AH_PAIRS = [
    ("600036", "招商银行", "hk03968"),
    ("601318", "中国平安", "hk02318"),
    ("600519", "贵州茅台", None),    # 无H股，仅做A股锚
    ("000858", "五粮液", None),
    ("600585", "海螺水泥", "hk00914"),
    ("601899", "紫金矿业", "hk02899"),
    ("600011", "华能国际", "hk00902"),
    ("002156", "通富微电", None),
    ("600584", "长电科技", None),
    ("002185", "华天科技", None),
    ("000021", "深科技", None),
    ("600667", "太极实业", None),
]

# 港股通权重股
HK_STOCKS = [
    ("hk00700", "腾讯控股"),
    ("hk09988", "阿里巴巴-SW"),
    ("hk03690", "美团-W"),
    ("hk01810", "小米集团-W"),
    ("hk09999", "网易-S"),
    ("hk02318", "中国平安"),
    ("hk00941", "中国移动"),
]

# 港股指数的A股对标
HK_SECTORS = {
    "hk00700": ["AI算力", "互联网"],
    "hk09988": ["互联网", "电商"],
    "hk01810": ["消费电子", "新能源车"],
    "hk02318": ["券商金融"],
    "hk00941": ["通信", "电信运营"],
}


def fetch_tencent_realtime(code: str, market: str = "sz") -> Dict:
    """腾讯实时行情"""
    url = f"http://qt.gtimg.cn/q={market}{code.lstrip('hk') if market == 'sz' else code}"
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
            "change_pct": float(parts[32]) if parts[32] else None,
            "high": float(parts[33]) if parts[33] else None,
            "low": float(parts[34]) if parts[34] else None,
            "amount": float(parts[37]) * 10000 if parts[37] else None,
            "turnover": float(parts[38]) if parts[38] else None,
        }
    except Exception as e:
        return {"error": str(e)}


def fetch_tencent_hk(code_hk: str) -> Dict:
    """港股实时行情（腾讯格式）"""
    code = code_hk.replace("hk", "")
    url = f"http://qt.gtimg.cn/q=hk{code}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("gbk")
        parts = raw.split("=")[1].strip().strip('"').split("~")
        if len(parts) < 30:
            return {"error": "HK数据不完整"}
        return {
            "price": float(parts[3]) if parts[3] else None,
            "prev_close": float(parts[4]) if parts[4] else None,
            "change_pct": float(parts[32]) if parts[32] else None,
            "amount": float(parts[37]) * 10000 if parts[37] else None,
            "pe": float(parts[39]) if parts[39] else None,
            "market_cap": float(parts[44]) if parts[44] else None,
        }
    except Exception as e:
        return {"error": str(e)}


def calc_ah_premium(a_price: float, h_price: float, fx_rate: float = 0.91) -> float:
    """计算 AH 溢价率"""
    if not a_price or not h_price:
        return None
    return (a_price / (h_price * fx_rate) - 1) * 100


def collect_hk_a_data() -> Dict[str, Any]:
    """采集港A联动数据"""
    result = {
        "timestamp": datetime.now().isoformat(),
        "ah_pairs": [],
        "hk_stocks": [],
        "index_divergence": {},
        "alerts": [],
        "summary": "",
    }

    # 1. 获取指数（yfinance 或腾讯降级）
    import yfinance as yf
    try:
        hsi_tk = yf.Ticker("^HSI")
        hsi_info = hsi_tk.info
        hsi_price = hsi_info.get("regularMarketPrice") or hsi_info.get("previousClose")
        hsi_pct = hsi_info.get("regularMarketChangePercent")
    except Exception:
        hk_raw = fetch_tencent_hk("hkHSI")
        hsi_price = hk_raw.get("price")
        hsi_pct = hk_raw.get("change_pct")

    sse = fetch_tencent_realtime("000001", "sh")

    result["indices"] = {
        "sse": {"name": "上证综指", "price": sse.get("price"), "change_pct": sse.get("change_pct")},
        "hsi": {"name": "恒生指数", "price": hsi_price, "change_pct": hsi_pct},
    }

    sse_pct = sse.get("change_pct")
    if sse_pct is not None and hsi_pct is not None:
        spread = sse_pct - hsi_pct
        result["index_divergence"] = {
            "spread": round(spread, 2),
            "sse_pct": sse_pct,
            "hsi_pct": hsi_pct,
        }
        if abs(spread) >= 1.5:
            direction = "上证相对强势" if spread > 0 else "港股相对强势"
            result["alerts"].append({
                "level": "🟡" if abs(spread) >= 2.0 else "ℹ️",
                "msg": f"AH指数背离{spread:.1f}%——{direction}，关注资金偏好切换",
            })

    # 2. AH配对股溢价率
    for a_code, a_name, hk_code in AH_PAIRS:
        if hk_code is None:
            continue
        a_data = fetch_tencent_realtime(a_code, "sh" if a_code.startswith("6") else "sz")
        hk_data = fetch_tencent_hk(hk_code)

        if a_data.get("price") and hk_data.get("price"):
            premium = calc_ah_premium(a_data["price"], hk_data["price"])
            if premium is not None:
                pair = {
                    "a_code": a_code,
                    "a_name": a_name,
                    "a_price": a_data["price"],
                    "a_change": a_data.get("change_pct"),
                    "hk_code": hk_code,
                    "hk_price": hk_data["price"],
                    "hk_change": hk_data.get("change_pct"),
                    "premium": round(premium, 1),
                }
                result["ah_pairs"].append(pair)

                # 溢价异常检测
                if premium > 50:
                    result["alerts"].append({
                        "level": "ℹ️",
                        "msg": f"{a_name} AH溢价{premium:.0f}%，A股显著高估",
                    })

    # 3. 港股通权重股
    for hk_code, hk_name in HK_STOCKS:
        data = fetch_tencent_hk(hk_code)
        if data.get("price"):
            item = {
                "code": hk_code,
                "name": hk_name,
                "price": data["price"],
                "change_pct": data.get("change_pct"),
                "a_impact": HK_SECTORS.get(hk_code, []),
            }
            result["hk_stocks"].append(item)

            # 关键个股异动
            pct = data.get("change_pct")
            if pct and abs(pct) >= 5:
                direction = "涨" if pct > 0 else "跌"
                impact = HK_SECTORS.get(hk_code, [])
                result["alerts"].append({
                    "level": "🟡" if abs(pct) >= 8 else "ℹ️",
                    "msg": f"港股{hk_name}{direction}{abs(pct):.1f}%，可能联动A股{', '.join(impact)}" if impact else f"港股{hk_name}{direction}{abs(pct):.1f}%",
                })

    # 4. 汇总
    top_ah = sorted(result["ah_pairs"], key=lambda x: abs(x["premium"]), reverse=True)[:5]
    if top_ah:
        ah_lines = [f"{p['a_name']}: A={p['a_price']} H={p['hk_price']} 溢价{p['premium']}%" for p in top_ah]
        result["summary"] = "AH溢价TOP5: " + " | ".join(ah_lines)
    else:
        result["summary"] = "AH联动数据采集完成"

    return result


def format_report(data: Dict[str, Any]) -> str:
    """格式化报告"""
    lines = [
        "🇭🇰🤝🇨🇳 **港A联动监控**",
        f"⏰ {data['timestamp']}",
        "",
    ]

    # 指数
    idx = data.get("indices", {})
    sse = idx.get("sse", {})
    hsi = idx.get("hsi", {})
    lines.append("## 📊 指数对比")
    lines.append(f"上证: {sse.get('price', 'N/A')} ({sse.get('change_pct', 'N/A')}%) "
                 f"| 恒生: {hsi.get('price', 'N/A')} ({hsi.get('change_pct', 'N/A')}%)")

    div = data.get("index_divergence", {})
    if div:
        lines.append(f"AH指数差: {div.get('spread', 'N/A')}%")

    # AH溢价
    pairs = data.get("ah_pairs", [])
    if pairs:
        lines.append("\n## 💰 AH溢价率")
        for p in sorted(pairs, key=lambda x: abs(x["premium"]), reverse=True)[:8]:
            lines.append(f"- {p['a_name']}({p['a_code']}): "
                         f"A {p['a_price']}({p.get('a_change', 'N/A')}%) "
                         f"H {p['hk_price']}({p.get('hk_change', 'N/A')}%) "
                         f"溢价 **{p['premium']}%**")

    # 港股权重
    hk = data.get("hk_stocks", [])
    if hk:
        lines.append("\n## 🏢 港股权重异动")
        notable = [s for s in hk if s.get("change_pct") and abs(s["change_pct"]) >= 2]
        if notable:
            for s in notable:
                emoji = "🟢" if s["change_pct"] > 0 else "🔴"
                lines.append(f"- {emoji} {s['name']}: {s['change_pct']:+.1f}%")
        else:
            lines.append("港股权重无显著异动")

    # 警报
    alerts = data.get("alerts", [])
    if alerts:
        lines.append("\n## ⚡ 联动信号")
        for a in alerts:
            lines.append(f"- {a['level']} {a['msg']}")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="港A联动监控")
    parser.add_argument("--json", action="store_true", help="JSON输出")
    args = parser.parse_args()

    data = collect_hk_a_data()

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_report(data))
