import sys
import os

import pytest

BASE = os.path.dirname(__file__)
PROJ = os.path.abspath(os.path.join(BASE, '..'))
sys.path.insert(0, os.path.join(PROJ, 'skills', 'stock-triage', 'scripts'))
sys.path.insert(0, os.path.join(PROJ, 'skills', 'common'))
sys.path.insert(0, PROJ)


@pytest.fixture
def verified_gate_factory(tmp_path):
    """Create a gate result backed by a real, verifiable research artifact."""
    from research_artifact import write_artifact

    counter = 0

    def _build(strategy_id, *, allowed=True):
        nonlocal counter
        counter += 1
        source = tmp_path / f"research-input-{counter}.json"
        source.write_text('{"fixture":true}', encoding="utf-8")
        alpha = 0.02 if allowed else -0.01
        metrics = {
            "permutation_p": 0.01,
            "fdr_p": 0.02,
            "oos_alpha": alpha,
            "benchmark_alpha": 0.0,
            "oos_sample_count": 100,
        }
        artifact_path = tmp_path / f"research-artifact-{counter}.json"
        artifact = write_artifact(
            str(artifact_path),
            input_path=str(source),
            strategy_id=strategy_id,
            rules={"version": "fixture-v1"},
            result={"strategy_id": strategy_id, "metrics": metrics},
            gate_metrics=metrics,
            control_counts={"benchmark": 100},
        )
        return {
            "strategy_id": strategy_id,
            "decision": "passed_for_reference" if allowed else "failed",
            "allowed_in_live_agent": allowed,
            "asof": "2026-06-03",
            "stats": metrics,
            "evidence": {
                "verified": True,
                "artifact": str(artifact_path),
                "sha256": artifact["artifact_sha256"],
            },
        }

    return _build
