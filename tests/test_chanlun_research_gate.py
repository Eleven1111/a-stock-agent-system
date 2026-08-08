import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "chanlun-backtest" / "scripts" / "research_gate.py"
SPEC = importlib.util.spec_from_file_location("research_gate", SCRIPT)
research_gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(research_gate)


def test_example_payload_is_ready_for_oos():
    result = research_gate.evaluate_gate(research_gate.example_payload())

    assert result["decision"] == "ready_for_oos"
    assert result["allowed_in_live_agent"] is False
    assert not result["blocking_reasons"]


def test_missing_controls_blocks_research_gate():
    payload = research_gate.example_payload()
    payload["controls"] = ["random_entry"]

    result = research_gate.evaluate_gate(payload)

    assert result["decision"] == "blocked"
    assert result["allowed_in_live_agent"] is False
    assert any("simple_breakout" in reason for reason in result["blocking_reasons"])


def test_significant_oos_result_can_be_used_as_reference_only():
    payload = research_gate.example_payload()
    payload.update(
        {
            "phase": "oos_complete",
            "oos_run_count": 1,
            "permutation_p": 0.03,
            "fdr_p": 0.08,
            "oos_alpha": 0.12,
            "benchmark_alpha": 0.02,
        }
    )

    result = research_gate.evaluate_gate(payload)

    assert result["decision"] == "blocked"
    assert result["allowed_in_live_agent"] is False
    assert any("evidence" in reason.lower() for reason in result["blocking_reasons"])


def test_verified_oos_artifact_can_be_used_as_reference(tmp_path):
    artifact_module_path = (
        Path(__file__).resolve().parents[1] / "skills" / "common" / "research_artifact.py"
    )
    artifact_spec = importlib.util.spec_from_file_location(
        "research_artifact_for_gate_test", artifact_module_path
    )
    artifact_module = importlib.util.module_from_spec(artifact_spec)
    artifact_spec.loader.exec_module(artifact_module)
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps({"rows": [1, 2, 3]}), encoding="utf-8")
    artifact_path = tmp_path / "evidence.json"
    artifact = artifact_module.write_artifact(
        str(artifact_path),
        input_path=str(input_path),
        strategy_id="verified_strategy",
        rules={"entry": "next_open", "exit": "t_plus_1_close"},
        result={"summary": "fixture"},
        gate_metrics={
            "permutation_p": 0.03,
            "fdr_p": 0.08,
            "oos_alpha": 0.12,
            "benchmark_alpha": 0.02,
            "oos_sample_count": 60,
        },
        control_counts={
            "random_entry": 60,
            "simple_breakout": 60,
            "buy_hold": 60,
        },
    )
    payload = research_gate.example_payload()
    payload.update(
        {
            "strategy_id": "verified_strategy",
            "phase": "oos_complete",
            "oos_run_count": 1,
            "permutation_p": 0.03,
            "fdr_p": 0.08,
            "oos_alpha": 0.12,
            "benchmark_alpha": 0.02,
            "oos_sample_count": 60,
            "evidence_artifact": str(artifact_path),
            "evidence_sha256": artifact["artifact_sha256"],
        }
    )

    result = research_gate.evaluate_gate(payload)

    assert result["decision"] == "passed_for_reference"
    assert result["allowed_in_live_agent"] is True


def test_oos_rule_change_blocks_result():
    payload = research_gate.example_payload()
    payload.update({"phase": "oos_complete", "oos_run_count": 1, "changed_after_oos": True})

    result = research_gate.evaluate_gate(payload)

    assert result["decision"] == "blocked"
    assert any("OOS" in reason for reason in result["blocking_reasons"])


def test_oos_result_requires_minimum_sample_when_declared():
    payload = research_gate.example_payload()
    payload.update(
        {
            "phase": "oos_complete",
            "oos_run_count": 1,
            "permutation_p": 0.01,
            "fdr_p": 0.02,
            "oos_alpha": 0.03,
            "benchmark_alpha": 0.0,
            "min_oos_samples": 30,
            "oos_sample_count": 12,
        }
    )

    result = research_gate.evaluate_gate(payload)

    assert result["decision"] == "blocked"
    assert any("样本" in reason for reason in result["blocking_reasons"])


def _cross_sectional(direction, cohorts=5, n=120):
    """构造 cohorts 个互不重叠窗口的横截面证据。"""
    out = []
    for k in range(cohorts):
        src = f"2026-{7 + (k * 8) // 30:02d}-{1 + (k * 8) % 30:02d}"
        dst = f"2026-{7 + (k * 8 + 6) // 30:02d}-{1 + (k * 8 + 6) % 30:02d}"
        pairs = [
            [float(n - i), 0.001 * (n - i) if direction == "aligned" else 0.001 * i]
            for i in range(n)
        ]
        out.append({"src": src, "dst": dst, "pairs": pairs})
    return out


def test_cross_sectional_strategy_without_direction_evidence_is_blocked():
    """声明为横截面打分的策略，必须提交方向证据，否则阻断。

    trend_score 正是从这个盲区漏过去的：事件级 T+1/T+3 判定回答不了
    「高分是否真的比低分好」。
    """
    payload = research_gate.example_payload()
    payload.update({"strategy_kind": "cross_sectional_score"})

    checks = {item["id"]: item for item in research_gate.phase_checklist(payload)}

    assert checks["cross_sectional_direction"]["passed"] is False
    assert "缺少横截面方向证据" in checks["cross_sectional_direction"]["reason"]


def test_inverted_cross_sectional_direction_blocks_the_gate():
    payload = research_gate.example_payload()
    payload.update({
        "strategy_kind": "cross_sectional_score",
        "cross_sectional_cohorts": _cross_sectional("inverted"),
    })

    checks = {item["id"]: item for item in research_gate.phase_checklist(payload)}
    result = research_gate.evaluate_gate(payload)

    assert checks["cross_sectional_direction"]["passed"] is False
    assert "direction_inverted" in checks["cross_sectional_direction"]["reason"]
    assert result["decision"] == "blocked"


def test_confirmed_cross_sectional_direction_passes_the_check():
    payload = research_gate.example_payload()
    payload.update({
        "strategy_kind": "cross_sectional_score",
        "cross_sectional_cohorts": _cross_sectional("aligned"),
    })

    checks = {item["id"]: item for item in research_gate.phase_checklist(payload)}

    assert checks["cross_sectional_direction"]["passed"] is True
    assert "direction_confirmed" in checks["cross_sectional_direction"]["reason"]


def test_event_strategies_are_not_affected_by_the_new_check():
    """事件级策略（缠论买卖点等）没有横截面打分，不该被这条检查误伤。"""
    payload = research_gate.example_payload()

    ids = {item["id"] for item in research_gate.phase_checklist(payload)}

    assert "cross_sectional_direction" not in ids
