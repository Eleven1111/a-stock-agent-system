"""算子参数化 — schema 校验、默认值、哈希绑定与按策略切片（纯函数，不触网）"""

import pytest

import analysis_plan
import dataset_contract


CATALOG = dataset_contract.load_catalog("config/dataset_catalog.json")
SETTLED = dataset_contract.resolve_dataset(CATALOG, "settled_signal_outcomes_v1")


def _plan(params, *, node_id="grouped"):
    return {
        "schema": analysis_plan.PLAN_SCHEMA,
        "plan_id": "settled-direction",
        "question": "该策略的打分是否预测了税后收益？",
        "research_only": True,
        "inputs": {
            "settled": {
                "kind": "dataset",
                "dataset_id": "settled_signal_outcomes_v1",
                "contract_hash": SETTLED["contract_hash"],
                "catalog_hash": CATALOG["catalog_hash"],
                "coverage_ratio": 0.99,
            }
        },
        "nodes": [
            {
                "id": node_id,
                "operator": "group_settled_outcomes_v1",
                "inputs": ["settled"],
                "params": params,
            }
        ],
        "outputs": [node_id],
    }


def _rows(strategy_id="daban:first_board_reseal", count=3):
    return [
        {
            "entity_id": f"60000{index}",
            "strategy_id": strategy_id,
            "src": "2026-06-10",
            "dst": "2026-06-11",
            "score": 7.0 + index,
            "forward_return": 0.03,
            "net_forward_return": 0.0288,
            "score_available_at": "2026-06-10T09:30:00+08:00",
            "outcome_available_at": "2026-06-11T15:00:00+08:00",
            "snapshot_ref": f"sig-{index}",
        }
        for index in range(count)
    ]


# ---------- schema 校验 ----------

def test_declared_params_are_accepted():
    sealed = analysis_plan.seal_plan(
        _plan({"strategy_id": "daban:first_board_reseal"}), catalog=CATALOG
    )
    assert sealed["plan_hash"].startswith("sha256:")


def test_unknown_param_names_are_rejected():
    with pytest.raises(analysis_plan.AnalysisPlanError) as excinfo:
        analysis_plan.seal_plan(
            _plan({"strategy_id": "s", "min_pairs_per_cohort": 1}), catalog=CATALOG
        )
    assert "param_not_allowed:grouped.min_pairs_per_cohort" in excinfo.value.errors


def test_required_param_must_be_present():
    with pytest.raises(analysis_plan.AnalysisPlanError) as excinfo:
        analysis_plan.seal_plan(_plan({}), catalog=CATALOG)
    assert "param_required:grouped.strategy_id" in excinfo.value.errors


def test_param_type_is_enforced():
    with pytest.raises(analysis_plan.AnalysisPlanError) as excinfo:
        analysis_plan.seal_plan(_plan({"strategy_id": 7}), catalog=CATALOG)
    assert "param_type_invalid:grouped.strategy_id" in excinfo.value.errors


def test_enumerated_param_rejects_values_outside_its_choices():
    with pytest.raises(analysis_plan.AnalysisPlanError) as excinfo:
        analysis_plan.seal_plan(
            _plan({"strategy_id": "s", "return_basis": "pretax"}), catalog=CATALOG
        )
    assert "param_choice_invalid:grouped.return_basis" in excinfo.value.errors


def test_operators_without_params_still_reject_any_param():
    plan = _plan({"strategy_id": "s"})
    plan["nodes"][0]["operator"] = "group_direction_cohorts_v1"
    plan["inputs"]["settled"]["dataset_id"] = "cross_sectional_direction_rows_v1"
    plan["inputs"]["settled"]["contract_hash"] = dataset_contract.resolve_dataset(
        CATALOG, "cross_sectional_direction_rows_v1"
    )["contract_hash"]

    with pytest.raises(analysis_plan.AnalysisPlanError) as excinfo:
        analysis_plan.seal_plan(plan, catalog=CATALOG)
    assert "param_not_allowed:grouped.strategy_id" in excinfo.value.errors


# ---------- 哈希绑定 ----------

def test_changing_a_param_changes_the_plan_hash():
    """参数进正文即进 plan_hash——「改参数不换哈希」这条旁路必须堵死。"""
    first = analysis_plan.seal_plan(_plan({"strategy_id": "alpha"}), catalog=CATALOG)
    second = analysis_plan.seal_plan(_plan({"strategy_id": "beta"}), catalog=CATALOG)
    basis = analysis_plan.seal_plan(
        _plan({"strategy_id": "alpha", "return_basis": "gross"}), catalog=CATALOG
    )

    assert first["plan_hash"] != second["plan_hash"]
    assert first["plan_hash"] != basis["plan_hash"]


def test_declared_plan_hash_must_match_after_params_change():
    sealed = analysis_plan.seal_plan(_plan({"strategy_id": "alpha"}), catalog=CATALOG)
    tampered = {**sealed}
    tampered["nodes"] = [{**sealed["nodes"][0], "params": {"strategy_id": "beta"}}]

    with pytest.raises(analysis_plan.AnalysisPlanError) as excinfo:
        analysis_plan.seal_plan(tampered, catalog=CATALOG)
    assert "plan_hash_mismatch" in excinfo.value.errors


# ---------- 默认值与执行 ----------

def test_default_is_applied_and_recorded():
    assert analysis_plan.resolved_params(
        "group_settled_outcomes_v1", {"strategy_id": "s"}
    ) == {"strategy_id": "s", "return_basis": "net"}


def test_return_basis_selects_gross_or_net():
    rows = _rows()
    net = analysis_plan._group_settled_outcomes(
        rows, {"strategy_id": "daban:first_board_reseal", "return_basis": "net"}
    )
    gross = analysis_plan._group_settled_outcomes(
        rows, {"strategy_id": "daban:first_board_reseal", "return_basis": "gross"}
    )

    assert net["cohorts"][0]["pairs"][0][1] == pytest.approx(0.0288)
    assert gross["cohorts"][0]["pairs"][0][1] == pytest.approx(0.03)


def test_selecting_a_strategy_with_no_rows_fails_loudly():
    """选不出行必须报错——返回空队列会在下游被读成「样本不够」，掩盖真因。"""
    with pytest.raises(analysis_plan.AnalysisPlanError) as excinfo:
        analysis_plan._group_settled_outcomes(_rows(), {"strategy_id": "absent"})
    assert "no_rows_for_strategy:absent" in excinfo.value.errors


def test_missing_return_basis_field_fails_closed():
    rows = [{**row, "net_forward_return": None} for row in _rows()]
    with pytest.raises(analysis_plan.AnalysisPlanError) as excinfo:
        analysis_plan._group_settled_outcomes(
            rows, {"strategy_id": "daban:first_board_reseal", "return_basis": "net"}
        )
    assert "return_basis_unavailable:net_forward_return" in excinfo.value.errors


def test_only_the_selected_strategy_reaches_the_cohorts():
    rows = [*_rows("alpha", 2), *_rows("beta", 3)]
    grouped = analysis_plan._group_settled_outcomes(rows, {"strategy_id": "beta"})

    assert sum(len(cohort["pairs"]) for cohort in grouped["cohorts"]) == 3
