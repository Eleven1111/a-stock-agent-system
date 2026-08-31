import json
import os
from datetime import datetime, timezone
from pathlib import Path

import gc_index
import pytest
import storage_retention


NOW = datetime(2026, 6, 13, 9, 0, tzinfo=timezone.utc)


def _backdate(path: Path, when: datetime) -> None:
    """Give a file a settled mtime.

    The fact cache refuses to memoise anything written within
    ``gc_index.SETTLE_SECONDS`` of the wall clock, so a file a test just created
    is deliberately uncacheable. Real snapshots are minutes-to-days old by the
    time the 17:20 GC sees them; backdating restores that shape.
    """
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))


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
    _backdate(path, datetime.fromisoformat(captured_at))
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


# --------------------------------------------------------------------------
# Fact-cache behaviour.
#
# The cache exists to stop the GC re-parsing the whole snapshot corpus every
# day (2.4 GB / 33.7 s against a 120 s budget on 2026-08-05). Every test below
# asserts what the GC *does* — files read, snapshots kept, plan produced — not
# that a cache file appeared. A cache that is written but never consulted, or
# consulted after the file changed, would pass a config-shaped test and fail
# every one of these.
# --------------------------------------------------------------------------


def _counting_reader(monkeypatch):
    """Count real file reads so 'the cache is used' is an observation, not a claim."""
    reads: list[str] = []
    original = storage_retention._read_json

    def _spy(path):
        reads.append(str(path))
        return original(path)

    monkeypatch.setattr(storage_retention, "_read_json", _spy)
    return reads


def _corpus(root: Path) -> dict[str, Path]:
    expired = _write_snapshot(
        root,
        dataset="candidate-discovery-input",
        snapshot_id="expired",
        captured_at="2026-05-01T09:00:00+00:00",
    )
    referenced = _write_snapshot(
        root,
        dataset="candidate-discovery-input",
        snapshot_id="referenced",
        captured_at="2026-05-02T09:00:00+00:00",
    )
    recent = _write_snapshot(
        root,
        dataset="candidate-discovery-input",
        snapshot_id="recent",
        captured_at="2026-06-12T09:00:00+00:00",
    )
    broken = root / "market" / "snapshots" / "2026-01-01" / "misc" / "broken.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{not-json", encoding="utf-8")
    _backdate(broken, NOW)
    state_path = root / "agent_state" / "agent_state_latest.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"input_snapshot": {"snapshot_path": str(referenced)}}),
        encoding="utf-8",
    )
    _backdate(state_path, NOW)
    return {
        "expired": expired,
        "referenced": referenced,
        "recent": recent,
        "broken": broken,
        "state": state_path,
    }


def _plan(root: Path, **overrides):
    return storage_retention.cleanup_storage(
        state_home=root,
        settings=_settings(**overrides.pop("settings", {})),
        now=NOW,
        apply=False,
        **overrides,
    )


def _comparable(result):
    return {key: value for key, value in result.items() if key != "index"}


def test_second_run_reuses_facts_instead_of_rereading_the_corpus(tmp_path, monkeypatch):
    _corpus(tmp_path)

    first = _plan(tmp_path)
    reads = _counting_reader(monkeypatch)
    second = _plan(tmp_path)

    assert first["index"]["read_files"] > 0
    assert second["index"]["read_files"] == 0
    assert second["index"]["reused_facts"] == first["index"]["read_files"]
    assert reads == []


def test_the_cached_plan_is_identical_to_the_freshly_derived_one(tmp_path):
    _corpus(tmp_path)

    cold = _plan(tmp_path)
    warm = _plan(tmp_path)
    uncached = _plan(tmp_path, use_index=False)

    assert _comparable(warm) == _comparable(cold)
    assert _comparable(uncached) == _comparable(cold)


def test_a_rewrite_of_the_same_length_is_not_served_from_the_cache(tmp_path):
    """The nastiest staleness case: size unchanged, content different.

    Datasets ending in ``-input`` expire after 7 days here, everything else
    after 365, so mistaking one for the other flips this file between deleted
    and kept — while its size on disk never changes.
    """
    policy = {"snapshot_min_keep_per_dataset": 0, "snapshot_output_retention_days": 365}
    path = _write_snapshot(
        tmp_path,
        dataset="candidate-discovery-input",
        snapshot_id="rewritten",
        captured_at="2026-05-01T09:00:00+00:00",
    )
    original_size = path.stat().st_size
    assert _plan(tmp_path, settings=policy)["deleted"]["expired_snapshots"] == 1

    record = json.loads(path.read_text(encoding="utf-8"))
    record["dataset"] = "candidate-discovery-outpu"  # same length as "-input"
    path.write_text(json.dumps(record), encoding="utf-8")
    _backdate(path, NOW)
    assert path.stat().st_size == original_size, "the test needs a same-size rewrite"

    after = _plan(tmp_path, settings=policy)

    assert after["deleted"]["expired_snapshots"] == 0


def test_a_file_written_moments_ago_is_never_memoised(tmp_path, monkeypatch):
    """mtime granularity is a filesystem promise, not a guarantee.

    A file whose mtime is within the settle window may still be rewritten at the
    same apparent mtime and size, so its facts must be re-derived every run.
    """
    _write_snapshot(
        tmp_path,
        dataset="global-preopen",
        snapshot_id="fresh",
        captured_at="2026-06-12T09:00:00+00:00",
    )
    fresh = tmp_path / "market" / "snapshots" / "2026-01-01" / "global-preopen" / "fresh.json"
    os.utime(fresh, None)  # now

    _plan(tmp_path)
    reads = _counting_reader(monkeypatch)
    second = _plan(tmp_path)

    assert str(fresh) in reads
    assert second["index"]["read_files"] == 1


def test_referenced_snapshot_stays_protected_on_the_cached_run(tmp_path):
    files = _corpus(tmp_path)

    _plan(tmp_path)
    warm = storage_retention.cleanup_storage(
        state_home=tmp_path, settings=_settings(), now=NOW, apply=True
    )

    assert files["referenced"].exists()
    assert warm["protected"]["referenced_snapshots"] == 1
    assert not files["expired"].exists()


def test_a_corrupt_index_falls_back_to_reading_every_file(tmp_path, monkeypatch):
    _corpus(tmp_path)
    expected = _comparable(_plan(tmp_path))
    index = gc_index.index_path(tmp_path)
    index.write_text("{ this is not json", encoding="utf-8")

    reads = _counting_reader(monkeypatch)
    result = _plan(tmp_path)

    assert _comparable(result) == expected
    assert reads, "a corrupt index must degrade to slow-and-correct, not to trusting it"


def test_an_index_from_a_future_version_is_ignored(tmp_path):
    _corpus(tmp_path)
    expected = _comparable(_plan(tmp_path))
    index = gc_index.index_path(tmp_path)
    payload = json.loads(index.read_text(encoding="utf-8"))
    payload["version"] = gc_index.INDEX_VERSION + 1
    index.write_text(json.dumps(payload), encoding="utf-8")

    result = _plan(tmp_path)

    assert _comparable(result) == expected
    assert result["index"]["reused_facts"] == 0


def test_vanished_files_do_not_accumulate_in_the_index(tmp_path):
    files = _corpus(tmp_path)
    storage_retention.cleanup_storage(
        state_home=tmp_path, settings=_settings(), now=NOW, apply=True
    )
    assert not files["expired"].exists()

    _plan(tmp_path)
    sections = json.loads(
        gc_index.index_path(tmp_path).read_text(encoding="utf-8")
    )["sections"]

    assert str(files["expired"]) not in sections["snapshots"]
    assert str(files["recent"]) in sections["snapshots"]


def test_the_index_is_neither_scanned_as_a_snapshot_nor_charged_to_the_size_cap(tmp_path):
    _corpus(tmp_path)
    _plan(tmp_path)
    second = _plan(tmp_path)
    index = gc_index.index_path(tmp_path)

    assert index.exists()
    assert not storage_retention._is_within(index, tmp_path / "market" / "snapshots")
    assert second["scanned"]["invalid_snapshots"] == 1  # only the deliberate broken.json
    assert second["invalid_snapshot_paths"] == [str(tmp_path / "market" / "snapshots"
                                                    / "2026-01-01" / "misc" / "broken.json")]
    assert second["scanned"]["reference_files"] == 1  # the state file, not the index


def test_gc_still_runs_when_the_index_cannot_be_written(tmp_path):
    """Losing the cache costs one slow run. It must never cost the GC itself."""
    files = _corpus(tmp_path)
    market = tmp_path / "market"
    market.chmod(0o555)
    try:
        result = storage_retention.cleanup_storage(
            state_home=tmp_path, settings=_settings(), now=NOW, apply=True
        )
    finally:
        market.chmod(0o755)

    assert result["index"]["saved"] is False
    assert not gc_index.index_path(tmp_path).exists()
    assert result["deleted"]["expired_snapshots"] == 1
    assert files["referenced"].exists()
    assert not files["expired"].exists()


def test_a_truncated_ledger_keeps_the_references_it_did_yield(tmp_path):
    """A corrupt tail must not silently un-protect what the good lines named."""
    referenced = _write_snapshot(
        tmp_path,
        dataset="candidate-discovery-input",
        snapshot_id="referenced",
        captured_at="2026-05-01T09:00:00+00:00",
    )
    ledger = tmp_path / "signals" / "signal_ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps({"snapshot_path": str(referenced)}) + "\n{ truncated",
        encoding="utf-8",
    )
    _backdate(ledger, NOW)

    result = _plan(tmp_path, settings={"snapshot_min_keep_per_dataset": 0})

    assert result["protected"]["referenced_snapshots"] == 1
    assert result["deleted"]["expired_snapshots"] == 0
    # A partially read file is never memoised, so the next run tries again.
    sections = json.loads(
        gc_index.index_path(tmp_path).read_text(encoding="utf-8")
    )["sections"]
    assert str(ledger) not in sections.get("references", {})


def test_large_json_reference_scan_does_not_build_the_whole_object(tmp_path, monkeypatch):
    referenced = _write_snapshot(
        tmp_path,
        dataset="large-state",
        snapshot_id="referenced-large",
        captured_at="2026-01-01T00:00:00+00:00",
    )
    state = tmp_path / "state" / "large.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        '{"padding":"' + ("x" * 2_000_000) + '","snapshot_path":'
        + json.dumps(str(referenced)) + "}",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        storage_retention,
        "_read_json",
        lambda path: pytest.fail("reference scan must stay streaming"),
    )
    found = set()

    storage_retention._read_references_into(
        state, found, state_home=tmp_path
    )

    assert referenced.resolve() in found
