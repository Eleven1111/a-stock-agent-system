import json

import four_dim_score_log as fdl


def _batch(*items):
    return {"schema": "four_dim_batch_v1", "results": list(items)}


def _scored(code, tech, sent, cat, deep, *, lane="daban", grade="A", weighted=7.0):
    return {
        "code": code,
        "strategy_lane": lane,
        "grade": grade,
        "weighted": weighted,
        "input_fingerprint_sha256": "f" * 64,
        "weight_values": {"technical": 0.15, "sentiment": 0.35, "catalyst": 0.30, "deep": 0.20},
        "effective_weight_values": {"technical": 0.15, "sentiment": 0.35, "catalyst": 0.30, "deep": 0.20},
        "dimension_provenance": {
            dim: {"status": "available", "source": "fixture", "asof": "2026-07-02"}
            for dim in fdl.DIMENSIONS
        },
        "scores": {
            "technical": {"score": tech},
            "sentiment": {"score": sent},
            "catalyst": {"score": cat},
            "deep": {"score": deep},
        },
    }


def test_record_scores_writes_one_row_per_scored_stock(tmp_path):
    log = tmp_path / "four_dim_score_log.jsonl"
    snapshot = tmp_path / "candidate_pool.json"
    snapshot.write_text('{"asof":"2026-07-02"}', encoding="utf-8")
    batch = _batch(
        _scored("sh600000", 8, 6, 7, 5),
        _scored("sz000001", 4, 3, 2, 9),
    )

    written = fdl.record_scores(
        batch,
        asof="2026-07-02",
        path=str(log),
        input_snapshot_path=str(snapshot),
    )

    assert written == 2
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["code"] == "600000"  # prefix stripped
    assert rows[0]["trading_date"] == "2026-07-02"
    assert rows[0]["schema"] == "four_dim_observation_v2"
    assert rows[0]["dimensions"]["technical"]["score"] == 8
    assert rows[0]["point_in_time"]["status"] == "complete"
    assert len(rows[0]["input_snapshot"]["sha256"]) == 64
    assert len(rows[0]["versions"]["scorer_sha256"]) == 64
    assert len(rows[0]["versions"]["config_sha256"]) == 64
    assert rows[0]["live_effect"] == "none"
    assert rows[1]["code"] == "000001"
    assert rows[1]["dimensions"]["deep"]["score"] == 9


def test_record_scores_skips_failed_and_scoreless_items(tmp_path):
    log = tmp_path / "four_dim_score_log.jsonl"
    batch = _batch(
        {"code": "600000", "status": "failed", "error": "boom"},
        {"code": "600001", "scores": {"technical": {"score": None}, "sentiment": {"score": None},
                                       "catalyst": {"score": None}, "deep": {"score": None}}},
        _scored("600002", 5, 5, 5, 5),
    )

    written = fdl.record_scores(batch, asof="2026-07-02", path=str(log))

    assert written == 1
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["code"] == "600002"


def test_record_scores_appends_across_days(tmp_path):
    log = tmp_path / "four_dim_score_log.jsonl"
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text("{}", encoding="utf-8")
    second.write_text("{}", encoding="utf-8")
    fdl.record_scores(
        _batch(_scored("600000", 8, 6, 7, 5)), asof="2026-07-01",
        path=str(log), input_snapshot_path=str(first),
    )
    fdl.record_scores(
        _batch(_scored("600000", 7, 6, 6, 5)), asof="2026-07-02",
        path=str(log), input_snapshot_path=str(second),
    )

    rows = fdl.load_scores(str(log))
    assert len(rows) == 2
    assert {r["trading_date"] for r in rows} == {"2026-07-01", "2026-07-02"}


def test_record_scores_is_idempotent_for_same_snapshot(tmp_path):
    log = tmp_path / "four_dim_score_log.jsonl"
    snapshot = tmp_path / "candidate_pool.json"
    snapshot.write_text("{}", encoding="utf-8")
    batch = _batch(_scored("600000", 8, 6, 7, 5))

    assert fdl.record_scores(
        batch, asof="2026-07-02", path=str(log), input_snapshot_path=str(snapshot)
    ) == 1
    assert fdl.record_scores(
        batch, asof="2026-07-02", path=str(log), input_snapshot_path=str(snapshot)
    ) == 0
    assert len(fdl.load_scores(str(log))) == 1


def test_observation_identity_changes_with_scores_and_effective_weights(tmp_path):
    log = tmp_path / "four_dim_score_log.jsonl"
    snapshot = tmp_path / "candidate_pool.json"
    snapshot.write_text("{}", encoding="utf-8")
    first = _scored("600000", 8, 6, 7, 5)
    second = _scored("600000", 7, 6, 7, 5)
    second["effective_weight_values"] = {
        "technical": 0.20, "sentiment": 0.30, "catalyst": 0.30, "deep": 0.20,
    }

    assert fdl.record_scores(_batch(first), asof="2026-07-02", path=str(log), input_snapshot_path=str(snapshot)) == 1
    assert fdl.record_scores(_batch(second), asof="2026-07-02", path=str(log), input_snapshot_path=str(snapshot)) == 1
    rows = fdl.load_scores(str(log))
    assert rows[0]["observation_id"] != rows[1]["observation_id"]
    assert rows[0]["input_bundle_sha256"] != rows[1]["input_bundle_sha256"]


def test_mutable_snapshot_and_degraded_dimension_are_pit_incomplete(tmp_path):
    log = tmp_path / "four_dim_score_log.jsonl"
    snapshot = tmp_path / "candidate_pool_latest.json"
    snapshot.write_text("{}", encoding="utf-8")
    item = _scored("600000", 8, 6, 7, 5)
    item["dimension_provenance"]["catalyst"]["status"] = "degraded"

    fdl.record_scores(_batch(item), asof="2026-07-02", path=str(log), input_snapshot_path=str(snapshot))
    pit = fdl.load_scores(str(log))[0]["point_in_time"]
    assert pit["status"] == "incomplete"
    assert "input_snapshot_immutable_ref" in pit["missing"]
    assert "catalyst.status" in pit["missing"]


def test_record_scores_never_raises_on_bad_input(tmp_path):
    log = tmp_path / "four_dim_score_log.jsonl"
    assert fdl.record_scores({"results": "not-a-list"}, asof="2026-07-02", path=str(log)) == 0
    assert fdl.record_scores({}, asof="2026-07-02", path=str(log)) == 0
    assert not log.exists()


def test_load_scores_skips_corrupt_lines(tmp_path):
    log = tmp_path / "four_dim_score_log.jsonl"
    log.write_text(
        '{"code":"600000","technical":8}\n{not json\n{"code":"600001","technical":4}\n',
        encoding="utf-8",
    )
    rows = fdl.load_scores(str(log))
    assert [r["code"] for r in rows] == ["600000", "600001"]
