"""
新闻解析器 — 从资讯文本中提取关键要素
"""

import re


# 商品关键词列表（用于从新闻中匹配）
COMMODITY_PATTERNS = [
    # 黑色系
    (r"焦煤", "焦煤"),
    (r"焦炭", "焦煤"),
    (r"螺纹钢|热卷|线材|钢筋", "螺纹钢"),
    (r"铁矿石", "铁矿石"),
    # 能源
    (r"原油|WTI|布伦特|国际油价|石油", "原油"),
    (r"动力煤|电煤|郑煤", "动力煤"),
    # 有色金属
    (r"沪铜|电解铜|国际铜|伦敦铜", "铜"),
    (r"沪铝|电解铝|伦敦铝", "铝"),
    (r"黄金|金价|沪金|COMEX黄金|国际金价", "黄金"),
    (r"碳酸锂|锂价|锂盐|氢氧化锂", "碳酸锂"),
    # 农产品
    (r"豆粕|大豆|豆油", "豆粕"),
    (r"生猪|猪肉|猪价", "生猪"),
    (r"玉米", "玉米"),
    (r"白糖|原糖", "白糖"),
    # 化工
    (r"纯碱", "纯碱"),
    (r"玻璃", "纯碱"),
    (r"甲醇|乙二醇|PTA", "化工品"),
    # 航运
    (r"航运|海运费|波罗的海|BDI|集装箱|海运", "航运"),
]

# 涨幅/跌幅匹配
CHANGE_PATTERN = re.compile(r"((?:涨|跌)(?:幅|停)?[\s]*?)(\d+[\.\d]*)\s*%")
LIMIT_PATTERN = re.compile(r"(触及|封住)?(涨|跌)停")
EVENT_PATTERN = re.compile(r"(大涨|暴跌|飙升|跳水|暴涨|重挫|拉升|回调|强势|走弱|反弹|破位)")


def parse_news(text):
    """
    解析新闻文本，返回结构化信息。

    返回:
    {
        "commodities": [{"name": "焦煤", "matched": "焦煤期货"}],
        "direction": "bullish",  # bullish/bearish/neutral
        "magnitude": 8.0,        # 百分比数值
        "event_type": "涨停",     # 涨停/暴涨/政策利好等
        "details": [匹配到的各条信息]
    }
    """
    result = {
        "commodities": [],
        "direction": "neutral",
        "magnitude": 0.0,
        "event_type": None,
        "details": [],
    }

    # 匹配商品
    matched_commodities = set()
    for pattern, com_name in COMMODITY_PATTERNS:
        match = re.search(pattern, text)
        if match and com_name not in matched_commodities:
            matched_commodities.add(com_name)
            result["commodities"].append({
                "name": com_name,
                "matched": match.group(0),
            })
            result["details"].append(f"识别到商品: {com_name} (匹配: {match.group(0)})")

    # 匹配涨跌停
    limit_match = LIMIT_PATTERN.search(text)
    if limit_match:
        direction_text = limit_match.group(2)
        result["event_type"] = f"{direction_text}停"
        result["direction"] = "bullish" if "涨" in direction_text else "bearish"
        result["magnitude"] = 0.0  # 涨停/跌停通常无精确百分比
        result["details"].append(f"识别到{direction_text}停事件")

    # 匹配百分比涨跌
    change_match = CHANGE_PATTERN.search(text)
    if change_match:
        direction_text = change_match.group(1)
        magnitude = float(change_match.group(2))
        is_up = "涨" in direction_text
        result["direction"] = "bullish" if is_up else "bearish"
        result["magnitude"] = max(result["magnitude"], magnitude)
        result["event_type"] = result["event_type"] or f"{'涨' if is_up else '跌'}幅{magnitude}%"
        result["details"].append(f"识别到{'涨' if is_up else '跌'}幅: {magnitude}%")

    # 匹配事件关键词
    event_match = EVENT_PATTERN.search(text)
    if event_match and not result["event_type"]:
        event_word = event_match.group(1)
        is_up = event_word in ["大涨", "暴涨", "飙升", "拉升", "强势", "反弹"]
        result["direction"] = "bullish" if is_up else "bearish"
        result["event_type"] = event_word
        result["details"].append(f"识别到事件类型: {event_word}")

    return result
