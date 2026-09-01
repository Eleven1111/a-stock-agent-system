from __future__ import annotations

import pathlib
import re

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]

# 职责重叠、最容易被误路由的四个技能：都触及涨停与短线。
OVERLAPPING_SKILLS = (
    "hot-money-tactics",
    "daban-stock-picker",
    "stock-triage",
    "research-committee",
)
WHEN_NOT_HEADING = "## 何时不用本技能"
# 反引号里长得像技能名的 token：kebab-case，不含路径、调用、赋值或下划线。
SKILL_NAME_SHAPE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)+$")


def _installed_skills() -> set[str]:
    return {path.parent.name for path in ROOT.glob("skills/*/SKILL.md")}


def _when_not_section(skill: str) -> str:
    """Return the body of the skill's 何时不用 section, heading excluded."""
    text = (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    start = text.find(WHEN_NOT_HEADING)
    assert start != -1, f"{skill} 缺少「{WHEN_NOT_HEADING}」段"
    rest = text[start + len(WHEN_NOT_HEADING):]
    following = re.search(r"^## ", rest, re.MULTILINE)
    return (rest[: following.start()] if following else rest).strip()


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
    init_revision = next(
        value.rsplit("@", 1)[1]
        for value in uses
        if value.startswith("github/codeql-action/init@")
    )
    analyze_revision = next(
        value.rsplit("@", 1)[1]
        for value in uses
        if value.startswith("github/codeql-action/analyze@")
    )
    assert init_revision == analyze_revision


def test_overlapping_skills_document_when_not_to_apply():
    """四个职责重叠的技能必须写明「何时不用」，且该段不能被掏空。

    段落被删、被清空、或退化成一张没有路由行的空表，都要变红——否则文档漂移
    不会有任何信号。
    """
    installed = _installed_skills()
    assert installed, "skills/*/SKILL.md 一个都没扫到，样本为空则下面的断言恒真"
    missing = [name for name in OVERLAPPING_SKILLS if name not in installed]
    assert not missing, f"待校验技能不存在: {missing}"

    for skill in OVERLAPPING_SKILLS:
        body = _when_not_section(skill)
        rows = [
            line
            for line in body.splitlines()
            if line.startswith("|") and "---" not in line
        ]
        # 首行是表头，其余是路由行。
        assert len(rows) >= 4, f"{skill} 的何时不用段路由行不足: {len(rows) - 1}"
        assert len(body) >= 200, f"{skill} 的何时不用段被掏空: {len(body)} 字符"


def test_when_not_sections_only_route_to_skills_that_exist():
    """路由目标必须真实存在，否则改名或删技能会留下指向空气的指路牌。"""
    installed = _installed_skills()
    assert installed, "skills/*/SKILL.md 一个都没扫到，样本为空则下面的断言恒真"

    for skill in OVERLAPPING_SKILLS:
        body = _when_not_section(skill)
        referenced = {
            token
            for token in re.findall(r"`([^`]+)`", body)
            if SKILL_NAME_SHAPE.match(token)
        }
        dangling = sorted(referenced - installed)
        assert not dangling, f"{skill} 的何时不用段指向不存在的技能: {dangling}"
        # 「不要用我」必须同时回答「那用什么」，只否定不导流等于没写。
        assert referenced - {skill}, f"{skill} 的何时不用段没有指向任何其他技能"


FALSIFIED_LEDGER = ROOT / "docs" / "falsified-approaches.md"


def test_falsified_ledger_is_reachable_from_the_change_protocol():
    """台账没有入口就等于不存在——它记录的教训本身就是这个形状。"""
    assert FALSIFIED_LEDGER.is_file()
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "docs/falsified-approaches.md" in agents
    protocol = agents[agents.index("## Change Protocol"):]
    protocol = protocol[: protocol.find("\n## ", 1)]
    assert "falsified-approaches.md" in protocol


def test_falsified_ledger_index_and_detail_sections_agree():
    """索引与详情必须双向对齐。

    只加详情不加索引 = 后来的人扫不到它；只留索引不留详情 = 指向空气。两种漂移
    都要变红。
    """
    text = FALSIFIED_LEDGER.read_text(encoding="utf-8")
    indexed = re.findall(r"^\| (F\d{3}) \|", text, re.MULTILINE)
    detailed = re.findall(r"^## (F\d{3}) · ", text, re.MULTILINE)

    assert indexed, "索引表一行都没扫到，样本为空则下面的断言恒真"
    assert len(indexed) == len(set(indexed)), f"索引 ID 重复: {indexed}"
    assert len(detailed) == len(set(detailed)), f"详情 ID 重复: {detailed}"
    assert set(indexed) == set(detailed), (
        f"索引与详情不匹配: 只在索引 {sorted(set(indexed) - set(detailed))}, "
        f"只在详情 {sorted(set(detailed) - set(indexed))}"
    )


def test_every_falsified_entry_says_what_not_to_retry():
    """只说「A 不行」没有价值，每条必须回答「别再试什么」。"""
    text = FALSIFIED_LEDGER.read_text(encoding="utf-8")
    sections = re.split(r"^## (F\d{3}) · ", text, flags=re.MULTILINE)[1:]
    bodies = dict(zip(sections[::2], sections[1::2]))
    assert bodies, "详情段一个都没扫到，样本为空则下面的断言恒真"
    for entry_id, body in bodies.items():
        assert "别再试" in body, f"{entry_id} 没有写「别再试」"
        assert len(body.strip()) >= 200, f"{entry_id} 详情被掏空: {len(body.strip())} 字符"


def test_versioned_main_ruleset_has_no_bypass_and_requires_ci_and_pr():
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
    assert pull_request["required_approving_review_count"] == 0
    assert pull_request["dismiss_stale_reviews_on_push"] is False
    assert pull_request["require_last_push_approval"] is False
    assert pull_request["required_review_thread_resolution"] is True
    required = rules["required_status_checks"]["parameters"]
    assert required["strict_required_status_checks_policy"] is True
    assert required["do_not_enforce_on_create"] is False
    assert [item["context"] for item in required["required_status_checks"]] == [
        "Python 3.10",
        "Python 3.13",
    ]
