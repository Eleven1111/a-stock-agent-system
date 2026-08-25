"""State PnL 分阶段收益归因（升级方案 P1）。

守五条，每条对应一类会让整份校准报告作废的失败：**禁未来函数**（标签只用截至 t
日的数据，构造一个"误用 t+1 打标签则结论翻转"的用例）、**空集返回 unavailable 而
不是 0.0**、**n<30 标 UNVERIFIED 且扣住均值**、**partial 覆盖日不进结论集**、
**制度断点复用 a_share_rules 常量且不跨制度配对**。
"""

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

import a_share_rules


ROOT = Path(__file__).resolve().parents[1]


def _module():
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "state_pnl_report", ROOT / "scripts" / "state_pnl_report.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def spr():
    return _module()


def record(day, *, max_board=0, premium=0.0, coverage="full", **extra):
    """一条最小 ``sentiment_daily`` 记录。字段名与数据集契约一致。"""
    row = {
        "trading_date": day,
        "coverage_status": coverage,
        "max_board": max_board,
        "limit_count": 20,
        "limit_down_count": 1,
        "limit_premium_open": premium,
        "limit_premium_close": premium,
        "limit_red_ratio": 0.5,
        "break_rate": 0.2,
    }
    row.update(extra)
    return row


def alternating_series(days=62, *, coverage="full", start="2026-01-05"):
    """偶数日冰点 / 奇数日极热；次日溢价被刻意设计成**与当日标签反号**。

    偶数日（冰点）的次日溢价 +5.0，奇数日（极热）的次日溢价 −5.0。误用 t+1 的
    标签会让两格均值互换 —— 结论直接翻转，这正是未来函数用例要抓的。
    """
    first = date.fromisoformat(start)
    return [
        record((first + timedelta(days=index)).isoformat(),
               max_board=0 if index % 2 == 0 else 9,
               premium=5.0 if index % 2 == 1 else -5.0,
               coverage=coverage)
        for index in range(days)
    ]


# --- 制度分段：断点来自 a_share_rules，不跨制度混合 -----------------------


def test_regime_breakpoints_reuse_a_share_rules_constants(spr):
    """断点常量不在本脚本里重抄一份日期。"""
    days = [day for day, _ in spr.REGIME_BREAKPOINTS]
    assert a_share_rules.CHINEXT_20PCT_FROM in days
    assert a_share_rules.STAR_MARKET_OPEN in days
    assert a_share_rules.BSE_OPEN in days
    assert a_share_rules.SSE_RISK_WARNING_10PCT_FROM in days


def test_chinext_breakpoint_splits_regimes(spr):
    assert spr.regime_of("2020-08-21") != spr.regime_of("2020-08-24")
    assert spr.regime_of("2020-08-24") == "chinext_20pct"
    assert spr.regime_of("not-a-date") is None


def test_pairs_straddling_a_breakpoint_are_dropped(spr):
    rows = [record("2020-08-21", max_board=3), record("2020-08-24", max_board=3),
            record("2020-08-25", max_board=3)]
    observations = spr.build_observations(rows, config=None)
    assert [row["trading_date"] for row in observations] == ["2020-08-24"]


# --- 禁未来函数 ----------------------------------------------------------


def test_labels_never_read_tomorrow(spr):
    rows = [record("2026-01-05", max_board=0), record("2026-01-06", max_board=9)]
    labels = spr.label_series(rows, config=None)
    assert labels[0]["five_tier"] == "冰点"   # 若误读 t+1 的 max_board=9 会变极热
    assert labels[1]["five_tier"] == "极热"


def test_label_of_a_day_is_unchanged_by_appending_future_days(spr):
    rows = alternating_series(20)
    prefix = spr.label_series(rows[:11], config=None)
    full = spr.label_series(rows, config=None)
    assert [row["five_tier"] for row in prefix] == [row["five_tier"] for row in full[:11]]
    assert [row["market_state"] for row in prefix] == [row["market_state"] for row in full[:11]]


def test_lookahead_labelling_would_flip_the_conclusion(spr):
    """正确实现下冰点格均值为正；若用 t+1 打标签，正负号会挂到极热格上。"""
    observations = spr.build_observations(alternating_series(62), config=None)
    matrix = spr.state_matrix(observations, "five_tier", "next_limit_premium_close")
    cells = matrix["bse_open"]
    assert cells["冰点"]["status"] == "ok" and cells["冰点"]["mean"] == 5.0
    assert cells["极热"]["status"] == "ok" and cells["极热"]["mean"] == -5.0


# --- 空集与样本门槛 ------------------------------------------------------


def test_empty_cell_is_unavailable_not_zero(spr):
    cell = spr.summarize_cell([])
    assert cell == {"n": 0, "status": "unavailable", "mean": None, "median": None}
    # "这个状态没赚钱"和"这个状态没样本"必须可区分：均值不是 0.0，是 None。
    assert cell["mean"] is None and cell["status"] != "ok"


def test_cell_below_min_samples_is_unverified_and_withholds_mean(spr):
    cell = spr.summarize_cell([1.0] * 29)
    assert cell["status"] == "UNVERIFIED"
    assert cell["n"] == 29
    assert cell["mean"] is None and cell["median"] is None


def test_cell_at_min_samples_reports_mean(spr):
    cell = spr.summarize_cell([2.0] * 30)
    assert cell["status"] == "ok" and cell["mean"] == 2.0


def test_empty_input_produces_no_numbers(spr):
    report = spr.build_report([])
    assert report["trading_day_count"] == 0
    assert report["observation_count"] == 0
    assert report["conclusive"]["sample_count"] == 0
    assert report["conclusive"]["state_pnl"]["five_tier"]["next_limit_premium_close"] == {}


def test_spearman_returns_none_without_variation(spr):
    assert spr.spearman([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None
    assert spr.spearman([1.0, 2.0], [1.0, 2.0]) is None
    assert spr.spearman([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == 1.0


def test_discrimination_below_threshold_is_unverified_without_ic(spr):
    observations = spr.build_observations(alternating_series(20), config=None)
    entry = spr.discrimination(observations, "next_limit_premium_close",
                               config=None)["bse_open"]
    assert entry["five_tier"]["status"] == "UNVERIFIED"
    assert entry["five_tier"]["ic"] is None
    assert entry["five_tier"]["monotonic"] is None
    assert entry["sentiment_score_continuous"] == {"n": 0, "status": "unavailable", "ic": None}


# --- 覆盖率过滤 ----------------------------------------------------------


def test_partial_coverage_days_never_enter_the_conclusion_set(spr):
    report = spr.build_report(alternating_series(62, coverage="partial"))
    assert report["coverage_breakdown"] == {"partial": 62}
    assert report["conclusive"]["sample_count"] == 0
    assert report["conclusive"]["state_pnl"]["five_tier"]["next_limit_premium_close"] == {}
    assert report["partial_subset"]["sample_count"] == 61
    assert report["partial_subset"]["conclusive"] is False


def test_one_partial_end_disqualifies_the_pair(spr):
    rows = [record("2026-01-05", max_board=3), record("2026-01-06", max_board=3,
                                                      coverage="partial")]
    observations = spr.build_observations(rows, config=None)
    assert observations[0]["coverage_status"] == "full"
    assert spr.filter_full_coverage(observations) == []


# --- 被解释变量取自 t+1 --------------------------------------------------


def test_outcomes_are_taken_from_the_next_day(spr):
    today = record("2026-01-05", premium=1.0, break_rate=0.2)
    tomorrow = record("2026-01-06", premium=7.0, break_rate=0.5, limit_red_ratio=0.9)
    outcomes = spr.next_day_outcomes(today, tomorrow)
    assert outcomes["next_limit_premium_open"] == 7.0
    assert outcomes["next_limit_premium_close"] == 7.0
    assert outcomes["next_limit_red_ratio"] == 0.9
    assert outcomes["next_break_rate_change"] == pytest.approx(0.3)


def test_break_rate_change_is_none_when_either_side_missing(spr):
    today = record("2026-01-05", break_rate=None)
    tomorrow = record("2026-01-06", break_rate=0.5)
    assert spr.next_day_outcomes(today, tomorrow)["next_break_rate_change"] is None
    assert spr.next_day_outcomes(tomorrow, today)["next_break_rate_change"] is None


# --- 三套口径都在 -------------------------------------------------------


def test_all_three_schemes_are_labelled(spr):
    """S_t 需要 180 日预热；预热不足时该口径记不可用，另两套照常打标签。"""
    labels = spr.label_series(alternating_series(20), config=None)
    assert labels[-1]["five_tier"] and labels[-1]["market_state"]
    assert labels[-1]["sentiment_band"] is None
    assert labels[-1]["reasons"]["sentiment_band"] == "config_missing"


def test_sentiment_band_available_after_warmup(spr):
    import sentiment_score

    config = sentiment_score.load_config()
    assert config is not None, "config/scoring.yaml 缺 sentiment_score 节"
    rows = alternating_series(int(config["min_history"]) + 5)
    for index, row in enumerate(rows):
        row.update({"adr": 1.0 + index % 5 * 0.1, "board4plus": index % 4,
                    "leader_damage": -1.0 + index % 5 * 0.5})
    labels = spr.label_series(rows, config=config)
    assert labels[-1]["sentiment_band"] is not None
    assert labels[-1]["sentiment_score"] is not None
    assert labels[0]["sentiment_band"] is None  # 预热不足，不给 50 分


def test_full_coverage_but_empty_sample_is_not_marked_conclusive(spr):
    """full 覆盖 + 零样本时 conclusive 必须为 False。

    否则下游一句 ``if section["conclusive"]`` 会把空矩阵当成已校准结果放行——
    覆盖口径够格与真有结论是两回事，字段必须分开且默认保守。
    """
    report = spr.build_report([])
    section = report["conclusive"]
    assert section["sample_count"] == 0
    assert section["conclusion_eligible_scope"] is True   # 口径本身够格
    assert section["has_conclusion"] is False             # 但没有任何一格达门槛
    assert section["conclusive"] is False                 # 合取后不得放行
