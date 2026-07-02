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
        "scores": {
            "technical": {"score": tech},
            "sentiment": {"score": sent},
            "catalyst": {"score": cat},
            "deep": {"score": deep},
        },
    }


def test_record_scores_writes_one_row_per_scored_stock(tmp_path):
    log = tmp_path / "four_dim_score_log.jsonl"
    batch = _batch(
        _scored("sh600000", 8, 6, 7, 5),
        _scored("sz000001", 4, 3, 2, 9),
    )

    written = fdl.record_scores(batch, asof="2026-07-02", path=str(log))

    assert written == 2
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["code"] == "600000"  # prefix stripped
    assert rows[0]["date"] == "2026-07-02"
    assert rows[0]["technical"] == 8
    assert rows[1]["code"] == "000001"
    assert rows[1]["deep"] == 9


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
    fdl.record_scores(_batch(_scored("600000", 8, 6, 7, 5)), asof="2026-07-01", path=str(log))
    fdl.record_scores(_batch(_scored("600000", 7, 6, 6, 5)), asof="2026-07-02", path=str(log))

    rows = fdl.load_scores(str(log))
    assert len(rows) == 2
    assert {r["date"] for r in rows} == {"2026-07-01", "2026-07-02"}


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
