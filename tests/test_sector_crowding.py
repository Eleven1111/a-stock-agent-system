"""板块拥挤度 —— 「缺数据不得产生放行结论」是本模块的全部意义所在。

拥挤分是**反向**分数：高 = 危险、低 = 放行。因此每一条「静默补零/补中性」的路径
都不是保守退化，而是把危险信号变成放行信号。这里的断言全部围绕这一点：

- 空成分集不得给出数字（空集恒得一个好看的数，是本仓黑名单里的老坑）；
- 历史样本不足不得回落到 50 分位；
- 分量缺失不得补零，只能在可得分量上重新归一，并把缺哪一维写出来；
- 分数不可得时状态机必须是 unavailable / 不放行，而不是 NORMAL。

另有一条与报告**故意不同**的方向性断言：报告的 coverage 折扣（× sqrt(n/N)）对
「高分=好」的分数是保守的，对拥挤分则会把数据稀疏的板块压向 0 = 看起来不拥挤。
所以本模块不折扣分值，只降置信度，并在覆盖率过低时整体判不可得。
"""

from __future__ import annotations

import copy

import pytest
import yaml

import sector_crowding as sc
from config_registry import config_path


def _bar(code, amount, turn=2.0):
    return {"code": code, "amount": amount, "turn": turn}


def _membership(n, sector="半导体"):
    return {f"00000{index}"[-6:]: sector for index in range(n)}


def _day(trading_date, *, share, rel_turn, top, hhi):
    """构造一天的聚合，直接给出各分量取值（不经过 aggregate）。"""
    return {
        "trading_date": trading_date,
        "market_mean_turn": 1.0,
        "sectors": {
            "半导体": {
                "member_count": 20,
                "turnover_share": share,
                "mean_turn": rel_turn,
                "top_concentration": top,
                "amount_hhi": hhi,
            }
        },
    }


def _series(days, *, share=0.05, rel_turn=1.0, top=0.4, hhi=0.1):
    return [
        _day(f"2026-01-{index + 1:02d}", share=share, rel_turn=rel_turn, top=top, hhi=hhi)
        for index in range(days)
    ]


# ── 聚合 ────────────────────────────────────────────────────────

def test_aggregate_ignores_zero_amount_rows_instead_of_counting_them():
    """停牌股在缓存里没有成交额；算进分母会让集中度虚高。"""
    bars = [_bar("000001", 100.0), _bar("000002", 0.0), _bar("000003", None)]
    result = sc.aggregate_sector_day(
        bars, {"000001": "半导体", "000002": "半导体", "000003": "半导体"},
        trading_date="2026-09-02",
    )
    assert result["sectors"]["半导体"]["member_count"] == 1


def test_aggregate_skips_codes_without_a_sector():
    bars = [_bar("000001", 100.0), _bar("000002", 50.0)]
    result = sc.aggregate_sector_day(bars, {"000001": "半导体"}, trading_date="2026-09-02")
    assert set(result["sectors"]) == {"半导体"}
    # 全市场成交额仍然包含没有归属的那只 —— 否则占比会被系统性抬高
    assert result["market_amount"] == 150.0


def test_concentration_reflects_a_single_dominant_member():
    spread = sc.aggregate_sector_day(
        [_bar(f"00000{i}", 100.0) for i in range(5)],
        {f"00000{i}": "半导体" for i in range(5)},
        trading_date="2026-09-02",
    )["sectors"]["半导体"]
    concentrated = sc.aggregate_sector_day(
        [_bar("000000", 960.0)] + [_bar(f"00000{i}", 10.0) for i in range(1, 5)],
        {f"00000{i}": "半导体" for i in range(5)},
        trading_date="2026-09-02",
    )["sectors"]["半导体"]
    assert concentrated["amount_hhi"] > spread["amount_hhi"]


# ── 分位 ────────────────────────────────────────────────────────

def test_percentile_is_none_below_min_samples_not_a_neutral_fifty():
    assert sc.percentile_rank([1.0] * 10, 2.0, min_samples=60) is None


def test_percentile_ranks_within_history():
    history = [float(index) for index in range(100)]
    assert sc.percentile_rank(history, 99.0, min_samples=10) > 95
    assert sc.percentile_rank(history, 0.0, min_samples=10) < 5


# ── 单板块打分 ──────────────────────────────────────────────────

def _today(**overrides):
    row = {
        "member_count": 20,
        "turnover_share": 0.20,
        "mean_turn": 4.0,
        "market_mean_turn": 1.0,
        "top_concentration": 0.9,
        "amount_hhi": 0.5,
    }
    row.update(overrides)
    return row


def _history_rows(days=80, *, share=0.05, turn=1.0, top=0.4, hhi=0.1):
    return [
        {
            "member_count": 20,
            "turnover_share": share,
            "mean_turn": turn,
            "market_mean_turn": 1.0,
            "top_concentration": top,
            "amount_hhi": hhi,
        }
        for _ in range(days)
    ]


def test_crowded_today_against_calm_history_scores_high():
    result = sc.sector_crowding_score("半导体", _history_rows(), _today(), registered_members=20)
    assert result["status"] == "ok"
    assert result["score"] > 90


def test_too_few_observed_members_is_unavailable_not_a_low_score():
    result = sc.sector_crowding_score(
        "半导体", _history_rows(), _today(member_count=2), registered_members=20
    )
    assert result["status"] == sc.UNAVAILABLE
    assert result["score"] is None


def test_low_member_coverage_is_unavailable_not_a_low_score():
    """覆盖率不足必须整体判不可得 —— 折扣分值会把它压成「不拥挤」。"""
    result = sc.sector_crowding_score(
        "半导体", _history_rows(), _today(member_count=6), registered_members=100
    )
    assert result["status"] == sc.UNAVAILABLE
    assert result["score"] is None
    assert "覆盖" in result["reason"]


def test_short_history_makes_every_component_unavailable():
    result = sc.sector_crowding_score(
        "半导体", _history_rows(days=10), _today(), registered_members=20
    )
    assert result["status"] == sc.UNAVAILABLE
    assert result["score"] is None


def test_missing_component_renormalises_instead_of_scoring_zero():
    """换手缺失时，剩下两维重新归一；补零会把分数拉低成「不拥挤」。"""
    history = [dict(row, mean_turn=None, market_mean_turn=None) for row in _history_rows()]
    result = sc.sector_crowding_score(
        "半导体", history, _today(mean_turn=None, market_mean_turn=None), registered_members=20
    )
    assert result["status"] == "ok"
    assert "relative_turnover" in result["missing_components"]
    assert result["components"]["relative_turnover"] is None
    assert pytest.approx(sum(result["applied_weights"].values()), abs=1e-6) == 1.0
    assert result["score"] > 90


def test_weights_come_from_report_values_renormalised_over_available_components():
    result = sc.sector_crowding_score("半导体", _history_rows(), _today(), registered_members=20)
    expected_total = sum(sc.REPORT_WEIGHTS[name] for name in sc.AVAILABLE_COMPONENTS)
    for name in sc.AVAILABLE_COMPONENTS:
        assert result["applied_weights"][name] == pytest.approx(
            sc.REPORT_WEIGHTS[name] / expected_total, abs=1e-4
        )


# ── 状态机 ──────────────────────────────────────────────────────

def test_unavailable_score_never_yields_a_permissive_state():
    state = sc.crowding_state(None)
    assert state["state"] == sc.UNAVAILABLE
    assert state["allow_new_entry"] is False


@pytest.mark.parametrize(
    "score,expected",
    [(10.0, "NORMAL"), (72.0, "WATCH"), (85.0, "NO_ADD"), (95.0, "EXIT_RISK")],
)
def test_state_ladder(score, expected):
    assert sc.crowding_state(score)["state"] == expected


def test_cooldown_blocks_immediate_reentry_after_exit_risk():
    """追高→止损→次日动量又追：只靠分数掉下来不够，还要等够交易日。"""
    state = sc.crowding_state(60.0, prior_state="EXIT_RISK", sessions_since_exit=1)
    assert state["state"] == "COOLDOWN"
    assert state["allow_new_entry"] is False


def test_cooldown_also_requires_the_score_to_actually_come_down():
    state = sc.crowding_state(78.0, prior_state="EXIT_RISK", sessions_since_exit=5)
    assert state["state"] == "COOLDOWN"
    assert state["allow_new_entry"] is False


def test_reentry_allowed_once_both_conditions_hold():
    state = sc.crowding_state(60.0, prior_state="EXIT_RISK", sessions_since_exit=3)
    assert state["state"] == "NORMAL"
    assert state["allow_new_entry"] is True


# ── 全量组装 ────────────────────────────────────────────────────

def test_build_refuses_when_last_day_is_not_asof():
    payload = sc.build_sector_crowding(_series(5), asof="2026-09-02")
    assert payload["status"] == sc.UNAVAILABLE
    assert payload["sectors"] == []


def test_build_marks_the_whole_artifact_as_reconstruction_with_no_live_effect():
    series = _series(70)
    payload = sc.build_sector_crowding(
        series, asof=series[-1]["trading_date"], registered_members={"半导体": 20}
    )
    assert payload["evidence_qualification"] == "exploratory_reconstruction"
    assert payload["live_effect"] == "none"


def test_build_reports_unavailable_sectors_rather_than_dropping_them():
    series = _series(70)
    payload = sc.build_sector_crowding(
        series, asof=series[-1]["trading_date"], registered_members={"半导体": 1000}
    )
    assert payload["sector_count"] == 1
    assert payload["unavailable_count"] == 1
    assert payload["sectors"][0]["status"] == sc.UNAVAILABLE


def test_empty_series_is_unavailable():
    assert sc.build_sector_crowding([], asof="2026-09-02")["status"] == sc.UNAVAILABLE


# ── 配置 ────────────────────────────────────────────────────────

def test_yaml_section_and_module_defaults_do_not_drift():
    with open(config_path("scoring"), encoding="utf-8") as handle:
        section = (yaml.safe_load(handle) or {}).get(sc.CONFIG_SECTION)
    assert section == sc.DEFAULTS


def test_state_thresholds_are_actually_read_from_config():
    config = copy.deepcopy(sc.DEFAULTS)
    config["states"] = {"watch": 10.0, "no_add": 20.0, "exit_risk": 30.0}
    assert sc.crowding_state(35.0, config=config)["state"] == "EXIT_RISK"
    assert sc.crowding_state(35.0)["state"] == "NORMAL"
