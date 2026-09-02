"""板块动量阈值的单一事实源 —— 行为断言，不是字段断言。

两条纪律在这里交汇：

1. **同值常量留两份拷贝必须有相等断言。** `sector_momentum.DEFAULTS` 是为了配置
   缺失时不把 weakening / rotating_out 两个减分项一起抽掉而保留的副本；没有相等
   断言，只改一边的后果极隐蔽。
2. **断言配置字段的值 ≠ 消费它的代码真的读了它。** 所以每个可配项都要用一份
   「和默认值不同」的配置跑一遍，看行为是否真的跟着变 —— 只对 YAML 取值做断言的
   守卫，能让一条从未被读取的死配置活很多个月。
"""

from __future__ import annotations

import copy

import yaml

import sector_momentum as sm
import signal_context as sc
from config_registry import config_path


def _yaml_section():
    with open(config_path("scoring"), encoding="utf-8") as handle:
        return (yaml.safe_load(handle) or {}).get(sm.CONFIG_SECTION)


def test_yaml_section_and_module_defaults_do_not_drift():
    assert _yaml_section() == sm.DEFAULTS


def test_missing_section_keeps_every_default_including_the_deductions():
    resolved = sm.load_config({})
    assert resolved == sm.DEFAULTS
    assert resolved["boost"]["weakening"] < 0
    assert resolved["boost"]["rotating_out"] < 0


def _hot_row(**overrides):
    row = {
        "name": "示例板块",
        "return_1d": 1.0,
        "return_5d": 12.0,
        "net_inflow_1d": 1.0,
        "net_inflow_prior_4d": 1.0,
    }
    row.update(overrides)
    return row


def test_strong_threshold_is_actually_read_from_config():
    row = _hot_row()
    # vs_index = 12 - 8 = 4pp：默认 5pp 门槛下不算 strong
    assert sm.classify_sector_signal(row, 8.0)["signal"] != "strong"

    config = copy.deepcopy(sm.DEFAULTS)
    config["signals"]["strong_vs_index_5d"] = 3.0
    assert sm.classify_sector_signal(row, 8.0, config=config)["signal"] == "strong"


def test_rotating_out_thresholds_are_actually_read_from_config():
    row = _hot_row(return_5d=1.0, return_1d=-1.5, net_inflow_1d=-1.0, net_inflow_prior_4d=-3.0)
    assert sm.classify_sector_signal(row, 0.0)["signal"] == "neutral"

    config = copy.deepcopy(sm.DEFAULTS)
    config["signals"]["rotating_out_net_1d_yi"] = -0.5
    config["signals"]["rotating_out_prior_4d_yi"] = -1.0
    assert sm.classify_sector_signal(row, 0.0, config=config)["signal"] == "rotating_out"


def test_boost_magnitude_is_actually_read_from_config():
    momentum = {
        "schema": sm.SCHEMA,
        "sectors": [{"name": "示例板块", "signal": "strong", "signal_reason": "r"}],
    }
    assert sm.momentum_boost("示例板块", momentum)["delta"] == sm.DEFAULTS["boost"]["strong"]

    config = copy.deepcopy(sm.DEFAULTS)
    config["boost"]["strong"] = 0.25
    assert sm.momentum_boost("示例板块", momentum, config=config)["delta"] == 0.25


def test_limitup_ladder_matches_the_highest_qualifying_step():
    ctx = {"sector_limitups": {"示例板块": 6}}
    high = sc.sentiment_boost("600519", ctx, sector="示例板块")

    ctx_mid = {"sector_limitups": {"示例板块": 3}}
    mid = sc.sentiment_boost("600519", ctx_mid, sector="示例板块")

    ctx_low = {"sector_limitups": {"示例板块": 2}}
    low = sc.sentiment_boost("600519", ctx_low, sector="示例板块")

    ladder = {step["min_limitups"]: step["delta"] for step in sm.DEFAULTS["limitup_ladder"]}
    assert high["delta"] == ladder[5]
    assert mid["delta"] == ladder[3]
    assert low["delta"] == 0.0
    assert "板块赚钱效应强" in high["notes"][0]
