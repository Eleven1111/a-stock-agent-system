"""catalog 数据集的生产方 — 契约符合性、PIT 正确性与空集失败（纯函数，不触网）"""

import pytest

import dataset_contract
import dataset_projection as projection


CATALOG = dataset_contract.load_catalog("config/dataset_catalog.json")
DIRECTION = dataset_contract.resolve_dataset(CATALOG, projection.DIRECTION_DATASET_ID)
SETTLED = dataset_contract.resolve_dataset(CATALOG, projection.SETTLED_DATASET_ID)


def _bars(*pairs):
    return [{"date": day, "close": close} for day, close in pairs]


def _snapshot(codes=("600000",), day="2026-06-10"):
    return {
        "date": day,
        "generated_at": f"{day}T09:35:00+08:00",
        "snapshot_sha256": "sha256:snap",
        "candidates": [{"code": code, "score": 7.5} for code in codes],
    }


def _settled(**overrides):
    base = {
        "code": "600000",
        "strategy_id": "daban:first_board_reseal",
        "signal_date": "2026-06-10",
        "settled_on": "2026-06-11",
        "score": 7.5,
        "t1_close_ret": 3.0,
        "signal_id": "sig-1",
        "recorded_at": "2026-06-10T09:35:00+08:00",
    }
    base.update(overrides)
    return base


# ---------- 方向数据集 ----------

def test_direction_rows_satisfy_the_published_contract():
    bars = _bars(("2026-06-09", 10.0), ("2026-06-10", 10.0), ("2026-06-11", 10.5))

    payload = projection.build_direction_rows(
        [_snapshot()], {"600000": bars}, DIRECTION
    )

    assert payload["dataset_id"] == projection.DIRECTION_DATASET_ID
    assert payload["contract_hash"] == DIRECTION["contract_hash"]
    assert payload["validation"]["status"] == "valid"
    row = payload["rows"][0]
    assert row["src"] == "2026-06-10" and row["dst"] == "2026-06-11"
    assert row["forward_return"] == pytest.approx(0.05)
    assert row["outcome_available_at"] == "2026-06-11T15:00:00+08:00"


def test_entry_price_never_uses_a_bar_after_the_cutoff():
    """入场价只能取 cutoff 当日或之前——用了 T+1 的收盘就是前视偏差。"""
    bars = _bars(("2026-06-10", 10.0), ("2026-06-11", 20.0), ("2026-06-12", 22.0))

    payload = projection.build_direction_rows(
        [_snapshot()], {"600000": bars}, DIRECTION
    )

    # 若误取 T+1 的 20.0 作入场价，前瞻收益会是 +10%；正确应为 20/10-1 = +100%
    assert payload["rows"][0]["forward_return"] == pytest.approx(1.0)


def test_horizon_selects_the_nth_bar_after_the_cutoff():
    bars = _bars(
        ("2026-06-10", 10.0), ("2026-06-11", 11.0), ("2026-06-12", 12.0)
    )

    payload = projection.build_direction_rows(
        [_snapshot()], {"600000": bars}, DIRECTION, horizon_days=2
    )

    row = payload["rows"][0]
    assert row["dst"] == "2026-06-12"
    assert row["forward_return"] == pytest.approx(0.2)


def test_missing_forward_bars_drag_coverage_below_the_contract_minimum():
    """契约要求 0.95 覆盖率：漏掉一半候选必须 fail-closed，而不是照发一半数据。"""
    bars_ok = _bars(("2026-06-10", 10.0), ("2026-06-11", 10.5))

    with pytest.raises(dataset_contract.DatasetContractError) as excinfo:
        projection.build_direction_rows(
            [_snapshot(codes=("600000", "600001"))],
            {"600000": bars_ok, "600001": _bars(("2026-06-10", 10.0))},
            DIRECTION,
        )

    assert "coverage_ratio_below_minimum" in excinfo.value.errors


def test_coverage_at_the_minimum_is_accepted():
    """对照组：20 个候选缺 1 个 = 0.95，恰好达标——证明上一条拦的是覆盖率本身。"""
    codes = tuple(f"6000{index:02d}" for index in range(20))
    bars_by_code = {
        code: _bars(("2026-06-10", 10.0), ("2026-06-11", 10.5)) for code in codes
    }
    bars_by_code[codes[-1]] = _bars(("2026-06-10", 10.0))

    payload = projection.build_direction_rows(
        [_snapshot(codes=codes)], bars_by_code, DIRECTION
    )

    assert payload["considered"] == 20
    assert len(payload["rows"]) == 19
    assert payload["coverage_ratio"] == pytest.approx(0.95)
    assert payload["validation"]["status"] == "valid"


def test_empty_projection_fails_closed_instead_of_reporting_full_coverage():
    """空集不得被算成 coverage=1.0——这是本仓踩过的假绿。"""
    with pytest.raises(dataset_contract.DatasetContractError) as excinfo:
        projection.build_direction_rows([], {}, DIRECTION)
    assert "no_source_records" in excinfo.value.errors

    empty_snapshot = {**_snapshot(), "candidates": []}
    with pytest.raises(dataset_contract.DatasetContractError):
        projection.build_direction_rows([empty_snapshot], {}, DIRECTION)


def test_candidates_without_any_bars_yield_no_projectable_records():
    with pytest.raises(dataset_contract.DatasetContractError) as excinfo:
        projection.build_direction_rows([_snapshot()], {"600000": []}, DIRECTION)
    assert "no_projectable_records" in excinfo.value.errors


def test_snapshot_without_a_reference_hash_is_not_projected():
    unreferenced = {**_snapshot(), "snapshot_sha256": ""}
    with pytest.raises(dataset_contract.DatasetContractError):
        projection.build_direction_rows(
            [unreferenced],
            {"600000": _bars(("2026-06-10", 10.0), ("2026-06-11", 10.5))},
            DIRECTION,
        )


# ---------- 已结算信号数据集 ----------

def test_settled_rows_carry_both_gross_and_net_returns():
    payload = projection.build_settled_signal_rows([_settled()], SETTLED)

    row = payload["rows"][0]
    assert payload["dataset_id"] == projection.SETTLED_DATASET_ID
    assert payload["validation"]["status"] == "valid"
    assert row["forward_return"] == pytest.approx(0.03)
    # 税后必须严格小于税前，且差额即双边成本
    assert row["net_forward_return"] < row["forward_return"]
    assert row["net_forward_return"] == pytest.approx(0.028856, abs=1e-6)
    assert row["strategy_id"] == "daban:first_board_reseal"


def test_unsettled_signals_are_not_source_records():
    with pytest.raises(dataset_contract.DatasetContractError) as excinfo:
        projection.build_settled_signal_rows(
            [_settled(t1_close_ret=None)], SETTLED
        )
    assert "no_source_records" in excinfo.value.errors


def test_outcome_dated_before_the_signal_is_rejected():
    """结算日早于信号日 = 时间倒流，必须投不出来而不是产出坏行。"""
    with pytest.raises(dataset_contract.DatasetContractError) as excinfo:
        projection.build_settled_signal_rows(
            [_settled(settled_on="2026-06-09")], SETTLED
        )
    assert "no_projectable_records" in excinfo.value.errors


def test_records_without_a_reference_are_not_projected():
    with pytest.raises(dataset_contract.DatasetContractError):
        projection.build_settled_signal_rows(
            [_settled(signal_id="", snapshot_ref="")], SETTLED
        )


def test_net_return_restates_with_a_different_notional():
    small = projection.build_settled_signal_rows(
        [_settled()], SETTLED, notional=5_000.0
    )["rows"][0]
    large = projection.build_settled_signal_rows(
        [_settled()], SETTLED, notional=200_000.0
    )["rows"][0]

    assert small["net_forward_return"] < large["net_forward_return"]
    assert small["forward_return"] == large["forward_return"]
