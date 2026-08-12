"""A small, allowlisted analysis-plan compiler for research-only diagnostics."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import cross_sectional_direction
import dataset_contract
import discovery_recall
from state_store import atomic_write_json, read_json


PLAN_SCHEMA = "analysis_plan_v1"
RUN_SCHEMA = "analysis_run_v1"
ENGINE_VERSION = "analysis-plan-engine-v1"
ROOT_FIELDS = {
    "schema", "plan_id", "question", "research_only", "inputs", "nodes",
    "outputs", "plan_hash",
}
NODE_FIELDS = {"id", "operator", "inputs", "params"}
DATASET_INPUT_FIELDS = {
    "kind", "dataset_id", "contract_hash", "catalog_hash", "coverage_ratio",
}
INLINE_INPUT_FIELDS = {"kind", "schema"}
RECALL_INPUT_FIELDS = {
    "schema", "quotes", "prefilter_codes", "auction_codes", "executable_codes",
    "open_codes", "asof", "generated_at", "source_stage",
}
# 算子参数是**选择器，不是判定阈值**。判定阈值（如 cross_sectional_direction
# 的 min_pairs_per_cohort）在其模块里明确写着「固定为常量：调参 = 重新过闸」，
# 因此不暴露为参数——否则计划文件就成了绕开研究闸门的旁路。
# param_schema 里每项：type / required / 可选 default / 数值的 minimum-maximum /
# 枚举的 choices。未声明的参数名一律拒绝，白名单语义不变。
OPERATORS = {
    "group_direction_cohorts_v1": {
        "input_schemas": ("dataset:cross_sectional_direction_rows_v1",),
        "output_schema": "direction_cohorts_v1",
        "operator_version": "group-direction-cohorts-v1",
        "param_schema": {},
    },
    "group_settled_outcomes_v1": {
        "input_schemas": ("dataset:settled_signal_outcomes_v1",),
        "output_schema": "direction_cohorts_v1",
        "operator_version": "group-settled-outcomes-v1",
        "param_schema": {
            "strategy_id": {"type": "string", "required": True},
            "return_basis": {
                "type": "string",
                "required": False,
                "default": "net",
                "choices": ("gross", "net"),
            },
        },
    },
    "cross_sectional_direction_v1": {
        "input_schemas": ("direction_cohorts_v1",),
        "output_schema": "cross_sectional_direction_v1",
        "operator_version": "cross-sectional-direction-v1",
        "param_schema": {},
    },
    "discovery_recall_v1": {
        "input_schemas": ("discovery_recall_input_v1",),
        "output_schema": "discovery_recall_report_v1",
        "operator_version": "discovery-recall-v1",
        "param_schema": {},
    },
}


class AnalysisPlanError(ValueError):
    """A plan or execution input violates the bounded analysis contract."""

    def __init__(self, *errors: str) -> None:
        self.errors = tuple(dict.fromkeys(str(error) for error in errors if error))
        super().__init__("; ".join(self.errors) or "analysis_plan_invalid")


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _strict_fields(value: Mapping[str, Any], allowed: set[str], prefix: str) -> list[str]:
    return [f"{prefix}_field_not_allowed:{key}" for key in value if key not in allowed]


def _input_schemas(
    inputs: Any,
    catalog: Mapping[str, Any],
) -> tuple[list[str], dict[str, str]]:
    if not isinstance(inputs, Mapping) or not inputs:
        return ["inputs_missing"], {}
    errors: list[str] = []
    schemas: dict[str, str] = {}
    for input_id, spec in inputs.items():
        if not isinstance(spec, Mapping):
            errors.append(f"input_invalid:{input_id}")
            continue
        kind = spec.get("kind")
        allowed = DATASET_INPUT_FIELDS if kind == "dataset" else INLINE_INPUT_FIELDS
        errors.extend(_strict_fields(spec, allowed, "input"))
        if kind == "dataset":
            if spec.get("catalog_hash") != catalog.get("catalog_hash"):
                errors.append("catalog_hash_mismatch")
            try:
                contract = dataset_contract.resolve_dataset(
                    catalog,
                    str(spec.get("dataset_id") or ""),
                    contract_hash=str(spec.get("contract_hash") or ""),
                )
            except dataset_contract.DatasetContractError as exc:
                errors.extend(exc.errors)
                continue
            ratio = spec.get("coverage_ratio")
            if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1:
                errors.append(f"coverage_ratio_invalid:{input_id}")
            schemas[str(input_id)] = f"dataset:{contract['dataset_id']}"
        elif kind == "inline":
            schema = str(spec.get("schema") or "")
            if schema != "discovery_recall_input_v1":
                errors.append(f"inline_schema_not_allowed:{schema}")
            schemas[str(input_id)] = schema
        else:
            errors.append(f"input_kind_not_allowed:{kind}")
    return errors, schemas


_PARAM_TYPES = {
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
}


def _one_param_errors(node_id: str, name: str, rule: Mapping[str, Any], value: Any) -> list[str]:
    if not _PARAM_TYPES[str(rule["type"])](value):
        return [f"param_type_invalid:{node_id}.{name}"]
    errors: list[str] = []
    choices = rule.get("choices")
    if choices and value not in choices:
        errors.append(f"param_choice_invalid:{node_id}.{name}")
    minimum, maximum = rule.get("minimum"), rule.get("maximum")
    if minimum is not None and value < minimum:
        errors.append(f"param_below_minimum:{node_id}.{name}")
    if maximum is not None and value > maximum:
        errors.append(f"param_above_maximum:{node_id}.{name}")
    return errors


def _param_errors(node_id: str, operator: str, params: Any) -> list[str]:
    """按算子的 param_schema 校验；未知算子已在别处报错，这里不重复。"""
    schema = (OPERATORS.get(operator) or {}).get("param_schema")
    if schema is None:
        return []
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        return [f"params_not_object:{node_id}"]
    errors = [
        f"param_not_allowed:{node_id}.{name}"
        for name in params
        if name not in schema
    ]
    for name, rule in schema.items():
        if name not in params:
            if rule.get("required"):
                errors.append(f"param_required:{node_id}.{name}")
            continue
        errors.extend(_one_param_errors(node_id, name, rule, params[name]))
    return errors


def resolved_params(operator: str, params: Any) -> dict[str, Any]:
    """把声明的参数与 schema 默认值合并；校验已在计划编译期完成。"""
    schema = (OPERATORS.get(operator) or {}).get("param_schema") or {}
    declared = dict(params or {})
    return {
        name: declared.get(name, rule.get("default"))
        for name, rule in schema.items()
        if name in declared or "default" in rule
    }


def _node_definitions(nodes: Any, input_ids: set[str]) -> tuple[list[str], dict[str, dict[str, Any]]]:
    if not isinstance(nodes, list) or not nodes:
        return ["nodes_missing"], {}
    errors: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, Mapping):
            errors.append("node_invalid")
            continue
        errors.extend(_strict_fields(node, NODE_FIELDS, "node"))
        node_id = str(node.get("id") or "")
        if not node_id:
            errors.append("node_id_missing")
            continue
        if node_id in input_ids or node_id in by_id:
            errors.append(f"duplicate_node_id:{node_id}")
        operator = str(node.get("operator") or "")
        if operator not in OPERATORS:
            errors.append(f"operator_not_allowed:{operator}")
        if not isinstance(node.get("inputs"), list) or not node.get("inputs"):
            errors.append(f"node_inputs_missing:{node_id}")
        errors.extend(_param_errors(node_id, operator, node.get("params")))
        by_id[node_id] = dict(node)
    return errors, by_id


def _topological_order(
    nodes: Mapping[str, Mapping[str, Any]], input_ids: set[str]
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    dependencies: dict[str, set[str]] = {}
    for node_id, node in nodes.items():
        refs = {str(item) for item in node.get("inputs") or []}
        unknown = refs - input_ids - set(nodes)
        errors.extend(f"dependency_unknown:{item}" for item in sorted(unknown))
        dependencies[node_id] = refs & set(nodes)
    if errors:
        return errors, []
    order: list[str] = []
    pending = {key: set(value) for key, value in dependencies.items()}
    while pending:
        ready = sorted(key for key, deps in pending.items() if not deps)
        if not ready:
            return ["dependency_cycle"], []
        for node_id in ready:
            order.append(node_id)
            pending.pop(node_id)
        for deps in pending.values():
            deps.difference_update(ready)
    return [], order


def _type_flow_errors(
    nodes: Mapping[str, Mapping[str, Any]],
    order: Sequence[str],
    schemas: dict[str, str],
) -> list[str]:
    errors: list[str] = []
    produced = dict(schemas)
    for node_id in order:
        node = nodes[node_id]
        operator = str(node["operator"])
        spec = OPERATORS.get(operator)
        if not spec:
            continue
        actual = tuple(produced.get(str(item), "") for item in node["inputs"])
        if actual != spec["input_schemas"]:
            errors.append(f"input_schema_mismatch:{node_id}")
        produced[node_id] = str(spec["output_schema"])
    return errors


def validate_plan(plan: Mapping[str, Any], *, catalog: Mapping[str, Any]) -> list[str]:
    sealed_catalog = dataset_contract.seal_catalog(catalog)
    errors = _strict_fields(plan, ROOT_FIELDS, "plan")
    if plan.get("schema") != PLAN_SCHEMA:
        errors.append(f"schema_mismatch:{PLAN_SCHEMA}")
    if not str(plan.get("plan_id") or ""):
        errors.append("plan_id_missing")
    if not str(plan.get("question") or ""):
        errors.append("question_missing")
    if plan.get("research_only") is not True:
        errors.append("research_only_required")
    input_errors, schemas = _input_schemas(plan.get("inputs"), sealed_catalog)
    errors.extend(input_errors)
    node_errors, nodes = _node_definitions(plan.get("nodes"), set(schemas))
    errors.extend(node_errors)
    topology_errors, order = _topological_order(nodes, set(schemas))
    errors.extend(topology_errors)
    if not topology_errors:
        errors.extend(_type_flow_errors(nodes, order, schemas))
    outputs = plan.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        errors.append("outputs_missing")
    else:
        errors.extend(
            f"output_unknown:{item}" for item in outputs if str(item) not in nodes
        )
    if errors:
        raise AnalysisPlanError(*errors)
    return order


def seal_plan(plan: Mapping[str, Any], *, catalog: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(plan)
    declared_hash = body.pop("plan_hash", None)
    validate_plan(body, catalog=catalog)
    actual_hash = _hash(body)
    if declared_hash is not None and declared_hash != actual_hash:
        raise AnalysisPlanError("plan_hash_mismatch")
    return {**body, "plan_hash": actual_hash}


def _validate_recall_input(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise AnalysisPlanError("recall_input_not_object")
    errors = _strict_fields(value, RECALL_INPUT_FIELDS, "recall_input")
    required = (
        "schema", "quotes", "prefilter_codes", "auction_codes", "asof",
        "generated_at",
    )
    errors.extend(f"recall_input_missing:{key}" for key in required if not value.get(key))
    if value.get("schema") != "discovery_recall_input_v1":
        errors.append("recall_input_schema_mismatch")
    for key in ("quotes", "prefilter_codes", "auction_codes"):
        if not isinstance(value.get(key), list):
            errors.append(f"recall_input_type:{key}")
    for key in ("executable_codes", "open_codes"):
        if key in value and value.get(key) is not None and not isinstance(value.get(key), list):
            errors.append(f"recall_input_type:{key}")
    if isinstance(value.get("quotes"), list) and any(
        not isinstance(item, Mapping) for item in value["quotes"]
    ):
        errors.append("recall_input_type:quotes_item")
    try:
        date.fromisoformat(str(value.get("asof") or ""))
    except ValueError:
        errors.append("recall_input_asof_invalid")
    try:
        generated = datetime.fromisoformat(str(value.get("generated_at") or ""))
        if generated.tzinfo is None:
            raise ValueError
    except ValueError:
        errors.append("recall_input_generated_at_invalid")
    if errors:
        raise AnalysisPlanError(*errors)


def _validate_execution_inputs(
    values: Mapping[str, Any],
    plan: Mapping[str, Any],
    catalog: Mapping[str, Any],
) -> None:
    expected = set(plan["inputs"])
    actual = set(values)
    errors = [f"execution_input_missing:{key}" for key in sorted(expected - actual)]
    errors.extend(f"execution_input_unknown:{key}" for key in sorted(actual - expected))
    if errors:
        raise AnalysisPlanError(*errors)
    for input_id, binding in plan["inputs"].items():
        if binding["kind"] == "dataset":
            contract = dataset_contract.resolve_dataset(
                catalog,
                binding["dataset_id"],
                contract_hash=binding["contract_hash"],
            )
            try:
                dataset_contract.validate_records(
                    values[input_id],
                    contract,
                    coverage_ratio=float(binding["coverage_ratio"]),
                )
            except dataset_contract.DatasetContractError as exc:
                raise AnalysisPlanError(*exc.errors) from exc
        else:
            _validate_recall_input(values[input_id])


def _group_direction_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[tuple[float, float, str]]] = {}
    for row in rows:
        key = (str(row["src"]), str(row["dst"]))
        grouped.setdefault(key, []).append(
            (float(row["score"]), float(row["forward_return"]), str(row["entity_id"]))
        )
    cohorts = [
        {
            "src": src,
            "dst": dst,
            "pairs": [(score, outcome) for score, outcome, _ in sorted(pairs, key=lambda item: item[2])],
        }
        for (src, dst), pairs in sorted(grouped.items())
    ]
    return {"schema": "direction_cohorts_v1", "cohorts": cohorts}


def _group_settled_outcomes(
    rows: Sequence[Mapping[str, Any]], params: Mapping[str, Any]
) -> dict[str, Any]:
    """按策略切出该策略自己的横截面队列；税前/税后由 return_basis 选择。

    选不出任何行时**报错而非返回空队列**：空队列到下游只会得到
    ``insufficient_sample``，读起来像「样本不够」，掩盖了真正的原因
    （策略 ID 写错或该策略尚无结算）。
    """
    strategy_id = str(params["strategy_id"])
    field = "net_forward_return" if params.get("return_basis", "net") == "net" else "forward_return"
    selected = [row for row in rows if str(row.get("strategy_id") or "") == strategy_id]
    if not selected:
        raise AnalysisPlanError(f"no_rows_for_strategy:{strategy_id}")
    missing = [row for row in selected if row.get(field) is None]
    if missing:
        raise AnalysisPlanError(f"return_basis_unavailable:{field}")
    return _group_direction_rows([
        {
            "src": row["src"],
            "dst": row["dst"],
            "score": row["score"],
            "forward_return": row[field],
            "entity_id": row["entity_id"],
        }
        for row in selected
    ])


def _run_operator(operator: str, inputs: list[Any], params: Mapping[str, Any]) -> Any:
    if operator == "group_direction_cohorts_v1":
        return _group_direction_rows(inputs[0])
    if operator == "group_settled_outcomes_v1":
        return _group_settled_outcomes(inputs[0], params)
    if operator == "cross_sectional_direction_v1":
        return cross_sectional_direction.evaluate(inputs[0]["cohorts"])
    if operator == "discovery_recall_v1":
        value = inputs[0]
        return discovery_recall.build_discovery_recall_report(
            value["quotes"],
            prefilter_codes=value["prefilter_codes"],
            auction_codes=value["auction_codes"],
            executable_codes=value.get("executable_codes"),
            open_codes=value.get("open_codes"),
            asof=value["asof"],
            source_stage=value.get("source_stage") or "09:24_full_market",
            generated_at=value["generated_at"],
        )
    raise AnalysisPlanError(f"operator_not_allowed:{operator}")


def _execute_nodes(
    plan: Mapping[str, Any],
    input_values: Mapping[str, Any],
    order: Sequence[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    values = dict(input_values)
    nodes = {node["id"]: node for node in plan["nodes"]}
    lineage: dict[str, Any] = {}
    for node_id in order:
        node = nodes[node_id]
        dependencies = [values[str(item)] for item in node["inputs"]]
        operator = str(node["operator"])
        params = resolved_params(operator, node.get("params"))
        output = _run_operator(operator, dependencies, params)
        values[node_id] = output
        lineage[node_id] = {
            "operator": operator,
            "operator_version": OPERATORS[operator]["operator_version"],
            "inputs": list(node["inputs"]),
            "params": params,
            "input_hashes": [_hash(item) for item in dependencies],
            "output_hash": _hash(output),
        }
    return values, lineage


def _valid_cached_result(value: Any, cache_key: str) -> bool:
    if not isinstance(value, dict) or value.get("cache_key") != cache_key:
        return False
    claimed_hash = value.get("result_hash")
    body = {key: item for key, item in value.items() if key != "result_hash"}
    return claimed_hash == _hash(body)


def verify_run_result(value: Mapping[str, Any]) -> bool:
    """Verify a returned run, normalizing cache/path presentation fields."""

    if (
        not isinstance(value, Mapping)
        or value.get("schema") != RUN_SCHEMA
        or value.get("research_only") is not True
        or value.get("trading_action") != "none"
    ):
        return False
    body = {
        key: item
        for key, item in value.items()
        if key not in {"result_hash", "artifact_path"}
    }
    body["cached"] = False
    return value.get("result_hash") == _hash(body)


def execute_plan(
    plan: Mapping[str, Any],
    input_values: Mapping[str, Any],
    *,
    catalog: Mapping[str, Any],
    cache_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    sealed_catalog = dataset_contract.seal_catalog(catalog)
    sealed_plan = seal_plan(plan, catalog=sealed_catalog)
    order = validate_plan(sealed_plan, catalog=sealed_catalog)
    _validate_execution_inputs(input_values, sealed_plan, sealed_catalog)
    input_hash = _hash(input_values)
    cache_key = _hash({
        "engine_version": ENGINE_VERSION,
        "plan_hash": sealed_plan["plan_hash"],
        "catalog_hash": sealed_catalog["catalog_hash"],
        "input_hash": input_hash,
    })
    path = Path(cache_dir) / f"{cache_key.removeprefix('sha256:')}.json" if cache_dir else None
    if path:
        cached = read_json(str(path), None)
        if _valid_cached_result(cached, cache_key):
            return {**cached, "cached": True, "artifact_path": str(path)}
    values, lineage = _execute_nodes(sealed_plan, input_values, order)
    result_body = {
        "schema": RUN_SCHEMA,
        "engine_version": ENGINE_VERSION,
        "plan_id": sealed_plan["plan_id"],
        "plan_hash": sealed_plan["plan_hash"],
        "catalog_hash": sealed_catalog["catalog_hash"],
        "input_hash": input_hash,
        "cache_key": cache_key,
        "cached": False,
        "research_only": True,
        "trading_action": "none",
        "outputs": {node_id: values[node_id] for node_id in sealed_plan["outputs"]},
        "lineage": lineage,
    }
    result = {**result_body, "result_hash": _hash(result_body)}
    if path:
        atomic_write_json(str(path), result)
    return {**result, "artifact_path": str(path) if path else None}


__all__ = [
    "AnalysisPlanError",
    "ENGINE_VERSION",
    "PLAN_SCHEMA",
    "RUN_SCHEMA",
    "execute_plan",
    "seal_plan",
    "validate_plan",
    "verify_run_result",
]
