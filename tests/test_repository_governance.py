from __future__ import annotations

import pathlib

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_public_repository_has_required_governance_files():
    for relative in (
        "LICENSE",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
        "constraints.txt",
        ".github/CODEOWNERS",
        ".github/workflows/codeql.yml",
        ".github/rulesets/main.json",
        "docs/issue-94-ci-governance.md",
    ):
        assert (ROOT / relative).is_file(), relative


def test_release_version_is_1_4_0_and_license_is_mit():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "1.4.0"' in pyproject
    assert 'license = {text = "MIT"}' in pyproject
    assert "MIT License" in (ROOT / "LICENSE").read_text(encoding="utf-8")


def test_ci_is_manual_and_has_stable_python_check_names():
    workflow = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    triggers = workflow["on"]
    assert "workflow_dispatch" in triggers
    jobs = workflow["jobs"]
    assert jobs["verify"]["name"] == "Python ${{ matrix.python-version }}"
    assert jobs["verify"]["strategy"]["matrix"]["python-version"] == [
        "3.10",
        "3.13",
    ]
    for job in jobs.values():
        action_steps = [step["uses"] for step in job["steps"] if step.get("uses")]
        assert all(len(value.rsplit("@", 1)[1]) == 40 for value in action_steps)
        install = next(
            step["run"] for step in job["steps"] if step.get("name") == "Install"
        )
        assert "-c constraints.txt" in install
        assert any(
            "check_maintainability_budget.py" in step.get("run", "")
            for step in job["steps"]
        )


def test_constraints_cover_every_direct_runtime_dependency():
    constraints = (ROOT / "constraints.txt").read_text(encoding="utf-8").lower()
    for package in ("yfinance", "akshare", "adata", "pandas", "numpy", "pyyaml"):
        assert f"{package}==" in constraints


def test_codeql_is_pinned_and_covers_pull_requests_and_main():
    workflow = yaml.load(
        (ROOT / ".github/workflows/codeql.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert set(workflow["on"]) == {
        "workflow_dispatch",
        "push",
        "pull_request",
        "schedule",
    }
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["on"]["pull_request"]["branches"] == ["main"]
    assert workflow["permissions"] == {
        "contents": "read",
        "packages": "read",
        "security-events": "write",
    }
    job = workflow["jobs"]["analyze"]
    assert job["name"] == "CodeQL (python)"
    uses = [step["uses"] for step in job["steps"] if step.get("uses")]
    assert all(len(value.rsplit("@", 1)[1]) == 40 for value in uses)
    assert any(value.startswith("github/codeql-action/init@") for value in uses)
    assert any(value.startswith("github/codeql-action/analyze@") for value in uses)


def test_versioned_main_ruleset_has_no_bypass_and_requires_ci_and_review():
    import json

    ruleset = json.loads(
        (ROOT / ".github/rulesets/main.json").read_text(encoding="utf-8")
    )
    assert ruleset["enforcement"] == "active"
    assert ruleset["bypass_actors"] == []
    assert ruleset["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]
    rules = {rule["type"]: rule for rule in ruleset["rules"]}
    assert {"deletion", "non_fast_forward", "pull_request", "required_status_checks"} <= set(rules)
    pull_request = rules["pull_request"]["parameters"]
    assert pull_request["required_approving_review_count"] == 1
    assert pull_request["dismiss_stale_reviews_on_push"] is True
    assert pull_request["require_last_push_approval"] is True
    assert pull_request["required_review_thread_resolution"] is True
    required = rules["required_status_checks"]["parameters"]
    assert required["strict_required_status_checks_policy"] is True
    assert required["do_not_enforce_on_create"] is False
    assert [item["context"] for item in required["required_status_checks"]] == [
        "Python 3.10",
        "Python 3.13",
    ]
