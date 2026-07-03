"""Strategy attribution report tests — bucketing, gating, honest degradation."""

import json
import os
import sys

BASE = os.path.dirname(__file__)
PROJ = os.path.abspath(os.path.join(BASE, ".."))
sys.path.insert(0, os.path.join(PROJ, "skills", "common"))
sys.path.insert(0, PROJ)
# Append (not prepend): scripts/ holds exec-at-import compatibility wrappers
# (e.g. scripts/performance_tracker.py) that must never shadow the canonical
# modules under skills/*/scripts for other test files in the same session.
sys.path.append(os.path.join(PROJ, "scripts"))

import strategy_attribution_report as report  # noqa: E402


def _signal(
    code,
    *,
    strategy_id="daban:first_board_reseal",
    t1_close_ret,
    horizon_ret=None,
    t1_open_premium=None,
    tier=None,
    signal_date="2026-06-01",
    settlement_status="final",
):
    row = {
        "code": code,
        "name": f"stock{code}",
        "grade": "A",
        "strategy_id": strategy_id,
        "signal_date": signal_date,
        "outcome": "win" if t1_close_ret >= 0 else "loss",
        "t1_close_ret": t1_close_ret,
        "settlement_status": settlement_status,
    }
    if horizon_ret is not None:
        row["horizon_ret"] = horizon_ret
    if t1_open_premium is not None:
        row["t1_open_premium"] = t1_open_premium
    if tier is not None:
        row["selection_context"] = {"market_timing": {"tier": tier}}
    return row


# ---------- pure bucketing / stats helpers ----------

def test_return_stats_basic():
    stats = report._return_stats([10.0, -5.0, 3.0])
    assert stats["median"] == 3.0
    assert stats["mean"] == round((10.0 - 5.0 + 3.0) / 3, 2)
    assert stats["win_rate"] == round(2 / 3 * 100, 1)


def test_return_stats_empty():
    stats = report._return_stats([])
    assert stats == {"median": None, "mean": None, "win_rate": None}


def test_bucket_summary_flags_insufficient_sample():
    records = [_signal(str(i), t1_close_ret=1.0, horizon_ret=1.0) for i in range(5)]
    summary = report._bucket_summary(records, min_samples=10)
    assert summary["sample_count"] == 5
    assert summary["insufficient_sample"] is True

    records_ok = [_signal(str(i), t1_close_ret=1.0, horizon_ret=1.0) for i in range(10)]
    summary_ok = report._bucket_summary(records_ok, min_samples=10)
    assert summary_ok["insufficient_sample"] is False


def test_bucket_summary_reports_auction_premium_when_present():
    records = [
        _signal(str(i), t1_close_ret=1.0, t1_open_premium=2.5)
        for i in range(3)
    ]
    summary = report._bucket_summary(records, min_samples=10)
    assert summary["auction_premium"]["mean"] == 2.5


def test_bucket_summary_omits_auction_premium_when_absent():
    records = [_signal(str(i), t1_close_ret=1.0) for i in range(3)]
    summary = report._bucket_summary(records, min_samples=10)
    assert "auction_premium" not in summary


# ---------- entry pattern dimension ----------

def test_entry_pattern_dimension_buckets_by_strategy_id_suffix():
    records = [
        *[_signal(str(i), strategy_id="daban:first_board_reseal", t1_close_ret=5.0) for i in range(12)],
        *[_signal(f"b{i}", strategy_id="daban:second_board_weak_to_strong", t1_close_ret=-3.0) for i in range(4)],
    ]
    dim = report.dimension_entry_pattern(records, min_samples=10)
    assert dim["status"] == "ok"
    assert dim["buckets"]["first_board_reseal"]["sample_count"] == 12
    assert dim["buckets"]["first_board_reseal"]["insufficient_sample"] is False
    assert dim["buckets"]["second_board_weak_to_strong"]["sample_count"] == 4
    assert dim["buckets"]["second_board_weak_to_strong"]["insufficient_sample"] is True


def test_entry_pattern_dimension_unavailable_when_no_daban_prefix():
    records = [_signal(str(i), strategy_id="trend_pullback", t1_close_ret=1.0) for i in range(5)]
    dim = report.dimension_entry_pattern(records, min_samples=10)
    assert dim["status"] == "unavailable"
    assert "reason" in dim


# ---------- market temperature dimension ----------

def test_market_temperature_dimension_buckets_strong_mid_weak():
    records = [
        *[_signal(str(i), t1_close_ret=8.0, tier="极热") for i in range(10)],
        *[_signal(f"m{i}", t1_close_ret=2.0, tier="发酵") for i in range(10)],
        *[_signal(f"w{i}", t1_close_ret=-6.0, tier="冰点") for i in range(10)],
    ]
    dim = report.dimension_market_temperature(records, min_samples=10)
    assert dim["status"] == "ok"
    assert dim["buckets"]["强"]["sample_count"] == 10
    assert dim["buckets"]["中"]["sample_count"] == 10
    assert dim["buckets"]["弱"]["sample_count"] == 10
    assert dim["buckets"]["弱"]["t1_close_ret"]["mean"] == -6.0


def test_market_temperature_dimension_unavailable_without_selection_context():
    records = [_signal(str(i), t1_close_ret=1.0) for i in range(5)]
    dim = report.dimension_market_temperature(records, min_samples=10)
    assert dim["status"] == "unavailable"
    assert "selection_context" in dim["reason"]


# ---------- honestly-unavailable dimensions ----------

def test_ladder_height_dimension_always_unavailable():
    dim = report.dimension_ladder_height([_signal("1", t1_close_ret=1.0)])
    assert dim["status"] == "unavailable"
    assert "signal_context.json" in dim["reason"]


def test_board_level_dimension_always_unavailable():
    dim = report.dimension_board_level([_signal("1", t1_close_ret=1.0)])
    assert dim["status"] == "unavailable"
    assert "pattern" in dim["reason"]


# ---------- build_report integration (in-memory records) ----------

def test_build_report_empty_history_is_honest_not_crashing():
    result = report.build_report(records=[])
    assert result["status"] == "insufficient_data"
    assert result["t1_observed_signals"] == 0
    assert result["final_signals"] == 0
    assert result["dimensions"]["theme_stage_ladder_height"]["status"] == "unavailable"
    assert result["dimensions"]["board_level"]["status"] == "unavailable"


def test_build_report_only_pending_signals_is_insufficient_data():
    records = [{"code": "1", "strategy_id": "daban:first_board_reseal", "outcome": "pending"}]
    result = report.build_report(records=records)
    assert result["status"] == "insufficient_data"


def test_build_report_computes_baseline_and_dimensions():
    records = [
        _signal(str(i), strategy_id="daban:first_board_reseal", t1_close_ret=6.0,
                horizon_ret=-4.0, tier="加速")
        for i in range(12)
    ] + [
        _signal(f"b{i}", strategy_id="daban:second_board_weak_to_strong", t1_close_ret=-2.0,
                horizon_ret=-8.0, tier="冰点")
        for i in range(12)
    ]
    result = report.build_report(records=records, min_samples=10)
    assert result["status"] == "ok"
    assert result["t1_observed_signals"] == 24
    assert result["final_signals"] == 24
    assert result["baseline"]["sample_count"] == 24
    assert result["dimensions"]["entry_pattern"]["status"] == "ok"
    assert result["dimensions"]["market_temperature"]["status"] == "ok"
    # T+3 (horizon_ret) reversal shows up distinctly from T+1
    fbr = result["dimensions"]["entry_pattern"]["buckets"]["first_board_reseal"]
    assert fbr["t1_close_ret"]["mean"] == 6.0
    assert fbr["t3_horizon_ret"]["mean"] == -4.0


def test_build_report_excludes_non_daban_strategies_from_daban_count():
    records = [
        _signal("1", strategy_id="trend_pullback", t1_close_ret=5.0),
        *[_signal(str(i), strategy_id="daban:first_board_reseal", t1_close_ret=1.0) for i in range(10)],
    ]
    result = report.build_report(records=records)
    assert result["total_signals"] == 11
    assert result["daban_signals"] == 10
    assert result["t1_observed_signals"] == 10


def test_build_report_treats_default_strategy_id_as_daban_baseline():
    # legacy rows recorded via performance_tracker.py --record without --strategy-id
    records = [_signal(str(i), strategy_id="default", t1_close_ret=2.0) for i in range(10)]
    result = report.build_report(records=records)
    assert result["daban_signals"] == 10
    assert result["t1_observed_signals"] == 10


# ---------- H3: provisional / final cohort split ----------

def test_provisional_settlements_split_from_final_cohort():
    records = [
        _signal(str(i), t1_close_ret=5.0, horizon_ret=2.0, settlement_status="final")
        for i in range(6)
    ] + [
        # provisional rows carry a partial-window horizon_ret that must NOT
        # leak into T+3 stats
        _signal(f"p{i}", t1_close_ret=-4.0, horizon_ret=99.0, settlement_status="provisional")
        for i in range(6)
    ]
    result = report.build_report(records=records, min_samples=10)
    assert result["t1_observed_signals"] == 12
    assert result["final_signals"] == 6
    baseline = result["baseline"]
    assert baseline["sample_count"] == 12
    assert baseline["final_sample_count"] == 6
    # T+1 uses all 12 (provisional T+1 return has already happened)
    assert baseline["t1_close_ret"]["mean"] == 0.5
    assert baseline["t1_cohort"] == "t1_observed_includes_provisional"
    # T+3 uses final only: provisional horizon_ret=99 excluded
    assert baseline["t3_sample_count"] == 6
    assert baseline["t3_horizon_ret"]["mean"] == 2.0
    assert baseline["t3_cohort"] == "final_only"


def test_bucket_summary_t3_excludes_provisional_horizon():
    records = [
        _signal("1", t1_close_ret=1.0, horizon_ret=3.0, settlement_status="final"),
        _signal("2", t1_close_ret=1.0, horizon_ret=50.0, settlement_status="provisional"),
    ]
    summary = report._bucket_summary(records, min_samples=1)
    assert summary["sample_count"] == 2
    assert summary["final_sample_count"] == 1
    assert summary["t3_sample_count"] == 1
    assert summary["t3_horizon_ret"]["mean"] == 3.0


# ---------- M3: missing tier goes to unknown bucket, not dropped ----------

def test_market_temperature_unknown_bucket_captures_missing_tier():
    records = [
        *[_signal(str(i), t1_close_ret=5.0, tier="发酵") for i in range(10)],
        *[_signal(f"n{i}", t1_close_ret=-5.0) for i in range(4)],
    ]
    dim = report.dimension_market_temperature(records, min_samples=10)
    assert dim["status"] == "ok"
    assert dim["buckets"]["中"]["sample_count"] == 10
    assert dim["buckets"]["unknown"]["sample_count"] == 4
    assert dim["buckets"]["unknown"]["insufficient_sample"] is True
    assert dim["buckets"]["unknown"]["t1_close_ret"]["mean"] == -5.0
    assert dim["coverage"] == {"with_tier": 10, "without_tier": 4, "total_settled": 14}


def test_market_temperature_unclassified_tier_goes_to_unknown():
    records = [
        *[_signal(str(i), t1_close_ret=1.0, tier="冰点") for i in range(10)],
        _signal("x", t1_close_ret=0.0, tier="neutral"),
    ]
    dim = report.dimension_market_temperature(records, min_samples=10)
    assert dim["buckets"]["unknown"]["sample_count"] == 1
    assert dim["unclassified_tiers_seen"] == ["neutral"]


# ---------- L1: dirty values skipped via _num, no crash, no miscount ----------

def test_dirty_numeric_values_are_skipped_not_crashing():
    records = [
        {"code": "1", "strategy_id": "daban:first_board_reseal",
         "t1_close_ret": "abc", "settlement_status": "final"},
        {"code": "2", "strategy_id": "daban:first_board_reseal",
         "t1_close_ret": True, "settlement_status": "final"},
        _signal("3", t1_close_ret=4.0, horizon_ret="oops", t1_open_premium=True),
    ]
    result = report.build_report(records=records)
    # string/bool t1_close_ret rows are not valid T+1 observations
    assert result["t1_observed_signals"] == 1
    baseline = result["baseline"]
    assert baseline["t1_close_ret"]["mean"] == 4.0
    # dirty horizon_ret and bool premium skipped, not coerced
    assert baseline["t3_sample_count"] == 0
    assert "auction_premium" not in baseline


# ---------- CLI / on-disk integration ----------

def test_load_settled_signals_reads_legacy_history_file(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    import importlib

    import paths
    importlib.reload(paths)
    importlib.reload(signal_ledger := sys.modules["signal_ledger"])
    importlib.reload(report)

    history_path = report.LEGACY_HISTORY_FILE
    os.makedirs(os.path.dirname(history_path), exist_ok=True)
    with open(history_path, "w", encoding="utf-8") as handle:
        json.dump(
            [_signal(str(i), t1_close_ret=1.0) for i in range(3)],
            handle,
        )

    loaded = report.load_settled_signals()
    assert len(loaded) == 3


def test_build_report_with_empty_state_home_is_empty_not_error(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    import importlib

    import paths
    importlib.reload(paths)
    importlib.reload(sys.modules["signal_ledger"])
    importlib.reload(report)

    result = report.build_report()
    assert result["status"] == "insufficient_data"
    assert result["t1_observed_signals"] == 0


def test_format_markdown_handles_insufficient_data():
    result = report.build_report(records=[])
    text = report.format_markdown(result)
    assert "打板策略归因报告" in text
    assert "数据不足" in text or "尚无已结算" in text


def test_format_markdown_renders_unavailable_dimensions():
    records = [_signal(str(i), t1_close_ret=1.0) for i in range(10)]
    result = report.build_report(records=records)
    text = report.format_markdown(result)
    assert "不可用" in text
