import json
import os
from datetime import datetime, timezone
from pathlib import Path

import storage_retention


NOW = datetime(2026, 6, 13, 9, 0, tzinfo=timezone.utc)


def _write_snapshot(
    root: Path,
    *,
    dataset: str,
    snapshot_id: str,
    captured_at: str,
    payload_size: int = 0,
) -> Path:
    path = root / "market" / "snapshots" / "2026-01-01" / dataset / f"{snapshot_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "schema": "market_snapshot_v1",
            "snapshot_id": snapshot_id,
            "dataset": dataset,
            "trading_date": "2026-01-01",
            "captured_at": captured_at,
            "payload": {"padding": "x" * payload_size},
            "snapshot_path": str(path),
        }),
        encoding="utf-8",
    )
    Path(f"{path}.lock").touch()
    return path


def _settings(**overrides):
    settings = {
        "snapshot_input_retention_days": 7,
        "snapshot_output_retention_days": 30,
        "cron_artifact_retention_days": 30,
        "reference_protection_days": 30,
        "snapshot_min_keep_per_dataset": 1,
        "snapshot_max_total_mb": 1024,
        "gc_max_delete_files": 100,
        "snapshot_cold_archive_enabled": True,
    }
    settings.update(overrides)
    return settings


def test_gc_deletes_expired_unreferenced_but_preserves_active_reference(tmp_path):
    expired = _write_snapshot(
        tmp_path,
        dataset="candidate-discovery-input",
        snapshot_id="expired",
        captured_at="2026-05-01T09:00:00+00:00",
    )
    referenced = _write_snapshot(
        tmp_path,
        dataset="candidate-discovery-input",
        snapshot_id="referenced",
        captured_at="2026-05-02T09:00:00+00:00",
    )
    recent = _write_snapshot(
        tmp_path,
        dataset="candidate-discovery-input",
        snapshot_id="recent",
        captured_at="2026-06-12T09:00:00+00:00",
    )
    state_path = tmp_path / "agent_state" / "agent_state_latest.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"input_snapshot": {"snapshot_path": str(referenced)}}),
        encoding="utf-8",
    )
    os.utime(state_path, (NOW.timestamp(), NOW.timestamp()))

    result = storage_retention.cleanup_storage(
        state_home=tmp_path,
        settings=_settings(),
        now=NOW,
        apply=True,
    )

    assert not expired.exists()
    assert Path(f"{expired}.lock").exists()
    assert referenced.exists()
    assert recent.exists()
    assert result["deleted"]["expired_snapshots"] == 1
    assert result["protected"]["referenced_snapshots"] == 1


def test_gc_dry_run_reports_without_deleting(tmp_path):
    expired = _write_snapshot(
        tmp_path,
        dataset="auction-input",
        snapshot_id="expired",
        captured_at="2026-05-01T09:00:00+00:00",
    )

    result = storage_retention.cleanup_storage(
        state_home=tmp_path,
        settings=_settings(snapshot_min_keep_per_dataset=0),
        now=NOW,
        apply=False,
    )

    assert expired.exists()
    assert result["mode"] == "dry_run"
    assert result["deleted"]["expired_snapshots"] == 1
    assert result["reclaimed_bytes"] > 0


def test_gc_enforces_size_cap_but_keeps_minimum_per_dataset(tmp_path):
    oldest = _write_snapshot(
        tmp_path,
        dataset="global-preopen",
        snapshot_id="oldest",
        captured_at="2026-06-10T09:00:00+00:00",
        payload_size=700_000,
    )
    middle = _write_snapshot(
        tmp_path,
        dataset="global-preopen",
        snapshot_id="middle",
        captured_at="2026-06-11T09:00:00+00:00",
        payload_size=700_000,
    )
    newest = _write_snapshot(
        tmp_path,
        dataset="global-preopen",
        snapshot_id="newest",
        captured_at="2026-06-12T09:00:00+00:00",
        payload_size=700_000,
    )

    result = storage_retention.cleanup_storage(
        state_home=tmp_path,
        settings=_settings(
            snapshot_output_retention_days=365,
            snapshot_max_total_mb=0.8,
        ),
        now=NOW,
        apply=True,
    )

    assert not oldest.exists()
    assert not middle.exists()
    assert newest.exists()
    assert result["deleted"]["size_cap_snapshots"] == 2
    assert result["remaining_snapshot_bytes"] <= 0.8 * 1024 * 1024


def test_gc_removes_old_cron_artifacts_but_keeps_job_runs(tmp_path):
    artifact = tmp_path / "cron" / "output" / "candidate-discovery" / "old.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    job_runs = tmp_path / "cron" / "output" / "job_runs.json"
    job_runs.write_text("[]", encoding="utf-8")
    old_timestamp = datetime(2026, 4, 1, tzinfo=timezone.utc).timestamp()
    os.utime(artifact, (old_timestamp, old_timestamp))
    os.utime(job_runs, (old_timestamp, old_timestamp))
    Path(f"{artifact}.bak").touch()
    Path(f"{artifact}.lock").touch()

    result = storage_retention.cleanup_storage(
        state_home=tmp_path,
        settings=_settings(snapshot_min_keep_per_dataset=0),
        now=NOW,
        apply=True,
    )

    assert not artifact.exists()
    assert not Path(f"{artifact}.bak").exists()
    assert Path(f"{artifact}.lock").exists()
    assert job_runs.exists()
    assert result["deleted"]["cron_artifacts"] == 1


def test_gc_reports_but_never_deletes_invalid_snapshot(tmp_path):
    invalid = (
        tmp_path
        / "market"
        / "snapshots"
        / "2026-01-01"
        / "candidate-discovery-input"
        / "broken.json"
    )
    invalid.parent.mkdir(parents=True)
    invalid.write_text("{not-json" + ("x" * 10_000), encoding="utf-8")

    result = storage_retention.cleanup_storage(
        state_home=tmp_path,
        settings=_settings(
            snapshot_min_keep_per_dataset=0,
            snapshot_max_total_mb=0.001,
        ),
        now=NOW,
        apply=True,
    )

    assert invalid.exists()
    assert result["scanned"]["invalid_snapshots"] == 1
    assert result["invalid_snapshot_paths"] == [str(invalid)]
    assert result["capacity_satisfied"] is False


def test_gc_archives_expired_snapshot_before_deleting_it(tmp_path):
    import gzip

    expired = _write_snapshot(
        tmp_path,
        dataset="candidate-discovery-input",
        snapshot_id="expired",
        captured_at="2026-05-01T09:00:00+00:00",
    )
    original_bytes = expired.read_bytes()

    result = storage_retention.cleanup_storage(
        state_home=tmp_path,
        settings=_settings(snapshot_min_keep_per_dataset=0),
        now=NOW,
        apply=True,
    )

    assert not expired.exists()
    archived_path = (
        storage_retention.archive_dir(tmp_path)
        / "2026-01-01"
        / "candidate-discovery-input"
        / "expired.json.gz"
    )
    assert archived_path.exists()
    with gzip.open(archived_path, "rb") as handle:
        assert handle.read() == original_bytes
    assert result["archived"]["count"] == 1
    assert result["archived"]["bytes"] > 0
    assert result["archived"]["enabled"] is True


def test_gc_dry_run_reports_would_archive_without_writing_anything(tmp_path):
    _write_snapshot(
        tmp_path,
        dataset="auction-input",
        snapshot_id="expired",
        captured_at="2026-05-01T09:00:00+00:00",
    )

    result = storage_retention.cleanup_storage(
        state_home=tmp_path,
        settings=_settings(snapshot_min_keep_per_dataset=0),
        now=NOW,
        apply=False,
    )

    assert result["archived"]["count"] == 1
    assert result["archived"]["bytes"] is None
    assert not storage_retention.archive_dir(tmp_path).exists()


def test_gc_skips_archiving_when_disabled_via_config(tmp_path):
    expired = _write_snapshot(
        tmp_path,
        dataset="candidate-discovery-input",
        snapshot_id="expired",
        captured_at="2026-05-01T09:00:00+00:00",
    )

    result = storage_retention.cleanup_storage(
        state_home=tmp_path,
        settings=_settings(
            snapshot_min_keep_per_dataset=0,
            snapshot_cold_archive_enabled=False,
        ),
        now=NOW,
        apply=True,
    )

    assert not expired.exists()
    assert result["archived"]["enabled"] is False
    assert result["archived"]["count"] == 0
    assert not storage_retention.archive_dir(tmp_path).exists()


def test_gc_does_not_archive_cron_artifacts(tmp_path):
    artifact = tmp_path / "cron" / "output" / "candidate-discovery" / "old.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    job_runs = tmp_path / "cron" / "output" / "job_runs.json"
    job_runs.write_text("[]", encoding="utf-8")
    old_timestamp = datetime(2026, 4, 1, tzinfo=timezone.utc).timestamp()
    os.utime(artifact, (old_timestamp, old_timestamp))
    os.utime(job_runs, (old_timestamp, old_timestamp))

    result = storage_retention.cleanup_storage(
        state_home=tmp_path,
        settings=_settings(snapshot_min_keep_per_dataset=0),
        now=NOW,
        apply=True,
    )

    assert not artifact.exists()
    assert result["deleted"]["cron_artifacts"] == 1
    assert result["archived"]["count"] == 0
