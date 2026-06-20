#!/usr/bin/env python3
"""
缠论/打板研究闸门 — 检查回测是否满足上线前证据标准
==================================================
数据源：本地 JSON 研究状态，无网络调用。

Usage:
  python skills/chanlun-backtest/scripts/research_gate.py --example --json
  python skills/chanlun-backtest/scripts/research_gate.py --input research_state.json --json
"""

import argparse
import json
import os
import sys
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Set


ENGINE = {
    "name": "chanlun-backtest",
    "version": "1.0.0",
    "upstream_reference": "Eleven1111/chanlun-backtest@f25b36a",
    "scope": "offline strategy research gate",
}

REQUIRED_CONTROLS = {"random_entry", "simple_breakout", "buy_hold"}
REQUIRED_TESTS = {"t_test", "bootstrap", "permutation"}


def _verify_evidence(payload: Dict[str, Any]) -> Dict[str, Any]:
    path = payload.get("evidence_artifact")
    if not path:
        return {"passed": False, "reason": "OOS evidence_artifact is required"}
    common = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "common"))
    if common not in sys.path:
        sys.path.insert(0, common)
    from research_artifact import verify_artifact

    verification = verify_artifact(
        str(path),
        expected_sha256=payload.get("evidence_sha256"),
    )
    if not verification["valid"]:
        return {
            "passed": False,
            "reason": "OOS evidence verification failed: " + ",".join(verification["errors"]),
        }
    artifact = verification["artifact"]
    if str(artifact.get("strategy_id") or "") != str(payload.get("strategy_id") or ""):
        return {"passed": False, "reason": "OOS evidence strategy_id mismatch"}
    metrics = artifact.get("gate_metrics") or {}
    for field in (
        "permutation_p",
        "fdr_p",
        "oos_alpha",
        "benchmark_alpha",
        "oos_sample_count",
    ):
        if field not in payload:
            continue
        expected = _num(payload.get(field))
        actual = _num(metrics.get(field))
        if expected is None or actual is None or abs(expected - actual) > 1e-12:
            return {"passed": False, "reason": f"OOS evidence metric mismatch: {field}"}
    required_controls = _set(payload.get("controls"))
    counts = artifact.get("control_counts") or {}
    missing = sorted(name for name in required_controls if int(_num(counts.get(name), 0) or 0) <= 0)
    if missing:
        return {"passed": False, "reason": f"OOS evidence controls missing samples: {missing}"}
    return {"passed": True, "reason": "OOS evidence artifact verified"}


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "是"}
    return bool(value)


def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _set(value: Any) -> Set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {str(item) for item in value}
    if isinstance(value, dict):
        return {str(k) for k, v in value.items() if _bool(v)}
    return set()


def phase_checklist(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    controls = _set(payload.get("controls"))
    tests = _set(payload.get("stat_tests") or payload.get("tests"))
    required_controls = _set(payload.get("required_controls")) or REQUIRED_CONTROLS
    required_tests = _set(payload.get("required_stat_tests")) or REQUIRED_TESTS
    checks = [
        {
            "id": "rules_locked_before_oos",
            "passed": _bool(payload.get("rules_locked"), False),
            "reason": "rules_locked=true" if _bool(payload.get("rules_locked"), False) else "规则尚未锁定",
        },
        {
            "id": "cost_model",
            "passed": _bool(payload.get("has_costs"), False),
            "reason": "包含交易成本/滑点" if _bool(payload.get("has_costs"), False) else "未声明成本模型",
        },
        {
            "id": "all_variants",
            "passed": _bool(payload.get("reports_all_variants"), False),
            "reason": "报告全部变体" if _bool(payload.get("reports_all_variants"), False) else "存在只报最优结果风险",
        },
        {
            "id": "controls",
            "passed": required_controls.issubset(controls),
            "reason": f"missing={sorted(required_controls - controls)}",
        },
        {
            "id": "stat_tests",
            "passed": required_tests.issubset(tests),
            "reason": f"missing={sorted(required_tests - tests)}",
        },
        {
            "id": "oos_wall",
            "passed": _num(payload.get("oos_run_count"), 0) <= 1 and not _bool(payload.get("changed_after_oos"), False),
            "reason": (
                "OOS未被重复调参"
                if _num(payload.get("oos_run_count"), 0) <= 1 and not _bool(payload.get("changed_after_oos"), False)
                else "OOS已重复运行或看结果后改规则"
            ),
        },
    ]
    min_oos_samples = int(_num(payload.get("min_oos_samples"), 0) or 0)
    has_oos_result = (
        payload.get("phase") == "oos_complete"
        or int(_num(payload.get("oos_run_count"), 0) or 0) > 0
    )
    if min_oos_samples > 0 and has_oos_result:
        actual = int(_num(payload.get("oos_sample_count"), 0) or 0)
        checks.append({
            "id": "minimum_oos_sample",
            "passed": actual >= min_oos_samples,
            "reason": (
                f"样本量满足: {actual}>={min_oos_samples}"
                if actual >= min_oos_samples
                else f"样本量不足: {actual}<{min_oos_samples}"
            ),
        })
    if has_oos_result:
        evidence = _verify_evidence(payload)
        checks.append({
            "id": "evidence_artifact",
            "passed": evidence["passed"],
            "reason": evidence["reason"],
        })
    return checks


def evaluate_gate(payload: Dict[str, Any]) -> Dict[str, Any]:
    checklist = phase_checklist(payload)
    blocking_reasons = [item["reason"] for item in checklist if not item["passed"]]
    phase = payload.get("phase", "pre_oos")
    oos_run_count = int(_num(payload.get("oos_run_count"), 0) or 0)
    changed_after_oos = _bool(payload.get("changed_after_oos"), False)
    permutation_p = _num(payload.get("permutation_p") or payload.get("permutation_p_value"))
    fdr_p = _num(payload.get("fdr_p") or payload.get("fdr_p_value"))
    oos_alpha = _num(payload.get("oos_alpha"))
    benchmark_alpha = _num(payload.get("benchmark_alpha"), 0.0)

    decision = "blocked"
    allowed_in_live_agent = False
    next_actions = []

    if blocking_reasons:
        next_actions.append("补齐失败检查项后再进入下一阶段")
    elif phase in {"pre_oos", "is_locked"} and oos_run_count == 0:
        decision = "ready_for_oos"
        next_actions.append("规则已锁定，可以运行一次样本外验证；运行后禁止再调参")
    elif changed_after_oos or oos_run_count > 1:
        blocking_reasons.append("OOS墙已破坏，当前结果只能作为探索性研究")
        next_actions.append("重新定义规则并重新切分时间窗，重新开始研究流程")
    elif permutation_p is None or fdr_p is None or oos_alpha is None:
        blocking_reasons.append("缺少样本外统计结果，不能判断有效性")
        next_actions.append("补充 permutation_p、fdr_p、oos_alpha 后再判断")
    elif permutation_p <= 0.05 and fdr_p <= 0.10 and oos_alpha > max(0.0, benchmark_alpha):
        decision = "passed_for_reference"
        allowed_in_live_agent = True
        next_actions.append("可作为研究证据供日常 Agent 引用，但仍需实时可成交性/风控闸门")
    else:
        decision = "failed"
        next_actions.append("不要上线为已验证策略；可记录为失败假设或重新提出新规则")

    if blocking_reasons:
        decision = "blocked"
        allowed_in_live_agent = False

    return {
        "schema": "chanlun_research_gate_v1",
        "generated_at": datetime.now().isoformat(),
        "asof": payload.get("asof") or date.today().isoformat(),
        "engine": ENGINE,
        "strategy_id": payload.get("strategy_id", "unknown"),
        "phase": phase,
        "decision": decision,
        "allowed_in_live_agent": allowed_in_live_agent,
        "blocking_reasons": blocking_reasons,
        "phase_checklist": checklist,
        "stats": {
            "permutation_p": permutation_p,
            "fdr_p": fdr_p,
            "oos_alpha": oos_alpha,
            "benchmark_alpha": benchmark_alpha,
        },
        "warnings": [
            "本工具只做离线研究验证，不输出实时买卖指令",
            "OOS结果只能在规则锁定后运行一次；看结果后改规则会破坏样本外证据",
        ],
        "next_actions": next_actions,
    }


def format_report(result: Dict[str, Any]) -> str:
    lines = [
        f"## 策略研究闸门 | {result['strategy_id']}",
        f"结论：{result['decision']}",
        f"可供实时 Agent 引用：{'是' if result['allowed_in_live_agent'] else '否'}",
        "",
        "### 检查项",
    ]
    for item in result["phase_checklist"]:
        mark = "OK" if item["passed"] else "NO"
        lines.append(f"- {mark} {item['id']}: {item['reason']}")
    if result["blocking_reasons"]:
        lines.append("")
        lines.append("### 阻断原因")
        for reason in result["blocking_reasons"]:
            lines.append(f"- {reason}")
    lines.append("")
    lines.append("### 下一步")
    for action in result["next_actions"]:
        lines.append(f"- {action}")
    return "\n".join(lines)


def example_payload() -> Dict[str, Any]:
    return {
        "asof": "2026-06-03",
        "strategy_id": "chanlun_third_buy_loose",
        "phase": "pre_oos",
        "rules_locked": True,
        "has_costs": True,
        "reports_all_variants": True,
        "controls": ["random_entry", "simple_breakout", "buy_hold"],
        "stat_tests": ["t_test", "bootstrap", "permutation"],
        "oos_run_count": 0,
        "changed_after_oos": False,
    }


def load_payload(path: Optional[str], use_example: bool) -> Dict[str, Any]:
    if use_example:
        return example_payload()
    if path:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    if not sys.stdin.isatty():
        return json.load(sys.stdin)
    return example_payload()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="缠论/打板离线研究闸门")
    parser.add_argument("--input", help="JSON input file. If omitted, reads stdin when piped.")
    parser.add_argument("--example", action="store_true", help="Run with built-in example payload.")
    parser.add_argument("--json", action="store_true", help="Output JSON.")
    parser.add_argument("--register", action="store_true",
                        help="把闸门结论登记进 strategy_registry（供实时 Agent 裁决是否计权）。")
    args = parser.parse_args()

    output = evaluate_gate(load_payload(args.input, args.example))
    if args.register:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "common"))
        import strategy_registry  # noqa: E402
        rec = strategy_registry.register_gate_result(output["strategy_id"], output)
        output["registered"] = {
            "strategy_id": rec["strategy_id"],
            "allowed_in_live_agent": rec["allowed_in_live_agent"],
            "gating_status": rec.get("gating_status"),
        }
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(format_report(output))
