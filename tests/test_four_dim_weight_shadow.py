from datetime import date, timedelta

import pytest

import four_dim_score_log as observation_contract
import four_dim_weight_research as research
import local_market_history
from scripts import four_dim_weight_shadow as runner


DIMS = ("technical", "sentiment", "catalyst", "deep")
CURRENT = {
    "daban": {"technical": 0.15, "sentiment": 0.35, "catalyst": 0.30, "deep": 0.20},
    "trend": {"technical": 0.35, "sentiment": 0.10, "catalyst": 0.25, "deep": 0.30},
}


def _observation(identifier, trading_date, lane, scores):
    row = {
        "schema": "four_dim_observation_v2",
        "observation_id": identifier,
        "code": identifier[-6:].zfill(6),
        "trading_date": trading_date,
        "strategy_lane": lane,
        "dimensions": {
            dim: {"score": float(score), "status": "available", "source": "fixture", "asof": trading_date}
            for dim, score in zip(DIMS, scores)
        },
        "current_weights": CURRENT[lane],
        "effective_weights": CURRENT[lane],
        "point_in_time": {"status": "complete", "missing": []},
        "input_snapshot": {"ref": "fixture.json", "sha256": "a" * 64},
        "input_fingerprint_sha256": "d" * 64,
        "input_bundle_sha256": None,
        "versions": {
            "scorer_sha256": "b" * 64,
            "config_sha256": "c" * 64,
            "contract_sha256": "f" * 64,
        },
        "research_only": True,
        "live_effect": "none",
    }
    row["input_bundle_sha256"] = observation_contract.recompute_input_bundle_sha256(row)
    row["observation_id"] = observation_contract.recompute_observation_id(row)
    return row


def test_labels_use_local_history_for_t1_t3_benchmark_and_cost(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    days = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06"]
    rows = []
    for code, closes in (("600001", [10.0, 11.0, 12.0, 13.0]), ("000300", [100.0, 101.0, 102.0, 103.0])):
        for trading_date, close in zip(days, closes):
            rows.append({
                "code": code,
                "trading_date": trading_date,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "preclose": close,
                "source": "fixture",
                "source_version": "v1",
            })
    local_market_history.upsert_daily_bars(rows)
    obs = _observation("obs-600001", days[0], "trend", [8, 6, 7, 5])

    labels = research.build_labels([obs])

    label = labels[obs["observation_id"]]
    assert label["status"] == "complete"
    assert label["t1_gross_return_pct"] == pytest.approx(10.0)
    assert label["t3_gross_return_pct"] == pytest.approx(30.0)
    assert label["t1_benchmark_return_pct"] == pytest.approx(1.0)
    assert label["t3_benchmark_return_pct"] == pytest.approx(3.0)
    assert label["t1_net_excess_pct"] < 9.0
    assert label["t3_net_excess_pct"] < 27.0
    assert label["fee_schedule_version"]
    assert len(label["history_sha256"]) == 64


def test_small_sample_is_explicitly_insufficient_and_non_live():
    observations = []
    labels = {}
    for offset in range(3):
        trading_date = (date(2026, 1, 1) + timedelta(days=offset)).isoformat()
        obs = _observation(f"trend-{offset:06d}", trading_date, "trend", [8, 6, 7, 5])
        observations.append(obs)
        labels[obs["observation_id"]] = {
            "status": "complete",
            "t1_net_excess_pct": 1.0,
            "t3_net_excess_pct": 2.0,
        }

    report = research.build_shadow_report(observations, labels, current_weights=CURRENT)

    assert report["status"] == "insufficient_data"
    assert report["research_only"] is True
    assert report["live_effect"] == "none"
    assert report["lanes"]["trend"]["status"] == "insufficient_training_days"
    assert report["lanes"]["daban"]["status"] == "no_valid_observations"
    assert report["promotion"]["allowed"] is False


def test_each_lane_gets_simplex_posterior_and_same_date_oos_comparison():
    observations = []
    labels = {}
    start = date(2025, 1, 1)
    for lane_index, lane in enumerate(("trend", "daban")):
        for offset in range(120):
            trading_date = (start + timedelta(days=offset)).isoformat()
            for stock_index in range(2):
                identifier = f"{lane}-{offset:03d}-{stock_index:06d}"
                technical = 2.0 + (offset % 8) + stock_index
                sentiment = 9.0 - (offset % 6)
                catalyst = 3.0 + ((offset + stock_index) % 7)
                deep = 4.0 + ((offset * 2 + stock_index) % 5)
                obs = _observation(identifier, trading_date, lane, [technical, sentiment, catalyst, deep])
                observations.append(obs)
                # Lane-specific signal ensures the two posteriors are estimated independently.
                target = technical if lane_index == 0 else sentiment
                labels[obs["observation_id"]] = {
                    "status": "complete",
                    "t1_net_excess_pct": target * 0.3 - 1.0,
                    "t3_net_excess_pct": target * 0.5 - 2.0,
                }

    report = research.build_shadow_report(
        observations,
        labels,
        current_weights=CURRENT,
        posterior_draws=512,
        seed=7,
    )

    assert report["status"] == "oos_complete_research_only"
    assert report["promotion"]["allowed"] is False
    for lane in ("trend", "daban"):
        result = report["lanes"][lane]
        assert result["status"] == "oos_complete_research_only"
        posterior = result["posterior"]
        assert set(posterior["weights"]) == set(DIMS)
        assert sum(item["mean"] for item in posterior["weights"].values()) == pytest.approx(1.0, abs=0.02)
        assert all(item["lower_90"] <= item["mean"] <= item["upper_90"] for item in posterior["weights"].values())
        assert set(result["comparison"]) >= {"current", "equal", "shadow", "ablation"}
        assert result["comparison_dates"] == 60
        assert result["rollback"]["action"] == "no_live_change"
    assert len(report["version_hashes"]["observation_set_sha256"]) == 64
    assert len(report["version_hashes"]["label_set_sha256"]) == 64


def test_frozen_fit_never_rolls_when_later_oos_arrives():
    observations = []
    labels = {}
    start = date(2025, 1, 1)
    for offset in range(60):
        day = (start + timedelta(days=offset)).isoformat()
        obs = _observation(f"trend-{offset:06d}", day, "trend", [offset % 9, 6, 7, 5])
        observations.append(obs)
        labels[obs["observation_id"]] = {
            "status": "complete", "t1_net_excess_pct": 1.0,
            "t3_net_excess_pct": float(offset % 9), "t3_date": day,
        }
    first = research.build_shadow_report(
        observations, labels, current_weights=CURRENT, posterior_draws=256,
        asof="2025-12-31",
    )
    frozen = first["frozen_lanes"]
    original_model = frozen["trend"]["model_sha256"]
    original_weights = frozen["trend"]["shadow_weights"]

    for offset in range(60, 125):
        day = (start + timedelta(days=offset)).isoformat()
        obs = _observation(f"trend-{offset:06d}", day, "trend", [9, offset % 8, 2, 1])
        observations.append(obs)
        labels[obs["observation_id"]] = {
            "status": "complete", "t1_net_excess_pct": 2.0,
            "t3_net_excess_pct": float(offset % 8), "t3_date": day,
        }
    later = research.build_shadow_report(
        observations, labels, current_weights=CURRENT, posterior_draws=256,
        asof="2025-12-31", frozen_lanes=frozen,
    )
    trend = later["lanes"]["trend"]

    assert trend["model_sha256"] == original_model
    assert trend["freeze"]["shadow_weights"] == original_weights
    assert trend["comparison_dates"] == 60
    assert trend["comparison_date_values"][0] > trend["fit_cutoff"]
    assert len(trend["comparison_date_values"]) == 60


def test_strict_attrition_rejects_degraded_or_missing_hash_observations():
    good = _observation("trend-000001", "2026-01-01", "trend", [8, 6, 7, 5])
    degraded = _observation("trend-000002", "2026-01-02", "trend", [8, 6, 7, 5])
    degraded["dimensions"]["catalyst"]["status"] = "degraded"
    degraded["input_bundle_sha256"] = observation_contract.recompute_input_bundle_sha256(degraded)
    degraded["observation_id"] = observation_contract.recompute_observation_id(degraded)
    missing_hash = _observation("trend-000003", "2026-01-03", "trend", [8, 6, 7, 5])
    missing_hash["input_bundle_sha256"] = None
    future = _observation("trend-000004", "2027-01-01", "trend", [8, 6, 7, 5])
    labels = {
        row["observation_id"]: {
            "status": "complete", "t1_net_excess_pct": 1.0,
            "t3_net_excess_pct": 2.0, "t3_date": row["trading_date"],
        }
        for row in (good, degraded, missing_hash, future)
    }

    report = research.build_shadow_report(
        [good, degraded, missing_hash, future], labels, current_weights=CURRENT,
        asof="2026-12-31",
    )

    trend = report["lanes"]["trend"]
    assert trend["valid_rows"] == 1
    assert trend["attrition"]["catalyst_status"] == 1
    assert trend["attrition"]["input_hash"] == 1
    assert trend["attrition"]["observation_after_asof"] == 1


def test_strict_attrition_recomputes_bundle_and_observation_identity():
    row = _observation("not-a-content-hash", "2026-01-01", "trend", [8, 6, 7, 5])
    row["dimensions"]["technical"]["score"] = 9.0
    labels = {
        row["observation_id"]: {
            "status": "complete", "t1_net_excess_pct": 1.0,
            "t3_net_excess_pct": 2.0, "t3_date": "2026-01-05",
        }
    }

    report = research.build_shadow_report(
        [row], labels, current_weights=CURRENT, asof="2026-12-31",
    )

    assert report["lanes"]["trend"]["attrition"]["input_bundle_integrity"] == 1


def test_metric_uses_compounded_wealth_drawdown():
    metric = research._metric([10.0, -10.0])

    assert metric["max_drawdown_pct"] == pytest.approx(-10.0)


def test_runner_enforces_asof_and_never_changes_scoring_config(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    scoring = tmp_path / "scoring.yaml"
    scoring.write_text(
        "scoring:\n  weights:\n"
        "    trend: {technical: 0.35, sentiment: 0.10, catalyst: 0.25, deep: 0.30}\n"
        "    daban: {technical: 0.15, sentiment: 0.35, catalyst: 0.30, deep: 0.20}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "config_path", lambda name: str(scoring))
    rows = [
        _observation("trend-000001", "2026-01-01", "trend", [8, 6, 7, 5]),
        _observation("trend-000002", "2026-01-03", "trend", [8, 6, 7, 5]),
    ]
    captured = {}
    monkeypatch.setattr(runner.observations, "load_scores", lambda **kwargs: rows)

    def fake_labels(scoped, *, asof, assumed_notional):
        captured["label_rows"] = list(scoped)
        captured["label_asof"] = asof
        captured["assumed_notional"] = assumed_notional
        return {}

    def fake_report(scoped, labels, **kwargs):
        captured["report_rows"] = list(scoped)
        captured["report_asof"] = kwargs["asof"]
        return {
            "status": "insufficient_data",
            "lanes": {"trend": {"status": "insufficient_training_days"},
                      "daban": {"status": "no_valid_observations"}},
            "frozen_lanes": {},
            "version_hashes": {
                "observation_set_sha256": "a" * 64,
                "label_set_sha256": "b" * 64,
                "scoring_config_sha256": "c" * 64,
            },
        }

    monkeypatch.setattr(runner.research, "build_labels", fake_labels)
    monkeypatch.setattr(runner.research, "build_shadow_report", fake_report)
    before = scoring.read_bytes()

    result = runner.build(asof="2026-01-02", posterior_draws=128)

    assert [row["trading_date"] for row in captured["report_rows"]] == ["2026-01-01"]
    assert captured["label_asof"] == "2026-01-02"
    assert captured["report_asof"] == "2026-01-02"
    assert result["config_unchanged"] is True
    assert scoring.read_bytes() == before


def test_runner_rejects_config_that_weakens_sixty_day_gates(tmp_path, monkeypatch):
    scoring = tmp_path / "scoring.yaml"
    scoring.write_text(
        "scoring:\n  weight_shadow:\n"
        "    minimum_fit_trading_days: 59\n"
        "    minimum_unseen_oos_trading_days: 60\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "config_path", lambda name: str(scoring))

    with pytest.raises(ValueError, match="60/60"):
        runner._shadow_settings()
