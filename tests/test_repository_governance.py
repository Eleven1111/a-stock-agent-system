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
