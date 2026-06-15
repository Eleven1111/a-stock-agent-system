from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTRUCTION_FILES = (
    ROOT / "AGENTS.md",
    ROOT / "skills" / "stock-triage" / "AGENTS.md",
    ROOT / "skills" / "stock-triage" / "SKILL.md",
)
FORBIDDEN_DYNAMIC_PREFERENCE_MARKERS = (
    "深度参与 A 股，关注板块",
    "跟踪标的：",
    "**用户关注标的**",
    "**跟踪板块**",
)


def test_dynamic_market_preferences_are_not_hardcoded_in_instructions():
    for path in INSTRUCTION_FILES:
        content = path.read_text(encoding="utf-8")
        found = [marker for marker in FORBIDDEN_DYNAMIC_PREFERENCE_MARKERS if marker in content]
        assert not found, f"{path.relative_to(ROOT)} hardcodes dynamic preferences: {found}"
