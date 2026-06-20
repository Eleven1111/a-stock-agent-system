"""龙虎榜席位源 — 纯函数单测（席位分类 / 游资识别 / 净买汇总，不触网）。

真实数据校准：探针对 000010(2026-06-17) 拉到买卖各 5 席位，字段含「交易营业部名称」
「净额」，"机构专用"标识机构席位 —— 与本模块分类口径一致。
"""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "hot-money-tactics" / "scripts" / "dragon_tiger.py"
SPEC = importlib.util.spec_from_file_location("dragon_tiger", SCRIPT)
dt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dt)


def test_classify_seat():
    assert dt.classify_seat("机构专用") == "institution"
    assert dt.classify_seat("深股通专用") == "northbound"
    assert dt.classify_seat("沪股通专用") == "northbound"
    assert dt.classify_seat("中信建投证券股份有限公司上海营口路证券营业部") == "hot_money"


def test_is_famous_seat():
    assert dt.is_famous_seat("华鑫证券股份有限公司宁波解放南路证券营业部")
    assert dt.is_famous_seat("西藏东方财富证券拉萨东环路第二证券营业部")
    assert not dt.is_famous_seat("中信建投证券上海营口路")   # 普通营业部不在名单
    assert not dt.is_famous_seat("机构专用")


def test_summarize_seats_aggregates_and_dedups():
    seats = [
        {"交易营业部名称": "华鑫证券宁波解放南路", "净额": 5_000_000},   # 知名游资
        {"交易营业部名称": "中信建投上海营口路", "净额": 2_000_000},     # 普通游资
        {"交易营业部名称": "机构专用", "净额": -3_000_000},             # 机构卖
        {"交易营业部名称": "深股通专用", "净额": 1_000_000},            # 北向买
        {"交易营业部名称": "华鑫证券宁波解放南路", "净额": 9_999},      # 重复 → 去重忽略
    ]
    s = dt.summarize_seats(seats)
    assert s["hot_money_net"] == 7_000_000        # 5M + 2M，重复不计
    assert s["institution_net"] == -3_000_000
    assert s["northbound_net"] == 1_000_000
    assert s["famous_seats"] == ["华鑫证券宁波解放南路"]
    assert s["seat_count"] == 4


def test_net_fallback_buy_minus_sell():
    seats = [{"交易营业部名称": "X营业部", "买入金额": 3_000_000, "卖出金额": 1_000_000}]
    assert dt.summarize_seats(seats)["hot_money_net"] == 2_000_000


def test_seat_signal_hot_money_led():
    s = dt.seat_signal(dt.summarize_seats([
        {"交易营业部名称": "华鑫证券宁波解放南路", "净额": 5_000_000},
        {"交易营业部名称": "机构专用", "净额": -1_000_000},
    ]))
    assert s["dominant_force"] == "hot_money"
    assert s["hot_money_led"] is True
    assert s["has_famous_hot_money"] is True
    assert s["total_net"] == 4_000_000


def test_seat_signal_institution_dominant():
    s = dt.seat_signal(dt.summarize_seats([
        {"交易营业部名称": "某营业部", "净额": 1_000_000},
        {"交易营业部名称": "机构专用", "净额": 8_000_000},
    ]))
    assert s["dominant_force"] == "institution"
    assert s["hot_money_led"] is False
    assert s["has_famous_hot_money"] is False


def test_seat_signal_empty():
    s = dt.seat_signal(dt.summarize_seats([]))
    assert s["dominant_force"] == "none"
    assert s["hot_money_led"] is False
    assert s["total_net"] == 0.0
