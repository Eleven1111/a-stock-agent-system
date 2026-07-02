import json

from scripts import funnel_recall_report as frr
from scripts import score_calibration_report as scr


def _write_day(tmp_path, asof, records):
    day_dir = tmp_path / "skills" / "stock-triage" / "data" / "candidate_lifecycle"
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / f"{asof}.json").write_text(
        json.dumps({"schema": "candidate_lifecycle_v1", "records": records}),
        encoding="utf-8",
    )


def _rec(code, *, stage_events, current_stage, t3, mg, daban=0.0, momentum=0.0):
    return {
        "code": code,
        "current_stage": current_stage,
        "daban_score": daban,
        "trend_score": 0.0,
        "leader_score": 0.0,
        "momentum_5d": momentum,
        "change_pct": 0.0,
        "amount": 0.0,
        "turnover": 0.0,
        "volume_ratio_5d": 0.0,
        "breakout_20d": 0.0,
        "momentum_20d": 0.0,
        "momentum_60d": 0.0,
        "above_ma20": 0.0,
        "above_ma60": 0.0,
        "volatility_20d": 0.0,
        "stage_history": [{"stage": s, "selected": sel} for s, sel in stage_events],
        "outcome": {"resolved": True, "t3_close_ret": t3, "max_gain": mg},
    }


def test_funnel_report_shape_and_regret(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    _write_day(tmp_path, "2026-06-25", [
        _rec("000001", stage_events=[("discovery", False)], current_stage="discovery_rejected", t3=15.0, mg=16.0),
        _rec("000002", stage_events=[("discovery", True)], current_stage="watch_pool", t3=2.0, mg=3.0),
    ])

    report = frr.build_report(days=["2026-06-25"], outcome_key="t3_close_ret")

    assert report["status"] == "ok"
    assert report["research_only"] is True
    assert report["settled_days"] == ["2026-06-25"]
    disc = report["pooled"]["gates"][0]
    assert disc["stage"] == "discovery"
    assert disc["big_movers_wrongly_rejected"] == 1


def test_funnel_report_insufficient_when_no_settled_data(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    _write_day(tmp_path, "2026-06-25", [
        {"code": "000001", "current_stage": "watch_pool", "stage_history": [], "outcome": {"resolved": False}},
    ])
    report = frr.build_report(days=["2026-06-25"])
    assert report["status"] == "insufficient_data"


def test_calibration_report_shape_and_research_only(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    records = [
        _rec(f"{i:06d}", stage_events=[("discovery", i % 2 == 0)],
             current_stage="watch_pool" if i % 2 == 0 else "discovery_rejected",
             t3=float(i), mg=float(i), daban=float(i), momentum=float(i))
        for i in range(20)
    ]
    _write_day(tmp_path, "2026-06-25", records)

    report = scr.build_report(days=["2026-06-25"])

    assert report["status"] == "ok"
    assert report["research_only"] is True
    # daban_score was built to perfectly track the outcome here -> IC ~ 1.
    assert report["composite_scores"]["daban_score"]["ic_by_outcome"]["t3_close_ret"]["ic"] == 1.0
    assert report["four_dim_calibration"]["status"] == "insufficient_data"
    assert any(f["feature"] == "momentum_5d" for f in report["daban_features"])


def test_calibration_four_dim_join_reports_ok_once_paired(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    # 12 settled records with a monotone outcome.
    records = [
        _rec(f"{i:06d}", stage_events=[("discovery", True)], current_stage="watch_pool",
             t3=float(i), mg=float(i))
        for i in range(12)
    ]
    _write_day(tmp_path, "2026-06-25", records)
    # Matching four_dim log rows with technical tracking the outcome.
    log = tmp_path / "four_dim_log.jsonl"
    log.write_text("\n".join(
        json.dumps({"code": f"{i:06d}", "date": "2026-06-25",
                    "technical": float(i), "sentiment": 5.0, "catalyst": 5.0, "deep": 5.0})
        for i in range(12)
    ), encoding="utf-8")

    report = scr.build_report(days=["2026-06-25"], four_dim_log_path=str(log))
    fd = report["four_dim_calibration"]

    assert fd["status"] == "ok"
    assert fd["paired_rows"] == 12
    assert fd["ic_by_dimension"]["technical"]["t3_close_ret"]["ic"] == 1.0
