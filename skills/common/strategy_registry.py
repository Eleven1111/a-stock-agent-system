#!/usr/bin/env python3
"""
策略注册表 — research_gate 结论 + 实盘门控的单一裁决处
======================================================
回答两个问题：
1) 某个 strategy_id 是否已通过离线研究闸门（research_gate.allowed_in_live_agent）？
2) 该策略在实盘统计下是否仍被允许（performance_tracker 门控未将其停用）？

这是"信号过闸才加权"红线的执行点：缠论结构信号默认 0 权重（未注册=不允许），
只有 register_gate_result 写入 allowed_in_live_agent=true 且未被门控停用时，
is_allowed_in_live 才返回 True。淘汰走门控(set_gating)，改规则走 research_gate——两条路分开，
避免用实盘结果回拟合入场规则（过拟合）。

状态文件：$HERMES_HOME/skills/stock-triage/data/strategy_registry.json
并发安全：读改写走 state_store.mutate_json 单锁事务。
"""

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


def is_allowed_in_live(strategy_id: str, registry_file: Optional[str] = None) -> bool:
    """是否允许在实盘计权/建仓：过闸 且 未被门控停用。未注册默认 False。"""
    rec = get(strategy_id, registry_file)
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


def live_record(strategy_id: str, registry_file: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return the registry record with a freshly verified runtime admission bit."""
    rec = get(strategy_id, registry_file)
    if not rec:
        return None
    return {**rec, "runtime_allowed": is_allowed_in_live(strategy_id, registry_file)}


def live_weight(strategy_id: str, registry_file: Optional[str] = None) -> float:
    """实盘权重系数：允许=1.0，否则 0.0（display-only）。"""
    return 1.0 if is_allowed_in_live(strategy_id, registry_file) else 0.0


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
