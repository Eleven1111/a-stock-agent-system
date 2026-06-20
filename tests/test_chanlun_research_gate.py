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
