"""Cron Manifest 校验测试"""

import json
import os
import tempfile
from scripts.validate_cron_manifest import validate


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

VALID_JOB = {
    "id": "test-job",
    "name": "Test",
    "schedule": "0 9 * * 1-5",
    "timezone": "Asia/Shanghai",
    "command": "python scripts/run_agent_dag.py test-job --emit-target",
    "cwd": ".",
    "enabled": True,
    "silent_when_no_signal": True,
    "expected_output": "text",
    "external": True,
    "execution_mode": "isolated_subprocess",
    "context_scope": "cron",
    "deliver": "origin",
    "max_output_chars": 2000,
    "context_from": [],
    "artifact_path_template": "{cron_output_dir}/{job_id}/{run_id}.json",
    "allowed_state_writes": ["$A_STOCK_STATE_HOME/cron/output/test-job/"],
    "run": {
        "command": "python skills/stock-triage/scripts/context_digest.py --json",
        "cwd": ".",
        "timeout_seconds": 10,
    },
}


def test_valid_manifest():
    manifest = {"jobs": [VALID_JOB]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is True
    finally:
        os.unlink(path)


def test_missing_field():
    manifest = {"jobs": [{"id": "bad", "name": "Bad"}]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_duplicate_ids():
    j1 = dict(VALID_JOB)
    j2 = dict(VALID_JOB)
    manifest = {"jobs": [j1, j2]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_placeholders_are_rejected_without_vars():
    j = dict(VALID_JOB)
    j["command"] = "python scripts/run_agent_dag.py test-job --emit-target --var code={code}"
    manifest = {"jobs": [j]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_placeholders_are_rejected_even_with_vars():
    j = dict(VALID_JOB)
    j["command"] = "python scripts/run_agent_dag.py test-job --var code={code}"
    j["template_vars"] = ["code"]
    manifest = {"jobs": [j]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_run_command_placeholders_are_rejected():
    """run.command 也必须自包含，不能依赖 Gateway 动态注入。"""
    j = dict(VALID_JOB)
    j["run"] = dict(VALID_JOB["run"])
    j["run"]["command"] = "python script.py {code}"
    manifest = {"jobs": [j]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_template_vars_are_rejected_without_placeholders():
    j = dict(VALID_JOB)
    j["template_vars"] = ["code", "name"]
    manifest = {"jobs": [j]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_direct_business_command_rejected():
    """Hermes command 必须先进 runner，不能直接跑业务脚本污染主上下文。"""
    j = dict(VALID_JOB)
    j["command"] = "python skills/stock-triage/scripts/intraday_monitor.py --json"
    manifest = {"jobs": [j]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_business_state_writes_cannot_use_hermes_install_home():
    j = dict(VALID_JOB)
    j["allowed_state_writes"] = ["$HERMES_HOME/cron/output/test-job/"]
    manifest = {"jobs": [j]}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_top_level_duplicate_runtime_script_rejected():
    """run.command 必须指向 canonical skills 路径，不能用顶层重复脚本。"""
    j = dict(VALID_JOB)
    j["run"] = dict(VALID_JOB["run"])
    j["run"]["command"] = "python scripts/intraday_monitor.py --json"
    manifest = {"jobs": [j]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_missing_isolation_contract_rejected():
    j = dict(VALID_JOB)
    del j["context_scope"]
    manifest = {"jobs": [j]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_invalid_dependency_policy_rejected():
    j = dict(VALID_JOB)
    j["context_from"] = ["upstream"]
    j["dependency_policy"] = {
        "required": True,
        "max_age_minutes": 0,
        "trading_date": "tomorrow",
    }
    manifest = {"jobs": [j]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_unknown_dependency_rejected():
    j = dict(VALID_JOB)
    j["context_from"] = ["does-not-exist"]
    manifest = {"jobs": [j]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_dependency_cycle_rejected():
    first = dict(VALID_JOB)
    first["id"] = "first"
    first["command"] = "python scripts/run_agent_dag.py first --emit-target"
    first["context_from"] = ["second"]
    second = dict(VALID_JOB)
    second["id"] = "second"
    second["command"] = "python scripts/run_agent_dag.py second --emit-target"
    second["context_from"] = ["first"]
    manifest = {"jobs": [first, second]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_repo_manifest_keeps_runtime_isolation_contract():
    manifest_path = os.path.join(ROOT, "cron", "hermes-cron-manifest.json")
    assert validate(manifest_path) is True

    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    jobs = {job["id"]: job for job in manifest["jobs"]}

    for required in [
        "hot-money-context",
        "candidate-discovery",
        "auction-snapshot",
        "auction-finalize",
        "open-confirmation",
        "closing-triage",
    ]:
        assert required in jobs
        assert jobs[required]["command"] == (
            f"python scripts/run_agent_dag.py {required} --emit-target"
        )
        assert jobs[required]["context_scope"] == "cron"

    assert "--codes" not in jobs["auction-snapshot"]["run"]["command"]
    assert "--codes" not in jobs["auction-finalize"]["run"]["command"]
    assert "--codes" not in jobs["open-confirmation"]["run"]["command"]
    assert jobs["candidate-discovery"]["context_from"][0] == "hot-money-context"
    assert jobs["hot-money-context"]["run"]["command"].endswith("--cache-only")
    assert jobs["auction-snapshot"]["context_from"] == ["candidate-discovery"]
    assert jobs["open-confirmation"]["context_from"] == ["auction-finalize"]
    assert jobs["auction-finalize"]["schedule"] == "26 9 * * 1-5"
    assert any("monitor_registry.json" in path for path in jobs["auction-finalize"]["allowed_state_writes"])
    assert any("signal_ledger.jsonl" in path for path in jobs["auction-finalize"]["allowed_state_writes"])
    assert any("recommendations.json" in path for path in jobs["open-confirmation"]["allowed_state_writes"])
    assert any("signal_ledger.jsonl" in path for path in jobs["open-confirmation"]["allowed_state_writes"])
    assert any("signal_ledger.jsonl" in path for path in jobs["intraday-alert"]["allowed_state_writes"])
    assert set(jobs["closing-triage"]["context_from"]) >= {"four-dim-scorer", "portfolio-check"}
    assert jobs["performance-weekly"]["dependency_policy"]["trading_date"] == "same_trading_date"
    assert jobs["performance-weekly"]["context_from"] == ["performance-daily"]
    assert jobs["performance-weekly"]["run"]["command"].endswith("--json --gate")
    assert any("strategy_registry.json" in path for path in jobs["performance-weekly"]["allowed_state_writes"])
    assert jobs["performance-daily"]["run"]["command"].endswith("--json")
    assert jobs["ledger-projector"]["run"]["command"].startswith("python scripts/agent_state_projector.py")
