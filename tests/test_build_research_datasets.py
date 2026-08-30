"""研究数据集物化作业 — 契约符合、失败具名、不触网（settled 路径纯离线）"""

import json

import pytest

from scripts import build_research_datasets as builder
import dataset_contract


CATALOG = dataset_contract.load_catalog(builder.CATALOG_PATH)


def _settled(index=0, **overrides):
    base = {
        "code": f"60000{index}",
        "name": f"n{index}",
        "grade": "A",
        "score": 7.0 + index,
        "strategy_id": "daban:first_board_reseal",
        "signal_date": "2026-06-10",
        "settled_on": "2026-06-11",
        "signal_id": f"sig-{index}",
        "t1_close_ret": 3.0,
        "settlement_status": "final",
    }
    base.update(overrides)
    return base


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    return tmp_path


def _write_history(state_home, records):
    from paths import data_file

    path = data_file("stock-triage", builder.HISTORY_FILENAME)
    import os

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(records, handle)
    return path


def test_settled_dataset_is_written_and_conforms_to_the_catalog(state_home):
    _write_history(state_home, [_settled(i) for i in range(3)])

    result = builder.build_all("2026-08-12", include_direction=False)

    assert result["written"] == 1
    assert result["records"] == 3
    assert result["has_signal"] is True
    assert result["catalog_hash"] == CATALOG["catalog_hash"]
    entry = result["datasets"][0]
    assert entry["status"] == "written"
    assert entry["coverage_ratio"] == pytest.approx(1.0)
    with open(entry["path"], encoding="utf-8") as handle:
        payload = json.load(handle)
    assert payload["validation"]["status"] == "valid"
    assert payload["rows"][0]["net_forward_return"] < payload["rows"][0]["forward_return"]


def test_no_settled_signals_is_reported_by_name_not_silently(state_home):
    _write_history(state_home, [])

    result = builder.build_all("2026-08-12", include_direction=False)

    entry = result["datasets"][0]
    assert entry["status"] == "skipped"
    assert entry["reason"] == "no_source_records"
    assert result["has_signal"] is False
    assert result["written"] == 0


def test_contract_failure_is_surfaced_as_a_named_skip(state_home):
    """一半记录缺结算日 → 覆盖率 0.5 跌破契约下限，必须是具名 skip 而非崩溃。"""
    records = [_settled(i) for i in range(10)]
    for record in records[:5]:
        record["settled_on"] = None

    _write_history(state_home, records)
    result = builder.build_all("2026-08-12", include_direction=False)

    entry = result["datasets"][0]
    assert entry["status"] == "skipped"
    assert "coverage_ratio_below_minimum" in entry["reason"]


def test_settled_only_never_reaches_the_network(state_home, monkeypatch):
    """离线路径必须真的离线：取行情被调用即失败。"""
    def _explode(*args, **kwargs):
        raise AssertionError("settled-only path must not fetch quotes")

    monkeypatch.setattr(builder, "fetch_tencent_kline", _explode)
    _write_history(state_home, [_settled(0)])

    result = builder.build_all("2026-08-12", include_direction=False)

    assert result["written"] == 1


def test_direction_dataset_without_snapshots_is_skipped(state_home, monkeypatch):
    monkeypatch.setattr(builder, "fetch_tencent_kline", lambda *a, **k: [])
    _write_history(state_home, [_settled(0)])

    result = builder.build_all("2026-08-12", include_direction=True)

    direction = next(
        item for item in result["datasets"]
        if item["dataset_id"] == builder.projection.DIRECTION_DATASET_ID
    )
    assert direction["status"] == "skipped"
    assert direction["reason"] == "no_research_snapshots"


def test_output_declares_it_is_research_only(state_home):
    _write_history(state_home, [_settled(0)])

    result = builder.build_all("2026-08-12", include_direction=False)

    assert result["research_only"] is True
    assert result["trading_action"] == "none"


def test_forward_gate_datasets_are_materialized_per_strategy_and_remain_research_only(
    state_home, monkeypatch,
):
    contract = dataset_contract.resolve_dataset(CATALOG, "settled_forward_samples_v1")
    row = {
        "entity_id": "600001", "strategy_id": "rank_surprise",
        "decision_id": "d" * 64, "src": "2026-08-24",
        "decision_available_at": "2026-08-24T23:40:00+08:00",
        "entry_date": "2026-08-25", "dst": "2026-08-25",
        "outcome_available_at": "2026-08-25T15:00:00+08:00",
        "horizon_sessions": 1, "is_primary_horizon": True,
        "gross_forward_return": 0.03, "net_forward_return": 0.027,
        "benchmark_forward_return": 0.01, "gross_alpha": 0.02, "net_alpha": 0.017,
        "prediction_ref": "d" * 64, "prediction_sha256": "p" * 64,
        "shadow_sha256": "s" * 64, "evidence_sha256": "e" * 64,
        "strategy_rules_sha256": "r" * 64,
        "settlement_policy_sha256": "q" * 64, "bar_snapshot_sha256": "b" * 64,
    }

    def fake(strategy_id, **kwargs):
        if strategy_id != "rank_surprise":
            raise ValueError("no_eligible_forward_predictions")
        return {
            "schema": "settled_forward_samples_v1", "dataset_id": contract["dataset_id"],
            "contract_hash": contract["contract_hash"], "catalog_hash": CATALOG["catalog_hash"],
            "strategy_id": strategy_id, "rows": [row], "considered": 1,
            "coverage_ratio": 1.0, "research_only": True,
        }

    monkeypatch.setattr(builder.forward, "build_gate_dataset", fake)
    result = builder.build_forward_datasets(CATALOG, "2026-08-27")
    assert result["status"] == "written"
    assert result["record_count"] == 1
    assert result["research_only"] is True
    assert result["strategies"][0]["strategy_id"] == "rank_surprise"
    with open(result["strategies"][0]["path"], encoding="utf-8") as handle:
        persisted = json.load(handle)
    assert persisted["rows"] == [row]
