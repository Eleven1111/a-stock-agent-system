import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "common"))

from morning_note import build_morning_note, render_morning_note_markdown


def test_morning_note_no_material_information_says_no_change():
    note = build_morning_note(trading_date="2026-07-07", missing_inputs=["global-evening"])

    assert note["schema"] == "morning_note_v1"
    assert note["top_call"] == "隔夜无重大变化，维持原计划"
    assert note["overnight_developments"] == ["隔夜无重大变化"]
    assert note["has_signal"] is False
    assert "global-evening" in note["missing_inputs"]


def test_morning_note_markdown_contains_sections():
    note = build_morning_note(
        trading_date="2026-07-07",
        global_context={"events": [{"title": "美股科技股反弹"}]},
        company_events_context={"opportunities": [{"name": "测试股份", "event_label": "回购增持", "suggestion": "watch"}]},
        behavioral_context={"sentiment_phase": "caution"},
    )
    markdown = render_morning_note_markdown(note)

    assert "## 2026-07-07 Morning Note" in markdown
    assert "**Top Call**" in markdown
    assert "美股科技股反弹" in markdown
    assert "公司事件" in markdown
