"""A D-day sector cross-section has to survive the next two sessions.

The daily job writes only ``*_latest.json`` and the cron artifact is capped at
1500 characters, so before this archive existed the full cross-section stopped
being retrievable once ``latest`` was overwritten twice.
"""

from __future__ import annotations

import json

import pytest

from skills.common import sector_daily_archive as archive
from skills.common.sector_rotation_pools import (
    AVAILABLE_WEIGHT_SHARE,
    COMPONENT_DEPENDENCIES,
    build_sector_rotation_pools,
    sector_pool,
)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))


def _artifacts(marker: str):
    return {
        name: {"schema": name, "asof": "2026-09-04", "marker": marker,
               "sectors": [{"sector": "银行", "score": 55.0}]}
        for name in archive.ARTIFACT_NAMES
    }


def test_a_full_day_can_be_restored_after_latest_is_overwritten_twice():
    archive.archive_day("2026-09-04", _artifacts("day-one"), inputs={"membership_codes": 5000})
    archive.archive_day("2026-09-07", _artifacts("day-two"))
    archive.archive_day("2026-09-08", _artifacts("day-three"))

    restored = archive.restore_day("2026-09-04")

    assert restored["status"] == "ok"
    assert sorted(restored["artifacts"]) == sorted(archive.ARTIFACT_NAMES)
    assert restored["artifacts"]["sector_rotation_pools"]["marker"] == "day-one"
    assert restored["inputs"]["membership_codes"] == 5000


def test_archiving_the_same_day_twice_adds_no_version():
    first = archive.archive_day("2026-09-04", _artifacts("same"))
    second = archive.archive_day("2026-09-04", _artifacts("same"))

    assert first["row_sha256"] == second["row_sha256"]
    assert second["newly_written"] == []
    assert len(archive.versions_for("2026-09-04")) == 1


def test_a_recomputed_day_becomes_a_new_version_and_never_overwrites_the_old_one():
    original = archive.archive_day("2026-09-04", _artifacts("original"))
    recomputed = archive.archive_day("2026-09-04", _artifacts("recomputed"))

    versions = archive.versions_for("2026-09-04")
    assert len(versions) == 2
    assert original["row_sha256"] != recomputed["row_sha256"]

    old = archive.restore_day("2026-09-04", row_sha256=original["row_sha256"])
    new = archive.restore_day("2026-09-04")
    assert old["artifacts"]["sector_crowding"]["marker"] == "original"
    assert new["artifacts"]["sector_crowding"]["marker"] == "recomputed"
    assert old["version_count"] == 2


def test_restoring_an_unarchived_day_is_unavailable_not_empty_success():
    assert archive.restore_day("2026-01-01") == {
        "status": "unavailable",
        "reason": "no_archived_version",
        "trading_date": "2026-01-01",
    }
    archive.archive_day("2026-09-04", _artifacts("x"))
    assert archive.restore_day("2026-09-04", row_sha256="deadbeef")["status"] == "unavailable"


def test_a_deleted_content_file_is_reported_partial_rather_than_silently_dropped():
    row = archive.archive_day("2026-09-04", _artifacts("x"))
    target = row["paths"]["sector_fake_breakout"]
    archive.Path(target).unlink()

    restored = archive.restore_day("2026-09-04")
    assert restored["status"] == "partial"
    assert restored["missing"] == ["sector_fake_breakout"]


def test_an_unknown_artifact_name_is_refused():
    with pytest.raises(ValueError, match="unknown_sector_artifact"):
        archive.archive_day("2026-09-04", {"something_else": {}})


def test_the_index_stays_readable_json_lines():
    archive.archive_day("2026-09-04", _artifacts("x"))
    lines = (archive.archive_root() / archive.INDEX_NAME).read_text(encoding="utf-8")

    rows = [json.loads(line) for line in lines.splitlines() if line.strip()]
    assert rows[0]["schema"] == "sector_daily_archive_v1"
    assert rows[0]["trading_date"] == "2026-09-04"


def test_observed_coverage_falls_when_components_are_actually_missing():
    complete = sector_pool(
        "银行", rs_slope=0.001, breadth=0.6, crowding_score=40.0,
        crowding_state="NORMAL", fake_risk=20.0,
    )
    partial = sector_pool(
        "军工", rs_slope=None, breadth=0.6, crowding_score=None,
        crowding_state="NORMAL", fake_risk=None,
    )

    assert complete["observed_weight_share"] == AVAILABLE_WEIGHT_SHARE == 0.41
    assert complete["missing_weight_share"] == pytest.approx(0.59)
    # Only breadth survived: claiming 41% coverage here understates the gap.
    assert partial["observed_weight_share"] == pytest.approx(0.11)
    assert partial["missing_weight_share"] == pytest.approx(0.89)
    assert partial["available_weight_share_ceiling"] == 0.41


def test_the_pools_payload_reports_realised_coverage_and_shared_inputs():
    payload = build_sector_rotation_pools(
        ["银行", "军工"],
        asof="2026-09-04",
        price_factors={
            "银行": {"rs_slope_20d": 0.001, "breadth_ma20": 0.6},
            "军工": {"breadth_ma20": 0.6},
        },
        crowding={"银行": {"score": 40.0, "state": "NORMAL"}},
        fake_breakout={"银行": {"risk": 20.0}},
    )

    coverage = payload["observed_weight_share"]
    assert coverage["ceiling"] == 0.41
    assert coverage["minimum"] == pytest.approx(0.11)
    assert coverage["at_ceiling_sectors"] == 1
    assert coverage["mean"] < 0.41
    # The "four-fold resonance" is not four independent confirmations.
    assert payload["component_dependencies"] == COMPONENT_DEPENDENCIES
    assert "breadth" in payload["component_dependencies"]["anti_fake_breakout"]
    assert "anti_crowding" in payload["component_dependencies"]["anti_fake_breakout"]


def test_same_day_cycle_memory_becomes_a_regime_label(monkeypatch):
    from scripts import sector_crowding_daily as daily

    monkeypatch.setattr(
        daily.market_cycle_state, "read_cycle_memory",
        lambda: {"asof": "2026-09-04", "state": "S3"},
    )
    regime = daily._same_day_regime("2026-09-04")

    assert regime["status"] == "ok"
    assert regime["labels"] == ["S3"]
    assert regime["source"] == "market_cycle_state.read_cycle_memory"
    # No market-level crowding producer exists; a fourth classifier is not the fix.
    assert regime["market_crowding_score"] is None
    assert regime["market_crowding_reason"] == "no_market_level_crowding_producer"


def test_a_stale_cycle_memory_is_unavailable_rather_than_folded_in(monkeypatch):
    from scripts import sector_crowding_daily as daily

    monkeypatch.setattr(
        daily.market_cycle_state, "read_cycle_memory",
        lambda: {"asof": "2026-08-28", "state": "S3"},
    )
    regime = daily._same_day_regime("2026-09-04")

    assert regime["status"] == "unavailable"
    assert regime["reason"] == "cycle_memory_cutoff_mismatch"
    assert regime["labels"] == []
    assert regime["memory_asof"] == "2026-08-28"


def test_a_missing_or_stateless_cycle_memory_is_unavailable(monkeypatch):
    from scripts import sector_crowding_daily as daily

    monkeypatch.setattr(daily.market_cycle_state, "read_cycle_memory", lambda: None)
    assert daily._same_day_regime("2026-09-04")["reason"] == "cycle_memory_missing"

    monkeypatch.setattr(
        daily.market_cycle_state, "read_cycle_memory", lambda: {"asof": "2026-09-04"}
    )
    assert daily._same_day_regime("2026-09-04")["reason"] == "cycle_memory_state_missing"


def test_regime_labels_reach_the_pools_payload():
    with_label = build_sector_rotation_pools(
        ["银行"], asof="2026-09-04",
        price_factors={"银行": {"rs_slope_20d": 0.001, "breadth_ma20": 0.6}},
        crowding={"银行": {"score": 40.0, "state": "NORMAL"}},
        fake_breakout={"银行": {"risk": 20.0}},
        regime_labels=["S3"],
    )
    without = build_sector_rotation_pools(
        ["银行"], asof="2026-09-04",
        price_factors={"银行": {"rs_slope_20d": 0.001, "breadth_ma20": 0.6}},
        crowding={"银行": {"score": 40.0, "state": "NORMAL"}},
        fake_breakout={"银行": {"risk": 20.0}},
    )

    assert with_label["regime"]["labels"] == ["S3"]
    assert without["regime"]["labels"] == []
    # Market crowding stays unavailable in both: no producer exists yet.
    assert with_label["regime"]["market_crowding"] == "unavailable"
