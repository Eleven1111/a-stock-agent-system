#!/usr/bin/env python3
"""
策略注册表 — research_gate 结论 + 实盘门控的单一裁决处
======================================================
回答两个问题：
1) 某个 strategy_id 是否已通过离线研究闸门（research_gate.allowed_in_live_agent）？
2) 该策略在实盘统计下是否仍被允许（performance_tracker 门控未将其停用）？

这是"信号过闸才加权"红线的执行点：缠论结构信号默认 0 权重（未注册=不允许），
只有研究证据通过、promotion 到 manual_pilot/live 且未被门控停用时，
is_allowed_in_live 才返回 True。淘汰走门控(set_gating)，改规则走 research_gate——两条路分开，
避免用实盘结果回拟合入场规则（过拟合）。

状态文件：$HERMES_HOME/skills/stock-triage/data/strategy_registry.json
并发安全：读改写走 state_store.mutate_json 单锁事务。
"""

import hashlib
import math
import os
import sys
from datetime import date, datetime
from functools import lru_cache
from typing import Any, Dict, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from paths import data_file  # noqa: E402
from research_artifact import verify_artifact  # noqa: E402
from state_store import mutate_json, read_json  # noqa: E402
from validation_program import (  # noqa: E402
    load_validation_thresholds,
    verify_oos_precommit_record,
    verify_validation_artifact,
)


PROMOTION_STATES = (
    "research_only",
    "shadow",
    "eligible_for_manual_pilot",
    "manual_pilot",
    "live",
)
_NEXT_PROMOTION_STATE = {
    "shadow": "eligible_for_manual_pilot",
    "eligible_for_manual_pilot": "manual_pilot",
    "manual_pilot": "live",
}


def _default_file() -> str:
    # 不在模块级固定，每次读 HERMES_HOME，便于测试重定向。
    return data_file("stock-triage", "strategy_registry.json")


def _now() -> str:
    return datetime.now().isoformat()


def _file_signature(path: str) -> tuple[int, int] | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


@lru_cache(maxsize=128)
def _verify_cached(
    path: str,
    expected_sha256: str,
    artifact_signature: tuple[int, int],
    source_signature: tuple[int, int] | None,
) -> bool:
    del artifact_signature, source_signature
    return bool(
        verify_artifact(path, expected_sha256=expected_sha256).get("valid")
    )


def _evidence_valid(
    strategy_id: str,
    artifact_path: Any,
    expected_sha256: Any,
    expected_stats: Optional[Dict[str, Any]] = None,
) -> tuple[bool, str]:
    path = os.path.abspath(os.path.expanduser(str(artifact_path or "")))
    digest = str(expected_sha256 or "")
    if not path or not digest:
        return False, "verified evidence artifact is required"
    artifact = read_json(path, None)
    if not isinstance(artifact, dict):
        return False, "evidence artifact is unreadable"
    source_path = str((artifact.get("source") or {}).get("input_path") or "")
    artifact_signature = _file_signature(path)
    if artifact_signature is None:
        return False, "evidence artifact is missing"
    if not _verify_cached(
        path,
        digest,
        artifact_signature,
        _file_signature(source_path) if source_path else None,
    ):
        return False, "evidence artifact verification failed"
    if str(artifact.get("strategy_id") or "") != str(strategy_id):
        return False, "evidence artifact strategy_id mismatch"
    metrics = artifact.get("gate_metrics") or {}
    for field, expected in (expected_stats or {}).items():
        if field not in metrics or expected is None:
            continue
        try:
            if abs(float(metrics[field]) - float(expected)) > 1e-12:
                return False, f"evidence metric mismatch: {field}"
        except (TypeError, ValueError):
            return False, f"evidence metric invalid: {field}"
    return True, "verified"


def get(strategy_id: str, registry_file: Optional[str] = None) -> Optional[Dict[str, Any]]:
    data = read_json(registry_file or _default_file(), {})
    return data.get(strategy_id) if isinstance(data, dict) else None


def all_strategies(registry_file: Optional[str] = None) -> Dict[str, Any]:
    data = read_json(registry_file or _default_file(), {})
    return data if isinstance(data, dict) else {}


def register_gate_result(strategy_id: str, gate_output: Dict[str, Any],
                         registry_file: Optional[str] = None) -> Dict[str, Any]:
    """登记 research_gate 输出（gate_decision / allowed_in_live_agent / gate_asof）。"""
    rf = registry_file or _default_file()
    evidence = gate_output.get("evidence") or {}
    requested_allowed = (
        gate_output.get("decision") == "passed_for_reference"
        and gate_output.get("allowed_in_live_agent") is True
    )
    evidence_verified, evidence_reason = _evidence_valid(
        strategy_id,
        evidence.get("artifact"),
        evidence.get("sha256"),
        gate_output.get("stats") if requested_allowed else None,
    ) if requested_allowed else (False, "gate did not allow live use")
    allowed = requested_allowed and evidence_verified

    def _mut(data: Any) -> Dict[str, Any]:
        if not isinstance(data, dict):
            data = {}
        rec = data.get(strategy_id, {})
        rec.update({
            "strategy_id": strategy_id,
            "gate_decision": gate_output.get("decision"),
            "allowed_in_live_agent": allowed,
            "gate_asof": gate_output.get("asof") or date.today().isoformat(),
            "gate_stats": gate_output.get("stats"),
            "evidence_verified": evidence_verified,
            "evidence_reason": evidence_reason,
            "evidence_artifact": evidence.get("artifact"),
            "evidence_sha256": evidence.get("sha256"),
            "updated_at": _now(),
        })
        rec.setdefault("gating_status", "enabled")
        rec.setdefault("promotion", {
            "state": "research_only",
            "reason": "promotion_evidence_required",
            "live_effect": "none",
            "pilot_weight": 0.0,
            "history": [],
            "updated_at": _now(),
        })
        data[strategy_id] = rec
        return data

    return mutate_json(rf, _mut, {})[strategy_id]


def set_gating(strategy_id: str, enabled: bool, reason: str = "",
               expectancy: Optional[float] = None, samples: Optional[int] = None,
               registry_file: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """实盘门控：停用/启用某策略（不改入场规则，仅控制是否计权/建仓）。"""
    rf = registry_file or _default_file()

    def _mut(data: Any) -> Dict[str, Any]:
        if not isinstance(data, dict):
            data = {}
        rec = data.get(strategy_id) or {"strategy_id": strategy_id, "allowed_in_live_agent": False}
        rec["gating_status"] = "enabled" if enabled else "disabled"
        rec["gating_reason"] = reason
        if expectancy is not None:
            rec["live_expectancy"] = expectancy
        if samples is not None:
            rec["live_samples"] = samples
        rec["updated_at"] = _now()
        data[strategy_id] = rec
        return data

    return mutate_json(rf, _mut, {}).get(strategy_id)


def _base_live_allowed(strategy_id: str, rec: Optional[Dict[str, Any]]) -> bool:
    if not rec:
        return False
    if not rec.get("allowed_in_live_agent") or rec.get("gating_status", "enabled") == "disabled":
        return False
    valid, _ = _evidence_valid(
        strategy_id,
        rec.get("evidence_artifact"),
        rec.get("evidence_sha256"),
        rec.get("gate_stats"),
    )
    return valid


def is_allowed_in_live(strategy_id: str, registry_file: Optional[str] = None) -> bool:
    """Whether the strategy may have any live effect after every admission gate."""
    rec = get(strategy_id, registry_file)
    if not _base_live_allowed(strategy_id, rec):
        return False
    promotion = (rec or {}).get("promotion")
    if not isinstance(promotion, dict):
        return False
    return promotion.get("state") in {"manual_pilot", "live"}


def live_record(strategy_id: str, registry_file: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return the registry record with a freshly verified runtime admission bit."""
    rec = get(strategy_id, registry_file)
    if not rec:
        return None
    return {
        **rec,
        "runtime_allowed": is_allowed_in_live(strategy_id, registry_file),
        "runtime_weight": live_weight(strategy_id, registry_file),
    }


def live_weight(strategy_id: str, registry_file: Optional[str] = None) -> float:
    """Return zero in research/shadow and a precommitted cap in manual pilot."""
    rec = get(strategy_id, registry_file)
    if not _base_live_allowed(strategy_id, rec):
        return 0.0
    promotion = (rec or {}).get("promotion")
    if not isinstance(promotion, dict):
        return 0.0
    if promotion.get("state") == "manual_pilot":
        try:
            weight = float(promotion.get("pilot_weight", 0))
            maximum = float(promotion["policy"]["maximum_manual_pilot_weight"])
        except (KeyError, TypeError, ValueError):
            return 0.0
        return weight if math.isfinite(weight) and 0 < weight <= maximum else 0.0
    return 1.0 if promotion.get("state") == "live" else 0.0


def promotion_state(
    strategy_id: str, registry_file: Optional[str] = None
) -> Dict[str, Any]:
    """Return the durable promotion state; unregistered strategies are research-only."""

    rec = get(strategy_id, registry_file) or {}
    promotion = rec.get("promotion")
    if isinstance(promotion, dict) and promotion.get("state") in PROMOTION_STATES:
        return promotion
    return {"state": "research_only", "reason": "promotion_not_started"}


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValueError("threshold_config_invalid") from exc
    return digest.hexdigest()


def _promotion_history(
    promotion: Dict[str, Any], *, target: str, reason: str
) -> list[Dict[str, Any]]:
    history = promotion.get("history")
    if not isinstance(history, list):
        history = []
    event = {
        "from": promotion.get("state", "research_only"),
        "to": target,
        "reason": reason,
        "at": _now(),
    }
    return [*history[-99:], event]


def _validated_precommit(
    precommit: Dict[str, Any], thresholds_path: str
) -> tuple[str, Dict[str, Any]]:
    threshold_hash = _sha256_file(thresholds_path)
    required = (
        verify_oos_precommit_record(precommit),
        precommit.get("record_type") == "precommit",
        precommit.get("schema_version") == "oos-precommit-v1",
        bool(precommit.get("precommit_id")),
        precommit.get("clean_tree") is True,
        bool(precommit.get("ancestor_commit")),
        bool(precommit.get("variants")),
        bool(precommit.get("fold_ids")),
        precommit.get("thresholds_sha256") == threshold_hash,
    )
    if not all(required):
        raise ValueError("threshold_precommit_mismatch")
    config = load_validation_thresholds(thresholds_path)
    return threshold_hash, config


def start_shadow(
    strategy_id: str,
    *,
    precommit: Dict[str, Any],
    thresholds_path: str,
    registry_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Enter shadow from research-only, or reset after a new threshold precommit."""

    threshold_hash, config = _validated_precommit(precommit, thresholds_path)
    rf = registry_file or _default_file()

    def _mut(data: Any) -> Dict[str, Any]:
        if not isinstance(data, dict):
            data = {}
        rec = data.get(strategy_id) or {
            "strategy_id": strategy_id,
            "allowed_in_live_agent": False,
            "gating_status": "enabled",
        }
        current = rec.get("promotion")
        if not isinstance(current, dict):
            current = {"state": "research_only", "reason": "promotion_not_started"}
        same_threshold = current.get("thresholds_sha256") == threshold_hash
        if same_threshold and current.get("state") == "shadow":
            return data
        if current.get("state") != "research_only" and same_threshold:
            raise ValueError("invalid_promotion_transition")
        reason = "shadow_window_reset" if current.get("thresholds_sha256") else "shadow_started"
        promotion = {
            "state": "shadow",
            "reason": reason,
            "precommit_id": precommit["precommit_id"],
            "thresholds_sha256": threshold_hash,
            "threshold_config_sha256": config["config_sha256"],
            "threshold_schema_version": config["schema_version"],
            "policy": {
                **config["empirical"],
                **config["shadow"],
            },
            "observed_trading_days": 0,
            "live_effect": "none",
            "pilot_weight": 0.0,
            "history": _promotion_history(current, target="shadow", reason=reason),
            "updated_at": _now(),
        }
        rec["promotion"] = promotion
        rec["updated_at"] = _now()
        data[strategy_id] = rec
        return data

    return mutate_json(rf, _mut, {})[strategy_id]


def _promotion_evidence_ready(
    strategy_id: str,
    promotion: Dict[str, Any],
    empirical_gate: Any,
    shadow_record: Any,
) -> bool:
    if not isinstance(empirical_gate, dict) or not isinstance(shadow_record, dict):
        return False
    if not verify_validation_artifact(empirical_gate) or not verify_validation_artifact(shadow_record):
        return False
    policy = promotion.get("policy") or {}
    samples = empirical_gate.get("effective_samples") or {}
    requirements = (
        empirical_gate.get("schema_version") == "empirical-validation-gate-v1",
        empirical_gate.get("computed_by") == "validation_program-v1",
        empirical_gate.get("status") == "passed",
        empirical_gate.get("production_release") == "eligible_for_review",
        not empirical_gate.get("reasons"),
        int(empirical_gate.get("real_trading_days", 0))
        >= int(policy.get("minimum_real_trading_days", 60)),
        samples.get("status") == "evaluated",
        float(samples.get("trade", 0))
        >= float(policy.get("minimum_trade_effective_samples", math.inf)),
        float(samples.get("stock", 0))
        >= float(policy.get("minimum_stock_effective_samples", math.inf)),
        float(samples.get("regime", 0))
        >= float(policy.get("minimum_regime_effective_samples", math.inf)),
        empirical_gate.get("statistics_status") == "passed",
        empirical_gate.get("broker_status") == "reconciled",
        shadow_record.get("schema_version") == "shadow-window-v1",
        shadow_record.get("computed_by") == "validation_program-v1",
        shadow_record.get("strategy_id") == strategy_id,
        shadow_record.get("status") == "eligible_for_manual_pilot",
        shadow_record.get("precommit_id") == promotion.get("precommit_id"),
        shadow_record.get("thresholds_sha256") == promotion.get("thresholds_sha256"),
        int(shadow_record.get("observed_trading_days", 0))
        >= int(policy.get("minimum_trading_days", math.inf)),
        float(shadow_record.get("simulation_error", math.inf))
        <= float(policy.get("maximum_simulation_error", -math.inf)),
    )
    return all(requirements)


def _approval_valid(strategy_id: str, approval: Any) -> bool:
    if not isinstance(approval, dict):
        return False
    try:
        approved_at = datetime.fromisoformat(str(approval["approved_at"]))
    except (KeyError, TypeError, ValueError):
        return False
    return (
        approval.get("approved") is True
        and approval.get("strategy_id") == strategy_id
        and bool(str(approval.get("approver") or "").strip())
        and approved_at.tzinfo is not None
    )


def promote_strategy(
    strategy_id: str,
    target_state: str,
    *,
    empirical_gate: Optional[Dict[str, Any]] = None,
    shadow_record: Optional[Dict[str, Any]] = None,
    human_approval: Optional[Dict[str, Any]] = None,
    requested_weight: Optional[float] = None,
    registry_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Advance exactly one promotion state after verifying bound evidence."""

    if target_state not in PROMOTION_STATES:
        raise ValueError("invalid_promotion_transition")
    rf = registry_file or _default_file()

    def _mut(data: Any) -> Dict[str, Any]:
        if not isinstance(data, dict) or not isinstance(data.get(strategy_id), dict):
            raise ValueError("promotion_not_started")
        rec = data[strategy_id]
        promotion = rec.get("promotion")
        if not isinstance(promotion, dict):
            raise ValueError("promotion_not_started")
        current = str(promotion.get("state") or "research_only")
        if _NEXT_PROMOTION_STATE.get(current) != target_state:
            raise ValueError("invalid_promotion_transition")
        reason = "promotion_gate_passed"
        if target_state in {"eligible_for_manual_pilot", "live"}:
            if empirical_gate is None or shadow_record is None:
                raise ValueError("promotion_evidence_missing")
            if not _promotion_evidence_ready(
                strategy_id, promotion, empirical_gate, shadow_record
            ):
                raise ValueError("promotion_evidence_insufficient")
        if target_state == "manual_pilot":
            if not _approval_valid(strategy_id, human_approval):
                raise ValueError("manual_approval_required")
            try:
                weight = float(requested_weight)
                maximum = float(promotion["policy"]["maximum_manual_pilot_weight"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("pilot_weight_invalid") from exc
            if not math.isfinite(weight) or weight <= 0:
                raise ValueError("pilot_weight_invalid")
            if weight > maximum:
                raise ValueError("pilot_weight_exceeded")
            if not _base_live_allowed(strategy_id, rec):
                raise ValueError("research_gate_not_passed")
            promotion["pilot_weight"] = weight
            promotion["human_approval"] = dict(human_approval or {})
            reason = "manual_approval_recorded"
        if target_state == "live" and not _base_live_allowed(strategy_id, rec):
            raise ValueError("research_gate_not_passed")
        promotion["history"] = _promotion_history(
            promotion, target=target_state, reason=reason
        )
        promotion["state"] = target_state
        promotion["reason"] = reason
        promotion["updated_at"] = _now()
        if empirical_gate is not None:
            promotion["empirical_gate_sha256"] = empirical_gate.get("artifact_sha256")
        if shadow_record is not None:
            promotion["shadow_artifact_sha256"] = shadow_record.get("artifact_sha256")
            promotion["observed_trading_days"] = shadow_record.get("observed_trading_days")
        rec["promotion"] = promotion
        rec["updated_at"] = _now()
        data[strategy_id] = rec
        return data

    return mutate_json(rf, _mut, {})[strategy_id]


def apply_promotion_safety_check(
    strategy_id: str,
    *,
    thresholds_path: str,
    shadow_record: Optional[Dict[str, Any]],
    broker_report: Optional[Dict[str, Any]],
    registry_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Fail closed and auto-demote on threshold or reconciliation breaches."""

    threshold_hash = _sha256_file(thresholds_path)
    rf = registry_file or _default_file()

    def _shadow_threshold_breached(
        promotion: Dict[str, Any], record: Dict[str, Any]
    ) -> bool:
        try:
            error = float(record["simulation_error"])
            limit = float(promotion["policy"]["auto_demotion_error"])
        except (KeyError, TypeError, ValueError):
            return True
        return not math.isfinite(error) or not math.isfinite(limit) or error >= limit

    def _mut(data: Any) -> Dict[str, Any]:
        if not isinstance(data, dict) or not isinstance(data.get(strategy_id), dict):
            raise ValueError("promotion_not_started")
        rec = data[strategy_id]
        promotion = rec.get("promotion")
        if not isinstance(promotion, dict):
            raise ValueError("promotion_not_started")
        reason = "safety_check_passed"
        breached = False
        if promotion.get("thresholds_sha256") != threshold_hash:
            breached, reason = True, "threshold_hash_changed"
        elif (
            not isinstance(shadow_record, dict)
            or not verify_validation_artifact(shadow_record)
            or shadow_record.get("strategy_id") != strategy_id
            or shadow_record.get("precommit_id") != promotion.get("precommit_id")
            or shadow_record.get("thresholds_sha256") != threshold_hash
            or shadow_record.get("status") == "research_only"
            or _shadow_threshold_breached(promotion, shadow_record)
        ):
            breached = True
            reason = (
                "auto_demoted"
                if isinstance(shadow_record, dict)
                and (
                    shadow_record.get("reason") == "auto_demoted"
                    or _shadow_threshold_breached(promotion, shadow_record)
                )
                else "shadow_evidence_invalid"
            )
        elif (
            not isinstance(broker_report, dict)
            or not verify_validation_artifact(broker_report)
            or broker_report.get("schema_version") != "broker-reconciliation-v1"
            or broker_report.get("computed_by") != "validation_program-v1"
            or broker_report.get("status") != "reconciled"
        ):
            breached, reason = True, "reconciliation_error"
        if breached:
            promotion["history"] = _promotion_history(
                promotion, target="research_only", reason=reason
            )
            promotion["state"] = "research_only"
            promotion["reason"] = reason
            promotion["pilot_weight"] = 0.0
            promotion["live_effect"] = "none"
            promotion["updated_at"] = _now()
        rec["promotion"] = promotion
        rec["updated_at"] = _now()
        data[strategy_id] = rec
        return data

    return mutate_json(rf, _mut, {})[strategy_id]


def strategy_pack_hypotheses() -> Dict[str, Any]:
    """声明式策略包（config/strategy_packs/*.yaml）在本注册表语义下的定位。

    策略包是"解释与研究假设层"：它们从不写入本注册表的 allowed_in_live_agent，
    因此 is_allowed_in_live(pack_name) 恒为 False——与"未注册的 strategy_id"完全
    同义。这保证策略包永远不会绕过 research gate 影响实盘计权/排序（AGENTS.md 红线）。

    升级路径（想让某个包影响实盘）：
      1. 锁定该包的判定规则；
      2. 运行 skills/chanlun-backtest/scripts/research_gate.py 做样本外验证；
      3. 通过 OOS 墙后，用 register_gate_result 把带 sha256 证据的门禁结论登记进本表。
    在此之前，策略包只能通过 evidence_pack 的 strategy_pack_hints 段做解释性引用。

    返回 strategy_packs.registry_records()（未过门禁视图），加载失败时返回空表，
    绝不伪造任何"已允许"记录。
    """
    try:
        import strategy_packs

        return strategy_packs.registry_records()
    except Exception:  # noqa: BLE001 - 解释性视图，缺失时不得伪造门禁结论
        return {}


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="策略注册表查询")
    parser.add_argument("--list", action="store_true", help="列出全部策略")
    parser.add_argument("--check", help="查询某 strategy_id 是否允许实盘")
    args = parser.parse_args()

    if args.check:
        print(json.dumps({
            "strategy_id": args.check,
            "allowed_in_live": is_allowed_in_live(args.check),
            "record": get(args.check),
        }, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(all_strategies(), ensure_ascii=False, indent=2))
