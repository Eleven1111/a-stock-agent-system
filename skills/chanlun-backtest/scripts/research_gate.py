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
import hashlib
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

# These IDs are ranking outputs even when their callers omit strategy_kind.
KNOWN_SCORE_STRATEGY_IDS = {
    "trend_pullback",
    "trend_score",
    "daban_score",
    "auction_score",
    "auction_daban_score",
    "auction_trend_score",
    "open_score",
    "open_daban_score",
    "open_trend_score",
    "leader_score",
    "mainline_score",
    "tail_close_score",
}
KNOWN_EVENT_STRATEGY_IDS = {
    "chanlun_first_buy",
    "chanlun_second_buy",
    "chanlun_third_buy",
    "chanlun_first_sell",
    "chanlun_second_sell",
    "chanlun_top_divergence",
    "daban:first_board_reseal",
    "daban:second_board_weak_to_strong",
    "first_board_reseal",
    "second_board_weak_to_strong",
    "首板回封",
    "二板弱转强",
}
VALID_STRATEGY_KINDS = {"event_signal", "cross_sectional_score"}


def _ensure_common_on_path() -> None:
    """skills/common 入 sys.path，只此一处（避免每个调用点各插一次）。"""
    common = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "common"))
    if common not in sys.path:
        sys.path.insert(0, common)


def _verify_evidence(payload: Dict[str, Any]) -> Dict[str, Any]:
    path = payload.get("evidence_artifact")
    if not path:
        return {
            "passed": False,
            "reason": "OOS evidence_artifact is required",
            "artifact": None,
            "sha256": None,
        }
    _ensure_common_on_path()
    from research_artifact import verify_artifact

    verification = verify_artifact(
        str(path),
        expected_sha256=payload.get("evidence_sha256"),
    )
    if not verification["valid"]:
        return {
            "passed": False,
            "reason": "OOS evidence verification failed: " + ",".join(verification["errors"]),
            "artifact": str(path),
            "sha256": payload.get("evidence_sha256"),
        }
    artifact = verification["artifact"]
    if str(artifact.get("strategy_id") or "") != str(payload.get("strategy_id") or ""):
        return {
            "passed": False,
            "reason": "OOS evidence strategy_id mismatch",
            "artifact": str(path),
            "sha256": artifact.get("artifact_sha256"),
        }
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
            return {
                "passed": False,
                "reason": f"OOS evidence metric mismatch: {field}",
                "artifact": str(path),
                "sha256": artifact.get("artifact_sha256"),
            }
    required_controls = _set(payload.get("controls"))
    counts = artifact.get("control_counts") or {}
    missing = sorted(name for name in required_controls if int(_num(counts.get(name), 0) or 0) <= 0)
    if missing:
        return {
            "passed": False,
            "reason": f"OOS evidence controls missing samples: {missing}",
            "artifact": str(path),
            "sha256": artifact.get("artifact_sha256"),
        }
    return {
        "passed": True,
        "reason": "OOS evidence artifact verified",
        "artifact": str(path),
        "sha256": artifact.get("artifact_sha256"),
    }


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


def _json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_json_object(path: Any) -> Optional[Dict[str, Any]]:
    if not path:
        return None
    try:
        with open(os.path.abspath(os.path.expanduser(str(path))), encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _contains_score_field(value: Any) -> bool:
    """Detect score-bearing evidence without treating ordinary text as a score."""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in {"score", "scores"} or normalized.endswith("_score"):
                return True
            if _contains_score_field(child):
                return True
    elif isinstance(value, list):
        return any(_contains_score_field(item) for item in value)
    return False


def _looks_like_score_strategy(strategy_id: Any) -> bool:
    normalized = str(strategy_id or "").strip().lower()
    if not normalized:
        return False
    if normalized in KNOWN_SCORE_STRATEGY_IDS:
        return True
    # Custom ranking IDs must not be able to hide behind an omitted kind.
    tokens = {token for token in normalized.replace(":", "_").replace("-", "_").split("_") if token}
    return bool(tokens & {"score", "scores", "rank", "ranking", "factor"})


def _is_known_event_strategy(strategy_id: Any) -> bool:
    normalized = str(strategy_id or "").strip().lower()
    return normalized in {item.lower() for item in KNOWN_EVENT_STRATEGY_IDS} or normalized.startswith("chanlun_")


def _identify_strategy_kind(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve strategy kind fail-closed, with score evidence taking precedence."""
    strategy_id = payload.get("strategy_id")
    artifact = _load_json_object(payload.get("evidence_artifact"))
    score_evidence = _contains_score_field(artifact)
    submitted_cohorts = "cross_sectional_cohorts" in payload
    score_id = _looks_like_score_strategy(strategy_id)
    explicit = payload.get("strategy_kind")
    explicit_kind = str(explicit).strip() if isinstance(explicit, str) else ""
    score_detected = score_id or submitted_cohorts or score_evidence

    if score_detected:
        if explicit_kind and explicit_kind != "cross_sectional_score":
            return {
                "kind": "cross_sectional_score",
                "declared": bool(explicit_kind),
                "conflict": True,
                "source": "score_evidence_conflict",
                "reason": "strategy_kind 与打分型策略/证据冲突，必须声明 cross_sectional_score",
            }
        source = (
            "known_score_id" if score_id else
            "cross_sectional_cohorts" if submitted_cohorts else "score_evidence"
        )
        return {
            "kind": "cross_sectional_score",
            "declared": bool(explicit_kind),
            "source": source,
            "reason": (
                "已识别为横截面打分策略，但必须显式声明 strategy_kind=cross_sectional_score"
                if not explicit_kind else "已识别为横截面打分策略"
            ),
        }

    if explicit_kind in VALID_STRATEGY_KINDS:
        return {
            "kind": explicit_kind,
            "declared": True,
            "source": "explicit",
            "reason": f"显式声明 strategy_kind={explicit_kind}",
        }
    if explicit_kind:
        return {
            "kind": None,
            "declared": True,
            "source": "unknown_explicit_kind",
            "reason": f"未知 strategy_kind={explicit_kind}",
        }
    if _is_known_event_strategy(strategy_id):
        return {
            "kind": "event_signal",
            "declared": False,
            "source": "known_event_id",
            "reason": "已识别为事件型策略",
        }
    return {
        "kind": None,
        "declared": False,
        "source": "missing_kind",
        "reason": "缺少可判定的 strategy_kind，不能按 event_signal 静默放行",
    }


def _direction_verdict_sha256(evidence_sha256: str, result: Dict[str, Any]) -> str:
    return _json_sha256({"evidence_sha256": evidence_sha256, "result": result})


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
    kind = _identify_strategy_kind(payload)
    checks.append({
        "id": "strategy_kind",
        "passed": (
            kind["kind"] in VALID_STRATEGY_KINDS
            and (kind["kind"] != "cross_sectional_score" or kind["declared"])
            and not kind.get("conflict", False)
        ),
        "reason": kind["reason"],
        "detail": kind,
    })
    direction = _cross_sectional_check(payload)
    if direction is not None:
        checks.append(direction)
    return checks


def _cross_sectional_check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """横截面打分必须证明「高分优于低分」。

    事件级 T+1/T+3 判定回答不了排序方向——trend_score 正是从这个盲区漏过去的
    （2026-08-08 实测中窗口 rank IC -0.34、8/8 队列全负）。对识别为
    ``cross_sectional_score`` 的策略生效，事件级策略（缠论买卖点等）不受影响。
    """
    kind = _identify_strategy_kind(payload)
    if kind["kind"] != "cross_sectional_score":
        return None
    cohorts = payload.get("cross_sectional_cohorts")
    if not isinstance(cohorts, list) or not cohorts:
        return {
            "id": "cross_sectional_direction",
            "passed": False,
            "reason": "缺少横截面方向证据：声明为打分类策略必须提交 cross_sectional_cohorts",
        }
    _ensure_common_on_path()
    import cross_sectional_direction

    result = cross_sectional_direction.evaluate(cohorts)
    evidence_sha256 = str(payload.get("evidence_sha256") or _json_sha256(cohorts))
    verdict_sha256 = _direction_verdict_sha256(evidence_sha256, result)
    return {
        "id": "cross_sectional_direction",
        "passed": result["passed"],
        "reason": (
            f"{result['verdict']}: mean_ic={result['mean_ic']} "
            f"同号占比={result['positive_ic_ratio']} "
            f"可用队列={result['usable_cohorts']} 独立队列={result['independent_cohorts']}"
        ),
        "detail": result,
        "direction_verdict": {
            "schema": "cross_sectional_direction_binding_v1",
            "verdict": result["verdict"],
            "passed": result["passed"],
            "evidence_sha256": evidence_sha256,
            "verdict_sha256": verdict_sha256,
            "result": result,
        },
    }


def evaluate_gate(payload: Dict[str, Any]) -> Dict[str, Any]:
    checklist = phase_checklist(payload)
    kind = _identify_strategy_kind(payload)
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

    evidence_check = next(
        (item for item in checklist if item.get("id") == "evidence_artifact"),
        None,
    )
    direction_check = next(
        (item for item in checklist if item.get("id") == "cross_sectional_direction"),
        None,
    )

    return {
        "schema": "chanlun_research_gate_v1",
        "generated_at": datetime.now().isoformat(),
        "asof": payload.get("asof") or date.today().isoformat(),
        "engine": ENGINE,
        "strategy_id": payload.get("strategy_id", "unknown"),
        "strategy_kind": kind["kind"],
        "strategy_kind_source": kind["source"],
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
            "oos_sample_count": int(_num(payload.get("oos_sample_count"), 0) or 0),
        },
        "evidence": {
            "verified": bool(evidence_check and evidence_check.get("passed")),
            "artifact": payload.get("evidence_artifact"),
            "sha256": payload.get("evidence_sha256"),
            "reason": (evidence_check or {}).get("reason"),
        },
        "direction_verdict": (direction_check or {}).get("direction_verdict"),
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
        "strategy_kind": "event_signal",
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
