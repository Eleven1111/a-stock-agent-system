#!/usr/bin/env python3
"""
大盘快照脚本 — 抓取三大指数 + 热门龙头股涨跌，反推板块热度
数据源：新浪（指数）+ 新浪（个股），均可直连，不依赖 JS 渲染页面。

Usage:
  python3 market_pulse.py --json          # 盘中快照
  python3 market_pulse.py --close --json  # 收盘快照（含总结）
"""

import json
import re
import os
from datetime import datetime

COMMON = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "common"))
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

from data_provider import provider_client

SINA_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://finance.sina.com.cn/",
}

# 新浪指数
INDEX_URL = "https://hq.sinajs.cn/list=s_sh000001,s_sz399001,s_sz399006"

# 代表性龙头：覆盖半导体、AI、新能源、有色、白酒、医药、汽车等核心赛道
# 每个赛道 2-4 只代表股，新浪代码格式
STOCK_GROUPS = {
    "🔬 半导体/芯片": ["sh688981", "sz002371", "sh688256", "sz300223"],
    "🤖 AI/算力": ["sz300502", "sz300394", "sh603019"],
    "🔋 新能源/锂电": ["sz300750", "sz002460", "sh600438"],
    "⛏ 有色/资源": ["sh601899", "sh603993", "sh600489"],
    "🚗 汽车/零部件": ["sz000625", "sh600104", "sz002594"],
    "🍶 白酒/消费": ["sh600519", "sz000858", "sz002304"],
    "💊 医药": ["sh600276", "sz300760", "sh688180"],
    "🏦 金融": ["sh601318", "sh600036", "sh600030"],
}


def fetch_raw_sina(codes: str) -> list[str]:
    """拉原始行情"""
    url = f"https://hq.sinajs.cn/list={codes}"
    result = provider_client("sina").request_text(
        url,
        encoding="gbk",
        headers=SINA_HEADERS,
    )
    return result.data.strip().split("\n")


def fetch_indices() -> dict:
    """抓取三大指数"""
    try:
        result = {}
        code_map = {"sh000001": "000001", "sz399001": "399001", "sz399006": "399006"}
        for line in fetch_raw_sina("s_sh000001,s_sz399001,s_sz399006"):
            m = re.search(r'var hq_str_s_(\w+)="([^"]*)"', line)
            if not m:
                continue
            fields = m.group(2).split(",")
            if len(fields) < 4:
                continue
            code = code_map.get(m.group(1), m.group(1))
            result[code] = {
                "name": fields[0],
                "price": float(fields[1]) if fields[1] else 0,
                "change": float(fields[2]) if fields[2] else 0,
                "change_pct": float(fields[3]) if fields[3] else 0,
            }
        return result
    except Exception as e:
        return {"_error": str(e)}


def fetch_group_snapshot() -> list[dict]:
    """拉龙头股并反推板块热度"""
    all_codes = []
    code_to_group = {}
    for group, codes in STOCK_GROUPS.items():
        for c in codes:
            all_codes.append(c)
            code_to_group[c] = group

    try:
        raw = fetch_raw_sina(",".join(all_codes))
    except Exception:
        return []

    stocks = []
    for line in raw:
        m = re.search(r'var hq_str_(\w+)="([^"]*)"', line)
        if not m:
            continue
        fields = m.group(2).split(",")
        if len(fields) < 32:
            continue
        code = m.group(1)
        name = fields[0]
        price = float(fields[3]) if fields[3] else 0    # field[3] = 当前价
        prev = float(fields[2]) if fields[2] else 0     # field[2] = 昨收
        pct = round((price - prev) / prev * 100, 2) if prev else 0
        group = code_to_group.get(code, "")

        stocks.append({"code": code, "name": name, "price": price, "pct": pct, "group": group})

    # 按板块汇总
    group_data = {}
    for s in stocks:
        g = s["group"]
        if g not in group_data:
            group_data[g] = {"stocks": [], "avg_pct": 0}
        group_data[g]["stocks"].append(s)

    for g, d in group_data.items():
        pcts = [s["pct"] for s in d["stocks"]]
        d["avg_pct"] = round(sum(pcts) / len(pcts), 2)
        d["direction"] = "🔥" if d["avg_pct"] >= 1 else ("🟢" if d["avg_pct"] > 0 else "🔴")

    # 按平均涨幅排序
    sorted_groups = sorted(group_data.items(), key=lambda x: x[1]["avg_pct"], reverse=True)

    return sorted_groups


def format_report(indices: dict, groups: list, is_close: bool = False) -> str:
    now = datetime.now()
    label = "收盘总结" if is_close else f"盘中 {now.strftime('%H:%M')}"
    lines = [f"📊 A股大盘快照 | {label}", ""]

    # 三大指数
    lines.append("━━ 指数 ━━")
    for code, info in indices.items():
        if isinstance(info, dict):
            name = info.get("name", code)
            price = info.get("price", 0)
            pct = info.get("change_pct", 0)
            sign = "+" if pct >= 0 else ""
            lines.append(f"  {name}: {price:.2f} ({sign}{pct:.2f}%)")

    # 板块热度
    if groups:
        lines.append("")
        lines.append("━━ 板块热度（龙头加权）━━")
        for group_name, data in groups[:6]:
            icon = data["direction"]
            stocks_str = "  ".join(
                f"{s['name']} {s['pct']:+.1f}%" for s in data["stocks"]
            )
            lines.append(f"  {icon} {group_name} 均{data['avg_pct']:+.1f}%")
            lines.append(f"     {stocks_str}")

    # 收盘总结
    if is_close:
        lines.append("")
        up_count = sum(1 for info in indices.values() if isinstance(info, dict) and info.get("change_pct", 0) > 0)
        if up_count >= 2:
            style = "偏强，成长领涨"
        elif up_count == 1:
            style = "分化"
        else:
            style = "偏弱"
        # 找最热板块
        if groups:
            hottest = groups[0]
            style += f"，{hottest[0]}最热"
        lines.append(f"📝 今日风格: {style}")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true")
    p.add_argument("--close", action="store_true")
    args = p.parse_args()

    indices = fetch_indices()
    groups = fetch_group_snapshot()

    if args.json:
        out = {
            "timestamp": datetime.now().isoformat(),
            "indices": indices,
            "groups": {g: {"avg_pct": d["avg_pct"], "stocks": d["stocks"]} for g, d in groups},
            "is_close": args.close,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        report = format_report(indices, groups, is_close=args.close)
        print(report)
