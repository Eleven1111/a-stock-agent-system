from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTRUCTION_FILES = tuple(
    path
    for path in sorted(ROOT.rglob("*.md"))
    if ".git" not in path.parts and ".pytest_cache" not in path.parts
)
REDUNDANT_SCOPED_AGENT_FILE = ROOT / "skills" / "stock-triage" / "AGENTS.md"
FORBIDDEN_DYNAMIC_PREFERENCE_MARKERS = (
    "深度参与 A 股，关注板块",
    "跟踪标的：",
    "**用户关注标的**",
    "**跟踪板块**",
    "## 用户偏好",
    "用户明确偏好",
    "跟踪标的代码",
    "用户核心跟踪标的",
    "用户历史关注标的",
    "来自记忆",
    "当前跟踪：",
    "从 memory 中读取用户",
)


def test_dynamic_market_preferences_are_not_hardcoded_in_instructions():
    for path in INSTRUCTION_FILES:
        content = path.read_text(encoding="utf-8")
        found = [marker for marker in FORBIDDEN_DYNAMIC_PREFERENCE_MARKERS if marker in content]
        assert not found, f"{path.relative_to(ROOT)} hardcodes dynamic preferences: {found}"


def test_stock_triage_does_not_duplicate_root_agent_policy():
    assert not REDUNDANT_SCOPED_AGENT_FILE.exists()


def test_root_agent_contract_stays_compact_and_runtime_neutral():
    content = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert len(content.splitlines()) <= 140
    for marker in (
        "事故修复",
        "模块整合数据流",
        "delegate_task",
        "deepseek-v4",
        "qwen/qwen",
    ):
        assert marker not in content


def test_legacy_cron_wrappers_are_not_restored():
    wrappers = ROOT / "cron" / "wrappers"
    assert not wrappers.exists() or not any(wrappers.iterdir())
