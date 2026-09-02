"""板块价格类因子 —— 缺数据不得被折叠成一个信号。

四个因子里有三种「缺失被读成强信号」的断法，本文件逐条守住：

- 广度分母为空 → 0.0 读起来是「板块极弱」，必须是不可得；
- 板块当日无收益样本 → 0.0 读起来是「平盘」，必须是 None；
- 基准中位数样本不足 → 当日没有基准，跨过它的窗口整体不可得，绝不当成基准收益 0。

外加一条口径断言：本模块的规范基准是全 A 中位数，``sector_momentum`` 的指数基准
必须自报 degraded，两者不能被当成同一个东西读。
"""

from __future__ import annotations

import copy

import yaml

import sector_momentum as sm
import sector_price_factors as spf
from config_registry import config_path


def _bars(count, *, pct_chg=1.0, close=10.0, sector_codes=0):
    """构造一天的全市场日线；前 ``sector_codes`` 只属于板块，其余只做基准分母。"""
    rows = []
    for index in range(count):
        rows.append(
            {
                "code": f"{600000 + index}",
                "close": close if index < sector_codes else 10.0,
                "pct_chg": pct_chg if index < sector_codes else 0.0,
            }
        )
    return rows


def _membership(sector_codes, sector="半导体"):
    return {f"{600000 + index}": sector for index in range(sector_codes)}


def _series(days, *, pct_chg=1.0, sector_codes=10, market_codes=200, start_close=10.0):
    """板块每天涨 ``pct_chg``%，市场中位数恒 0 —— 超额动量应当持续为正。"""
    day_bars = []
    close = start_close
    for index in range(days):
        close *= 1 + pct_chg / 100.0
        day_bars.append(
            (f"2026-01-{index + 1:02d}", _bars(market_codes, pct_chg=pct_chg,
                                               close=close, sector_codes=sector_codes))
        )
    return spf.build_daily_series(day_bars, _membership(sector_codes))


# ── 基准 ────────────────────────────────────────────────────────

def test_market_median_needs_a_real_cross_section():
    assert spf.market_median_return(_bars(50), min_samples=100) is None
    assert spf.market_median_return(_bars(200), min_samples=100) == 0.0


def test_day_without_a_basis_is_labelled_unavailable():
    series = spf.build_daily_series(
        [("2026-01-01", _bars(20, sector_codes=10))], _membership(10)
    )
    assert series[0]["market_basis"] == spf.UNAVAILABLE
    assert series[0]["market_median_return"] is None


# ── 广度 ────────────────────────────────────────────────────────

def test_breadth_with_no_valid_denominator_is_unavailable_not_zero():
    """0.0 广度读起来是「板块极弱」—— 那是凭空造出来的强信号。"""
    assert spf.breadth({"above_ma": 0, "valid_ma": 0}, min_members=5) is None
    assert spf.breadth({"above_ma": 0, "valid_ma": 3}, min_members=5) is None


def test_breadth_counts_only_members_with_a_full_ma_window():
    series = _series(25, pct_chg=1.0, sector_codes=10)
    entry = series[-1]["sectors"]["半导体"]
    assert entry["valid_ma"] == 10
    # 持续上涨 -> 全部站上 MA20
    assert spf.breadth(entry, min_members=5) == 1.0


def test_breadth_is_unavailable_before_the_ma_window_fills():
    series = _series(10, sector_codes=10)
    assert spf.breadth(series[-1]["sectors"]["半导体"], min_members=5) is None


# ── 等权收益 ────────────────────────────────────────────────────

def test_sector_without_return_samples_is_none_not_flat():
    day_bars = [("2026-01-01", [{"code": "600000", "close": 10.0, "pct_chg": None}]
                 + _bars(200)[1:])]
    series = spf.build_daily_series(day_bars, {"600000": "半导体"})
    assert series[0]["sectors"]["半导体"]["equal_weight_return"] is None


# ── 超额动量 / RS 斜率 ──────────────────────────────────────────

def test_excess_momentum_is_positive_when_sector_beats_a_flat_market():
    series = _series(30, pct_chg=1.0)
    value = spf.excess_momentum(series, "半导体", 20)
    assert value is not None and value > 0.2


def test_excess_momentum_window_longer_than_history_is_unavailable():
    assert spf.excess_momentum(_series(10), "半导体", 60) is None


def test_a_single_missing_basis_day_invalidates_the_window_without_interpolating():
    series = _series(30, pct_chg=1.0)
    series[-5]["market_median_return"] = None
    assert spf.excess_momentum(series, "半导体", 20) is None
    assert spf.rs_slope(series, "半导体", 20) is None


def test_rs_slope_is_positive_for_a_sector_pulling_away_from_the_basis():
    series = _series(30, pct_chg=1.0)
    slope = spf.rs_slope(series, "半导体", 20)
    assert slope is not None and slope > 0


def test_rs_slope_is_flat_when_sector_tracks_the_basis():
    """板块与全市场同涨：RS 不动，斜率应当约等于 0。"""
    day_bars = []
    close = 10.0
    for index in range(30):
        close *= 1.01
        rows = [
            {"code": f"{600000 + code}", "close": close, "pct_chg": 1.0}
            for code in range(200)
        ]
        day_bars.append((f"2026-01-{index + 1:02d}", rows))
    series = spf.build_daily_series(day_bars, _membership(10))
    assert abs(spf.rs_slope(series, "半导体", 20)) < 1e-6


# ── 组装 ────────────────────────────────────────────────────────

def test_factors_report_which_ones_are_unavailable_rather_than_dropping_them():
    series = _series(25, sector_codes=10)
    row = spf.sector_price_factors(series, "半导体")
    assert row["status"] == "ok"
    assert "excess_momentum_120d" in row["unavailable_factors"]
    assert row["excess_momentum_20d"] is not None


def test_too_few_members_is_unavailable():
    series = _series(25, sector_codes=2)
    assert spf.sector_price_factors(series, "半导体")["status"] == spf.UNAVAILABLE


def test_build_refuses_when_last_day_is_not_asof():
    payload = spf.build_sector_price_factors(_series(25), asof="2099-01-01")
    assert payload["status"] == spf.UNAVAILABLE
    assert payload["sectors"] == []


def test_build_marks_reconstruction_no_live_effect_and_unvalidated():
    series = _series(25)
    payload = spf.build_sector_price_factors(series, asof=series[-1]["trading_date"])
    assert payload["evidence_qualification"] == "exploratory_reconstruction"
    assert payload["live_effect"] == "none"
    assert payload["validated"] is False


# ── 基准口径不得混读（A5）──────────────────────────────────────

def test_canonical_basis_is_the_whole_a_median():
    series = _series(25)
    assert series[-1]["market_basis"] == spf.BASIS_WHOLE_A_MEDIAN


def test_sector_momentum_reports_its_index_basis_as_degraded():
    rows = [{"name": "半导体", "return_1d": 1.0, "return_5d": 12.0,
             "net_inflow_1d": 1.0, "net_inflow_prior_4d": 1.0}]
    payload = sm.build_sector_momentum(rows, index_return_5d=3.0, trading_date="2026-09-02")
    assert payload["market_basis"] == spf.BASIS_INDEX_DEGRADED

    without_basis = sm.build_sector_momentum(rows, index_return_5d=None,
                                             trading_date="2026-09-02")
    assert without_basis["market_basis"] == spf.UNAVAILABLE


# ── 配置 ────────────────────────────────────────────────────────

def test_yaml_section_and_module_defaults_do_not_drift():
    with open(config_path("scoring"), encoding="utf-8") as handle:
        section = (yaml.safe_load(handle) or {}).get(spf.CONFIG_SECTION)
    assert section == spf.DEFAULTS


def test_windows_are_actually_read_from_config():
    series = _series(25)
    config = copy.deepcopy(spf.DEFAULTS)
    config["momentum_windows"] = [5]
    row = spf.sector_price_factors(series, "半导体", config=config)
    assert "excess_momentum_5d" in row
    assert "excess_momentum_60d" not in row
