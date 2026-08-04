import copy
import json
import subprocess
import sys
from pathlib import Path

import fundamentals_snapshot as fs
import pytest
from market_snapshot import PointInTimeViolation


REPO_ROOT = Path(__file__).resolve().parents[1]


def _payload(asof="2026-03-31", *, restated=False):
    return {
        "name": "示例",
        "asof": asof,
        "source": {"provider": "fixture", "version": "v1"},
        "units": {"scale": "CNY", "shares": "万股"},
        "restated": restated,
        "metrics": {"roe": None, "revenue": "12.5", "bad": float("nan")},
        "valuation": {"pe_ttm": 20},
        "periods": [{"period": "2026Q1", "revenue": 10, "profit": None}],
        "quality": {"status": "audited"},
    }


def _timing(day, *, hour=18):
    prefix = f"{day}T{hour:02d}:00:"
    return {
        "event_time": prefix + "00+08:00",
        "published_at": prefix + "01+08:00",
        "available_at": prefix + "02+08:00",
        "captured_at": prefix + "03+08:00",
        "watermark": {
            "coverage_asof": prefix + "00+08:00",
            "provider_published_at": prefix + "01+08:00",
            "complete": True,
            "provider_sequence": f"{day}-fixture-1",
        },
        "sealed_at": prefix + "04+08:00",
    }


def _write(day, payload=None, *, batch_id=None, **timing_overrides):
    timing = _timing(day)
    timing.update(timing_overrides)
    return fs.write_fundamental_snapshot(
        "600519",
        payload or _payload(),
        trading_date=day,
        batch_id=batch_id or f"fundamentals-{day}",
        producer="fixture",
        producer_version="v1",
        **timing,
    )


def test_snapshot_preserves_missing_as_null_and_reads_by_decision_cutoff(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    written = _write("2026-04-30")

    assert written["payload"]["metrics"]["roe"] is None
    assert written["payload"]["metrics"]["bad"] is None
    assert written["payload"]["periods"][0]["profit"] is None
    loaded = fs.read_latest_fundamentals(
        "600519", decision_cutoff="2026-04-30T18:00:02+08:00",
    )
    assert loaded["evidence_status"] == "fresh"
    assert loaded["metrics"]["revenue"] == 12.5
    assert loaded["available_at"] == "2026-04-30T18:00:02+08:00"


def test_index_keeps_versions_and_first_day_remains_readable_after_second_write(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    first = _write("2026-04-30", batch_id="b1")
    second_payload = _payload("2026-04-30")
    second_payload["periods"][0]["period"] = "2026Q2"
    second_payload["metrics"]["revenue"] = 13
    second = _write("2026-05-02", second_payload, batch_id="b2")

    first_day = fs.read_latest_fundamentals(
        "600519", decision_cutoff="2026-04-30T23:59:59+08:00",
    )
    latest = fs.read_latest_fundamentals(
        "600519", decision_cutoff="2026-05-02T23:59:59+08:00",
    )
    index = json.loads(Path(fs.index_file()).read_text(encoding="utf-8"))

    assert first_day["snapshot_ref"] == first["snapshot_id"]
    assert first_day["metrics"]["revenue"] == 12.5
    assert latest["snapshot_ref"] == second["snapshot_id"]
    assert latest["metrics"]["revenue"] == 13.0
    assert [item["snapshot_path"] for item in index["entries"]["600519"]] == [
        first["snapshot_path"],
        second["snapshot_path"],
    ]


def test_later_published_and_restated_report_is_invisible_to_early_replay(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    original = _write("2026-04-30", batch_id="original")
    restated = _payload(restated=True)
    restated["metrics"]["revenue"] = 99
    amended = _write("2026-05-02", restated, batch_id="restated")

    early = fs.read_latest_fundamentals(
        "600519", decision_cutoff="2026-05-01T12:00:00+08:00",
    )
    after_publication = fs.read_latest_fundamentals(
        "600519", decision_cutoff="2026-05-02T18:00:02+08:00",
    )

    assert early["snapshot_ref"] == original["snapshot_id"]
    assert early["restated"] is False
    assert early["metrics"]["revenue"] == 12.5
    assert after_publication["snapshot_ref"] == amended["snapshot_id"]
    assert after_publication["restated"] is True
    assert after_publication["metrics"]["revenue"] == 99.0


def test_restatement_cannot_backdate_availability(monkeypatch, tmp_path):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    _write("2026-04-30", batch_id="original")
    restated = _payload(restated=True)
    timing = _timing("2026-05-02")
    timing["available_at"] = "2026-04-30T17:59:59+08:00"
    timing["published_at"] = "2026-04-30T17:59:58+08:00"
    timing["event_time"] = "2026-04-30T17:59:57+08:00"
    timing["watermark"] = {
        "coverage_asof": timing["event_time"],
        "provider_published_at": timing["published_at"],
        "complete": True,
    }

    with pytest.raises(ValueError, match="restatement_availability_not_newer"):
        fs.write_fundamental_snapshot(
            "600519",
            restated,
            trading_date="2026-05-02",
            batch_id="backdated-restatement",
            producer="fixture",
            producer_version="v1",
            **timing,
        )


def test_report_published_after_cutoff_is_not_visible(monkeypatch, tmp_path):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    _write("2026-05-02")

    assert fs.read_latest_fundamentals(
        "600519", decision_cutoff="2026-05-02T18:00:01+08:00",
    ) is None


def test_trading_date_compatibility_means_market_close_not_day_end(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    _write("2026-05-02")

    assert fs.read_latest_fundamentals(
        "600519", trading_date="2026-05-02",
    ) is None
    next_day = fs.read_latest_fundamentals(
        "600519", trading_date="2026-05-03",
    )
    assert next_day["available_at"] == "2026-05-02T18:00:02+08:00"
    assert next_day["stale"] is False
    assert next_day["evidence_status"] == "fresh"
    assert 0 < next_day["age_days"] < 1


def test_fundamentals_become_stale_only_after_explicit_freshness_window(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    _write("2026-05-02")

    expired = fs.read_latest_fundamentals(
        "600519",
        decision_cutoff="2026-05-10T18:00:02+08:00",
    )
    extended = fs.read_latest_fundamentals(
        "600519",
        decision_cutoff="2026-05-10T18:00:02+08:00",
        max_age_days=8,
    )

    assert expired["age_days"] == 8.0
    assert expired["max_age_days"] == 7.0
    assert expired["stale"] is True
    assert expired["evidence_status"] == "stale"
    assert extended["stale"] is False
    assert extended["evidence_status"] == "fresh"


@pytest.mark.parametrize("max_age_days", [-1, True, float("nan"), "seven"])
def test_invalid_freshness_window_fails_closed(
    monkeypatch, tmp_path, max_age_days,
):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    _write("2026-05-02")

    with pytest.raises(ValueError, match="max_age_days_invalid"):
        fs.read_latest_fundamentals(
            "600519",
            decision_cutoff="2026-05-03T15:00:00+08:00",
            max_age_days=max_age_days,
        )


def test_write_requires_real_point_in_time_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    with pytest.raises(TypeError):
        fs.write_fundamental_snapshot(
            "600519",
            _payload(),
            trading_date="2026-04-30",
            batch_id="missing-pit",
            producer="fixture",
            producer_version="v1",
        )


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        (
            {"available_at": "2026-05-01T00:00:00+08:00"},
            "fundamental_time_order_invalid",
        ),
        (
            {"event_time": "2026-04-30T18:00:00"},
            "event_time_timezone_missing",
        ),
        (
            {"published_at": "2026-04-30T18:00:03+08:00"},
            "fundamental_time_order_invalid",
        ),
        (
            {
                "watermark": {
                    "coverage_asof": "2026-04-30T18:00:00+08:00",
                    "provider_published_at": "2026-04-30T17:59:59+08:00",
                    "complete": True,
                },
            },
            "watermark_publication_mismatch",
        ),
        (
            {
                "sealed_at": "2026-04-30T18:00:02+08:00",
            },
            "fundamental_time_order_invalid",
        ),
    ],
)
def test_invalid_or_future_times_fail_closed(
    monkeypatch, tmp_path, override, reason,
):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    with pytest.raises((ValueError, PointInTimeViolation), match=reason):
        _write("2026-04-30", **override)


def test_duplicate_version_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    _write("2026-04-30")
    equivalent = _timing("2026-04-30")
    for field in (
        "event_time",
        "published_at",
        "available_at",
        "captured_at",
        "sealed_at",
    ):
        equivalent[field] = equivalent[field].replace("+08:00", ".000+08:00")
    equivalent["watermark"] = {
        "coverage_asof": equivalent["event_time"],
        "provider_published_at": equivalent["published_at"],
        "complete": True,
    }

    with pytest.raises(ValueError, match="duplicate_fundamental_version"):
        fs.write_fundamental_snapshot(
            "600519",
            _payload(),
            trading_date="2026-04-30",
            batch_id="equivalent-duplicate",
            producer="fixture",
            producer_version="v1",
            **equivalent,
        )


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda value: value["metrics"].update({"revenue": "unknown"}), "metrics_revenue_non_numeric"),
        (lambda value: value["valuation"].update({"pe_ttm": True}), "valuation_pe_ttm_non_numeric"),
        (lambda value: value.update({"restated": "false"}), "restated_invalid"),
        (lambda value: value.update({"name": 123}), "name_invalid"),
        (lambda value: value["periods"].append({"period": 123, "revenue": 1}), "period_1_period_invalid"),
        (lambda value: value["source"].update({"provider": 123}), "source_provider_invalid"),
    ],
)
def test_non_numeric_and_metadata_fields_are_strict(
    monkeypatch, tmp_path, mutate, reason,
):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    payload = copy.deepcopy(_payload())
    mutate(payload)

    with pytest.raises(ValueError, match=reason):
        _write("2026-04-30", payload)


def test_read_cutoff_must_be_aware_and_valid(monkeypatch, tmp_path):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    _write("2026-04-30")

    with pytest.raises(ValueError, match="decision_cutoff_timezone_missing"):
        fs.read_latest_fundamentals(
            "600519", decision_cutoff="2026-04-30T23:59:59",
        )
    with pytest.raises(ValueError, match="decision_cutoff_invalid"):
        fs.read_latest_fundamentals("600519", decision_cutoff="not-a-time")


def test_invalid_trading_date_is_not_truncated(monkeypatch, tmp_path):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    with pytest.raises(ValueError, match="trading_date_invalid"):
        _write("2026-04-30-forged")


def test_cli_requires_point_in_time_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    input_file = tmp_path / "facts.json"
    input_file.write_text(
        json.dumps({"code": "600519", **_payload()}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "fundamentals_snapshot.py"),
            "--input",
            str(input_file),
            "--trading-date",
            "2026-04-30",
            "--batch-id",
            "cli-missing-pit",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "event_time is required" in result.stderr


def test_cli_accepts_explicit_point_in_time_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    input_file = tmp_path / "facts.json"
    record = {"code": "600519", **_payload(), **_timing("2026-04-30")}
    input_file.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "fundamentals_snapshot.py"),
            "--input",
            str(input_file),
            "--trading-date",
            "2026-04-30",
            "--batch-id",
            "cli-strict-pit",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["snapshots"][0]["point_in_time"]["available_time"] == (
        "2026-04-30T18:00:02+08:00"
    )
