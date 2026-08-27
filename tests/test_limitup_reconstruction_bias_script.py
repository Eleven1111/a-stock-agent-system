import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "limitup_reconstruction_bias_script",
    ROOT / "scripts" / "limitup_reconstruction_bias.py",
)
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)


def test_run_writes_non_live_bias_artifact(tmp_path):
    truth = [{
        "date": "2026-08-20", "code": "600001", "name": "合成股",
        "first_seal_time": "093500", "last_seal_time": "094500",
        "reseal_time": "094500", "open_board_count": 1,
    }]
    rows = [
        {"time": "09:35", "close": 11.0},
        {"time": "09:40", "close": 10.9},
        {"time": "09:45", "close": 11.0},
    ]
    output = tmp_path / "bias.json"

    report = script.run(
        "2026-08-20",
        "2026-08-20",
        out=str(output),
        truth_fetcher=lambda start, end: (
            truth,
            {"status": "ok", "source": "fixture", "missing_dates": []},
        ),
        daily_loader=lambda codes, end, lookback: [{
            "code": "600001", "trading_date": "2026-08-20", "close": 11.0,
        }],
        minute_collector=lambda events, mode: (
            {("2026-08-20", "600001"): rows},
            {"mode": mode, "covered_keys": 1},
        ),
    )

    persisted = json.loads(output.read_text())
    assert report["status"] == "ok"
    assert report["eligible_for_divergence_reseal"] is False
    assert report["artifact_role"] == "bias_audit_only_not_event_backfill"
    assert persisted["open_board_count"]["exact_match_rate"] == 1.0
    assert "artifact" not in persisted


def test_truth_row_preserves_real_reseal_semantics():
    row = script.standardize_truth_row(
        {
            "代码": "600001", "名称": "合成股", "首次封板时间": 93500,
            "最后封板时间": 101500, "炸板次数": 2,
        },
        "20260820",
    )

    assert row["date"] == "2026-08-20"
    assert row["first_seal_time"] == "093500"
    assert row["last_seal_time"] == "101500"
    assert row["reseal_time"] == "101500"
    assert row["event_source"] == "eastmoney_zt_pool"


def test_run_uses_final_minute_close_only_as_bias_audit_fallback(tmp_path):
    truth = [{
        "date": "2026-08-20", "code": "600001", "name": "合成股",
        "first_seal_time": "093500", "last_seal_time": "093500",
        "reseal_time": None, "open_board_count": 0,
    }]
    report = script.run(
        "2026-08-20",
        "2026-08-20",
        out=str(tmp_path / "bias.json"),
        truth_fetcher=lambda start, end: (truth, {"status": "ok"}),
        daily_loader=lambda codes, end, lookback: [],
        minute_collector=lambda events, mode: (
            {("2026-08-20", "600001"): [
                {"time": "09:35", "close": 11.0},
                {"time": "15:00", "close": 11.0},
            ]},
            {"mode": mode, "covered_keys": 1},
        ),
    )

    assert report["covered_events"] == 1
    assert report["missing_daily_events"] == 1
    assert report["minute_close_limit_price_fallbacks"] == 1
    assert report["missing_limit_price_events"] == 0
    assert report["artifact_role"] == "bias_audit_only_not_event_backfill"
