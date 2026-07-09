#!/usr/bin/env python3
"""
资讯驱动A股板块分析 — 主入口 v2

用法:
    python3 main.py "焦煤期货主力合约触及涨停，涨幅8%，报1387.5元/吨"
"""

import os
import sys
import argparse
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
COMMON_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "common"))
if COMMON_DIR not in sys.path:
    sys.path.insert(0, COMMON_DIR)

from market_adapters import fetch_board_quotes
from news_parser import parse_news
from industry_chain import find_matching_chains


# ====== 板块实时数据 ======

def _dfcf_req(params, retries=2):
    """Compatibility facade now backed by the resilient board quote chain."""
    return {"data": {"diff": fetch_board_quotes()}}


def _fetch_board_map():
    """获取全量行业板块名称→涨跌幅映射"""
    data = _dfcf_req({
        "pn": 1, "pz": 500, "po": 1, "np": 1, "fid": "f3",
        "fs": "m:90+t:2", "fields": "f12,f14,f3,f104,f105"
    })
    boards = data.get("data", {}).get("diff", [])
    return {b["f14"]: b for b in boards if b.get("f14")}


def match_board(plate_name, board_map):
    """根据板块名模糊匹配东方财富中的板块"""
    # 精确匹配
    if plate_name in board_map:
        return board_map[plate_name]
    # 模糊匹配：板块名包含plate_name
    for name, data in board_map.items():
        if plate_name in name:
            return data
    return None


# ====== 板块名修正表（产业链逻辑名 → 东财板块名）======
BOARD_ALIAS = {
    "焦化": "焦炭Ⅲ",           # 东财没有"焦化"，叫"焦炭Ⅲ"
    "煤化工": "煤化工",         # 保留原名
    "建筑材料": "建筑材料",     # 保留原名
    "石油开采": "石油开采",     # 保留原名
    "石油化工": "石油化工",     # 保留原名
    "油气设服": "油气设服",     # 保留原名
    "航空运输": "航空运输",     # 保留原名
    "交通运输": "交通运输",     # 保留原名
    "有色金属": "有色金属",     # 保留原名
    "工业金属": "工业金属",     # 保留原名
    "电网设备": "电网设备",     # 保留原名
    "黄金": "黄金",             # 保留原名
    "能源金属": "能源金属",     # 保留原名
    "小金属": "小金属",         # 保留原名
    "火电": "火电",             # 保留原名
    "电解铝": "电解铝",         # 保留原名
    "电力": "电力",             # 保留原名
    "养殖业": "养殖业",         # 保留原名
    "饲料": "饲料",             # 保留原名
    "种植业": "种植业",         # 保留原名
    "航运": "航运",             # 保留原名
    "物流": "物流",             # 保留原名
    "港口": "港口",             # 保留原名
    "玻璃": "玻璃",             # 保留原名
    "化工": "化工",             # 保留原名
    "房地产开发": "房地产开发",  # 保留原名
    "汽车": "汽车",             # 保留原名
    "家电": "家电",             # 保留原名
    "机械": "机械",             # 保留原名
    "电池": "电池",             # 保留原名
    "新能源": "新能源",         # 保留原名
    "新能源车": "新能源车",     # 保留原名
    "珠宝首饰": "珠宝首饰",     # 保留原名
    "食品加工": "食品加工",     # 保留原名
    "肉制品": "肉制品",         # 保留原名
    "外贸": "外贸",             # 保留原名
    "跨境电商": "跨境电商",     # 保留原名
    "光伏": "光伏",             # 保留原名
}


def resolve_board(plate_name):
    """产业链中的板块名 → 东财可查询的板块名"""
    return BOARD_ALIAS.get(plate_name, plate_name)


# ====== 预期差分析 ======

def analyze_divergence(bullish, bearish, direction, board_map):
    """
    分析逻辑方向和实际盘面的偏差。
    如果利好板块实际在跌、或利空板块实际在涨，标注为"预期差"。
    """
    notes = []
    for item in bullish:
        bname = resolve_board(item["sector"])
        bd = match_board(bname, board_map)
        if bd and bd.get("f3") is not None and bd["f3"] < -1:
            notes.append(f"⚠️ 预期差：{item['sector']} 逻辑上应利好，但今日实际跌幅 {bd['f3']:.1f}%，说明利好尚未兑现或被其他利空因素抵消")

    for item in bearish:
        bname = resolve_board(item["sector"])
        bd = match_board(bname, board_map)
        if bd and bd.get("f3") is not None and bd["f3"] > 1:
            notes.append(f"⚠️ 预期差：{item['sector']} 逻辑上应利空，但今日实际涨幅 {bd['f3']:.1f}%，说明利空被市场消化或有其他利好对冲")

    return notes


# ====== 主分析函数 ======

def analyze_news(news_text):
    output = []
    parsed = parse_news(news_text)

    output.append(f"📰 **资讯：** {news_text.strip()}")
    output.append("")

    if not parsed["commodities"]:
        output.append("⚠️ 未能从资讯中识别到已知商品。")
        output.append("当前支持：焦煤、螺纹钢、原油、铜、黄金、碳酸锂、动力煤、纯碱、豆粕、生猪、航运等。")
        return "\n".join(output)

    # 获取实时板块全景图（一次请求，后续复用）
    board_map = _fetch_board_map()

    com_names = [c["name"] for c in parsed["commodities"]]
    direction = parsed["direction"]
    magnitude = parsed["magnitude"]

    output.append(f"🔍 **关键商品：** {'、'.join(com_names)}")
    output.append(f"📌 **事件方向：** {'📈 上涨' if direction == 'bullish' else '📉 下跌'}")
    if magnitude > 0:
        output.append(f"📊 **涨跌幅度：** {magnitude}%")
    if parsed["event_type"]:
        output.append(f"🏷️  **事件类型：** {parsed['event_type']}")
    output.append("")

    # 产业链传导
    matched = find_matching_chains(com_names)
    if not matched:
        output.append("⚠️ 未能找到该商品的产业链映射。")
        return "\n".join(output)

    bullish_sectors = []
    bearish_sectors = []

    for m in matched:
        chains = m["chain_up"] if direction == "bullish" else m["chain_down"]
        for item in chains:
            entry = {
                "sector": item["sector"],
                "direction": item["direction"],
                "strength": item["strength"],
                "reasoning": item["reasoning"],
                "lag": item["lag"],
                "commodity": m["commodity"],
            }
            if item["direction"] == "bullish":
                existing = next((x for x in bullish_sectors if x["sector"] == item["sector"]), None)
                if existing:
                    existing["strength"] = max(existing["strength"], item["strength"])
                else:
                    bullish_sectors.append(entry)
            else:
                existing = next((x for x in bearish_sectors if x["sector"] == item["sector"]), None)
                if existing:
                    existing["strength"] = max(existing["strength"], item["strength"])
                else:
                    bearish_sectors.append(entry)

    bullish_sectors.sort(key=lambda x: x["strength"], reverse=True)
    bearish_sectors.sort(key=lambda x: x["strength"], reverse=True)

    lag_map = {"immediate": "即时", "short": "短期", "medium": "中期"}
    star_map = {5: "★★★★★", 4: "★★★★☆", 3: "★★★☆☆", 2: "★★☆☆☆", 1: "★☆☆☆☆"}

    # 利好板块
    output.append("━━━ 🔺 **利好板块** ━━━")
    for s in bullish_sectors:
        bname = resolve_board(s["sector"])
        bd = match_board(bname, board_map)
        rt = ""
        if bd and bd.get("f3") is not None:
            cp = bd["f3"]
            sign = "+" if cp >= 0 else ""
            rt = f"│ 今日{sign}{cp:.2f}%"
        output.append(f"  **{s['sector']}** {star_map.get(s['strength'], '')}")
        output.append(f"  ├ {lag_map.get(s['lag'], s['lag'])}传导")
        output.append(f"  └ {s['reasoning']} {rt}")

    output.append("")
    output.append("━━━ 🔻 **利空板块** ━━━")
    for s in bearish_sectors:
        bname = resolve_board(s["sector"])
        bd = match_board(bname, board_map)
        rt = ""
        if bd and bd.get("f3") is not None:
            cp = bd["f3"]
            sign = "+" if cp >= 0 else ""
            rt = f"│ 今日{sign}{cp:.2f}%"
        output.append(f"  **{s['sector']}** {star_map.get(s['strength'], '')}")
        output.append(f"  ├ {lag_map.get(s['lag'], s['lag'])}传导")
        output.append(f"  └ {s['reasoning']} {rt}")

    # 预期差分析
    divergence_notes = analyze_divergence(bullish_sectors, bearish_sectors, direction, board_map)
    if divergence_notes:
        output.append("")
        output.append("━━━ ⚡ **预期差分析** ━━━")
        for note in divergence_notes:
            output.append(f"  {note}")

    # 当日市场全景（涨幅前5 / 跌幅前5）
    output.append("")
    output.append("━━━ 📊 **当日板块全景** ━━━")

    sorted_boards = sorted(board_map.values(), key=lambda x: x.get("f3") or 0, reverse=True)
    top5 = sorted_boards[:5]
    bot5 = sorted_boards[-5:] if len(sorted_boards) >= 5 else sorted_boards

    output.append("  🔥 涨幅 TOP 5：")
    for b in top5:
        cp = b.get("f3", 0)
        output.append(f"    {b['f14']:12s}  {'+' if cp>=0 else ''}{cp:.2f}%")
    output.append("  🧊 跌幅 TOP 5：")
    for b in reversed(bot5):
        cp = b.get("f3", 0)
        output.append(f"    {b['f14']:12s}  {'+' if cp>=0 else ''}{cp:.2f}%")

    # 尾部
    output.append("")
    output.append("—" * 44)
    output.append("⚠️ 以上基于产业链传导规律分析，实际市场受多重因素影响，仅供参考。")
    output.append(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")

    return "\n".join(output)


# ====== 入口 ======

def main():
    parser = argparse.ArgumentParser(description="资讯驱动A股板块分析 v2")
    parser.add_argument("news", nargs="?", help="资讯文本")
    args = parser.parse_args()

    if not args.news:
        print("=" * 50)
        print("  News-to-Sector — 资讯驱动A股板块分析")
        print("=" * 50)
        news_text = input("\n请输入资讯文本: ").strip()
        if not news_text:
            print("未输入资讯，退出。")
            return
    else:
        news_text = args.news

    result = analyze_news(news_text)
    print("\n" + result)


if __name__ == "__main__":
    main()
