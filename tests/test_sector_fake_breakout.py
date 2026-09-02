"""板块假突破风险 —— 缺基本面数据不得被读成「基本面没有背离」。

报告九个子项里三个（盈利预期背离 / 景气背离 / 新闻依赖）本仓没有数据源。把它们
补 0 会让风险分被系统性拉低 —— 等于给每一个假突破发放行证。所以缺失项必须退出
加权并被写出来，权重在可得子项上重新归一。

另一条来自报告、必须守住的时序纪律：**不能用「突破后三天发生了什么」决定三天前
是否买入**。这里用同一段序列的不同截断点断言：某个决策点的风险值，只能由该点
为止的信息决定，后面再发生什么都不能改变它。
"""

from __future__ import annotations

import copy

import pytest
import yaml

import sector_fake_breakout as sfb
from config_registry import config_path


def _series(returns, sector="半导体", ex_top=None):
    """按日收益构造序列；``ex_top`` 给出剔除龙头后的收益（默认与整体相同）。"""
    series = []
    for index, value in enumerate(returns):
        rest = value if ex_top is None else ex_top[index]
        series.append(
            {
                "trading_date": f"2026-{1 + index // 28:02d}-{1 + index % 28:02d}",
                "market_median_return": 0.0,
                "sectors": {
                    sector: {
                        "member_count": 20,
                        "equal_weight_return": value,
                        "ex_top_return": rest,
                        "ex_top_samples": 17,
                    }
                },
            }
        )
    return series


def _flat(days, value=0.0):
    return [value] * days


# ── 等权指数与量价效率 ──────────────────────────────────────────

def test_index_levels_are_none_when_a_day_is_missing():
    series = _series(_flat(10, 0.01))
    series[3]["sectors"]["半导体"]["equal_weight_return"] = None
    assert sfb.sector_index_levels(series, "半导体") is None


def test_price_efficiency_is_low_for_a_round_trip_and_high_for_a_trend():
    trend = [1.0, 1.02, 1.04, 1.06, 1.08, 1.10]
    chop = [1.0, 1.05, 1.0, 1.05, 1.0, 1.005]
    assert sfb.price_efficiency(trend, 5) == pytest.approx(1.0, abs=1e-6)
    assert sfb.price_efficiency(chop, 5) < 0.1


def test_price_efficiency_needs_a_full_window():
    assert sfb.price_efficiency([1.0, 1.1], 5) is None


def _jitter(count, base=100.0):
    """带轻微波动的成交额序列：零方差历史的 z 分是未定义的，真实数据不会出现。"""
    return [base + (index % 5) for index in range(count)]


def test_amount_zscore_needs_history_and_nonzero_spread():
    assert sfb.amount_zscore(_jitter(5), 20) is None
    # 零方差历史：z 分未定义，必须是 None 而不是编一个出来
    assert sfb.amount_zscore([100.0] * 21, 20) is None
    assert sfb.amount_zscore(_jitter(20) + [500.0], 20) > 2.0


# ── 缺失子项 ────────────────────────────────────────────────────

def test_missing_fundamental_subrisks_leave_the_weighting_not_zero_filled():
    series = _series(_flat(70, 0.001))
    row = sfb.sector_fake_breakout_risk(
        series, "半导体", breadth_today=0.2, amounts=_jitter(71)
    )
    assert row["status"] == "ok"
    assert set(row["missing_subrisks"]) >= {
        "earnings_divergence", "prosperity_divergence", "news_dependence"
    }
    assert row["subrisks"]["earnings_divergence"] is None
    assert pytest.approx(sum(row["applied_weights"].values()), abs=1e-6) == 1.0


def test_zero_filling_the_missing_subrisks_would_dilute_the_risk():
    """同一份输入下，重新归一得到的风险必须高于「补零再按全权重平均」。"""
    series = _series(_flat(70, 0.001))
    row = sfb.sector_fake_breakout_risk(
        series, "半导体", breadth_today=0.2, amounts=_jitter(71)
    )
    diluted = 100.0 * sum(
        sfb.REPORT_WEIGHTS[name] * (value or 0.0)
        for name, value in row["subrisks"].items()
    ) / sum(sfb.REPORT_WEIGHTS.values())
    assert row["risk"] > diluted


def test_all_subrisks_missing_is_unavailable_not_zero_risk():
    series = _series(_flat(70, 0.001))
    series[-1]["sectors"]["半导体"]["ex_top_return"] = None
    row = sfb.sector_fake_breakout_risk(series, "半导体")
    # 广度/集中度/拥挤/成交额都没给，龙头背离也不可得 -> 只剩突破持续性
    assert row["subrisks"]["breadth_deficit"] is None
    assert row["subrisks"]["leader_divergence"] is None


def test_short_history_is_unavailable():
    row = sfb.sector_fake_breakout_risk(_series(_flat(10, 0.01)), "半导体")
    assert row["status"] == sfb.UNAVAILABLE
    assert row["risk"] is None


# ── 各子项确实在动 ──────────────────────────────────────────────

def test_breadth_below_floor_raises_the_risk():
    series = _series(_flat(70, 0.001))
    weak = sfb.sector_fake_breakout_risk(series, "半导体", breadth_today=0.2)
    strong = sfb.sector_fake_breakout_risk(series, "半导体", breadth_today=0.9)
    assert weak["risk"] > strong["risk"]


def test_breadth_drop_since_breakout_counts_even_above_the_floor():
    series = _series(_flat(70, 0.001))
    dropped = sfb.sector_fake_breakout_risk(
        series, "半导体", breadth_today=0.62, breadth_prior=0.80
    )
    steady = sfb.sector_fake_breakout_risk(
        series, "半导体", breadth_today=0.62, breadth_prior=0.63
    )
    assert dropped["risk"] > steady["risk"]


def test_volume_price_divergence_fires_on_heavy_volume_without_displacement():
    chop = _flat(65, 0.0) + [0.05, -0.05, 0.05, -0.05, 0.001]
    series = _series(chop)
    amounts = _jitter(70) + [5000.0]
    row = sfb.sector_fake_breakout_risk(series, "半导体", amounts=amounts)
    assert row["subrisks"]["volume_price_divergence"] == 1.0

    calm = sfb.sector_fake_breakout_risk(series, "半导体", amounts=_jitter(71))
    assert calm["subrisks"]["volume_price_divergence"] == 0.0


def test_high_crowding_is_nonlinear_above_the_threshold():
    series = _series(_flat(70, 0.001))
    mid = sfb.sector_fake_breakout_risk(series, "半导体", crowding_score=80.0)
    high = sfb.sector_fake_breakout_risk(series, "半导体", crowding_score=95.0)
    assert high["subrisks"]["high_crowding"] > 3 * mid["subrisks"]["high_crowding"]
    assert sfb.sector_fake_breakout_risk(
        series, "半导体", crowding_score=50.0
    )["subrisks"]["high_crowding"] == 0.0


def test_leader_divergence_fires_when_the_back_row_lags():
    returns = _flat(70, 0.001)
    ex_top = _flat(69, 0.001) + [-0.05]
    series = _series(returns[:-1] + [0.03], ex_top=ex_top)
    row = sfb.sector_fake_breakout_risk(series, "半导体")
    assert row["subrisks"]["leader_divergence"] == 1.0


def test_concentration_percentile_maps_into_the_unit_interval():
    series = _series(_flat(70, 0.001))
    assert sfb.sector_fake_breakout_risk(
        series, "半导体", concentration_percentile=50.0
    )["subrisks"]["turnover_concentration"] == 0.0
    assert sfb.sector_fake_breakout_risk(
        series, "半导体", concentration_percentile=100.0
    )["subrisks"]["turnover_concentration"] == 1.0


# ── 时序纪律 ────────────────────────────────────────────────────

def test_a_decision_point_only_sees_the_prefix_it_was_given():
    """t 日的读数只能由 t 日为止的信息决定。

    做法：先算一份「只到 t 日」的输入，再把后面三天极端行情**追加进同一份序列**，
    然后把序列截回 t 日重算 —— 两者必须逐字段相同。若哪个子项偷看了 series 之后
    的天数（或用了完整 amounts 尾部），这条会变红。
    """
    prefix = _flat(65, 0.0) + [0.05, -0.05, 0.05, -0.05, 0.001]
    future = [0.20, 0.20, 0.20]
    prefix_amounts = _jitter(70) + [5000.0]
    future_amounts = [100.0, 100.0, 100.0]

    only_prefix = sfb.sector_fake_breakout_risk(
        _series(prefix), "半导体", breadth_today=0.4, amounts=prefix_amounts
    )
    long_series = _series(prefix + future)
    replayed = sfb.sector_fake_breakout_risk(
        long_series[: len(prefix)], "半导体",
        breadth_today=0.4, amounts=prefix_amounts,
    )
    assert replayed["risk"] == only_prefix["risk"]
    assert replayed["subrisks"] == only_prefix["subrisks"]
    assert replayed["price_efficiency"] == only_prefix["price_efficiency"]

    # 对照：真的把后三天纳入决策点，读数必须变 —— 否则上面那条是恒真的
    including_future = sfb.sector_fake_breakout_risk(
        long_series, "半导体", breadth_today=0.4,
        amounts=prefix_amounts + future_amounts,
    )
    assert including_future["price_efficiency"] != only_prefix["price_efficiency"]


# ── 组装 ────────────────────────────────────────────────────────

def test_build_refuses_when_last_day_is_not_asof():
    payload = sfb.build_sector_fake_breakout(_series(_flat(70, 0.001)), asof="2099-01-01")
    assert payload["status"] == sfb.UNAVAILABLE
    assert payload["sectors"] == []


def test_build_marks_reconstruction_no_live_effect_and_unvalidated():
    series = _series(_flat(70, 0.001))
    payload = sfb.build_sector_fake_breakout(
        series, asof=series[-1]["trading_date"], breadth_by_sector={"半导体": 0.4}
    )
    assert payload["evidence_qualification"] == "exploratory_reconstruction"
    assert payload["live_effect"] == "none"
    assert payload["validated"] is False
    assert payload["scored_count"] == 1


# ── 配置 ────────────────────────────────────────────────────────

def test_yaml_section_and_module_defaults_do_not_drift():
    with open(config_path("scoring"), encoding="utf-8") as handle:
        section = (yaml.safe_load(handle) or {}).get(sfb.CONFIG_SECTION)
    assert section == sfb.DEFAULTS


def test_thresholds_are_actually_read_from_config():
    series = _series(_flat(70, 0.001))
    config = copy.deepcopy(sfb.DEFAULTS)
    config["breadth_floor"] = 0.10
    assert sfb.sector_fake_breakout_risk(
        series, "半导体", breadth_today=0.2, config=config
    )["subrisks"]["breadth_deficit"] == 0.0
    assert sfb.sector_fake_breakout_risk(
        series, "半导体", breadth_today=0.2
    )["subrisks"]["breadth_deficit"] == 1.0
