import json

import pytest

import research_bus as bus
from scripts import theme_strength_daily as daily


@pytest.fixture(autouse=True)
def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    return tmp_path / "state"


@pytest.fixture(autouse=True)
def research_config(tmp_path, monkeypatch):
    config = {
        "task_kinds": {
            "theme_review": {
                "experts": ["evidence_auditor", "thesis_builder", "risk_redteam"],
                "priority": 65,
                "cooldown_days": 7,
                "pack_budget_chars": 20000,
                "pack_jobs": [],
                "required_sections": [],
            },
        },
        "experts": {
            "evidence_auditor": {"max_output_chars": 4000},
            "thesis_builder": {"max_output_chars": 5000},
            "risk_redteam": {"max_output_chars": 5000},
        },
        "budget": {"daily_char_budget": 0, "instructions_chars_estimate": 3000},
    }
    path = tmp_path / "research_committee.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("A_STOCK_RESEARCH_CONFIG", str(path))
    return config


THEME = {
    "id": "theme-solid-state-battery",
    "name": "固态电池",
    "members": ["300750", "002074"],
    "status": "mainline",
}


def test_mainline_theme_enqueues_theme_review_once():
    first = daily._enqueue_theme_review(THEME, "2026-07-03")
    assert first is not None
    tasks = bus.load_tasks()
    assert len(tasks) == 1
    task = tasks[0]
    assert task["kind"] == "theme_review"
    assert task["subject"]["theme"] == "固态电池"
    assert task["status"] == "pending"

    repeat = daily._enqueue_theme_review(THEME, "2026-07-03")
    assert repeat is None or not repeat.get("enqueued")
    assert len(bus.load_tasks()) == 1


def test_theme_review_respects_cooldown_across_days():
    daily._enqueue_theme_review(THEME, "2026-07-03")
    task_id = bus.load_tasks()[0]["id"]
    bus.update_task(task_id, {"status": "done"})

    within_cooldown = daily._enqueue_theme_review(THEME, "2026-07-06")
    assert within_cooldown is None or not within_cooldown.get("enqueued")
    assert len(bus.load_tasks()) == 1


def test_enqueue_degrades_gracefully_without_kind(tmp_path, monkeypatch):
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps({"task_kinds": {}}), encoding="utf-8")
    monkeypatch.setenv("A_STOCK_RESEARCH_CONFIG", str(bare))
    outcome = daily._enqueue_theme_review(THEME, "2026-07-03")
    assert outcome is None
    assert bus.load_tasks() == []


def test_run_fails_closed_without_discovery_inputs(monkeypatch):
    monkeypatch.setattr(daily, "_load_discovery_inputs", lambda asof: None)
    monkeypatch.setattr(
        "theme_registry.active_themes", lambda asof: [dict(THEME)],
    )
    result = daily.run(trading_date="2026-07-03")
    assert result["status"] == "no_inputs"
    assert result["has_signal"] is False
    assert result["themes"] == []
    assert bus.load_tasks() == []
