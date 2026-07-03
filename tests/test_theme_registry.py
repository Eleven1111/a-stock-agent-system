"""L3 theme registry: registration, fail-closed membership, tombstone."""

from __future__ import annotations

import pytest


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    import importlib

    import theme_registry as module

    importlib.reload(module)
    return module


def test_register_creates_theme_with_members_and_evidence(registry):
    out = registry.register(
        "机器人概念",
        ["sh600000", "000001", "000001"],
        source_evidence=[{"kind": "limitup_commonality", "detail": "3板共性", "asof": "2026-07-03"}],
    )
    assert out["changed"] is True
    theme = registry.get_theme("theme:机器人概念")
    assert theme["members"] == ["600000", "000001"]
    assert theme["member_count"] == 2
    assert theme["status"] == "emerging"
    assert theme["source_evidence"][0]["kind"] == "limitup_commonality"


def test_empty_members_fail_closed(registry):
    out = registry.register("空主题", [])
    assert out["changed"] is False
    assert out["reason"] == "empty_members_fail_closed"
    assert registry.get_theme("theme:空主题") is None


def test_broad_label_rejected(registry):
    out = registry.register("制造业", ["000001"])
    assert out["changed"] is False
    assert out["reason"] == "broad_label_rejected"


def test_invalid_evidence_kind_dropped(registry):
    registry.register(
        "光伏",
        ["000002"],
        source_evidence=[{"kind": "bogus", "detail": "x"}, {"kind": "policy_pointer", "detail": "补贴"}],
    )
    theme = registry.get_theme("theme:光伏")
    kinds = [e["kind"] for e in theme["source_evidence"]]
    assert kinds == ["policy_pointer"]


def test_archived_theme_not_resurrected_by_automatic_register(registry):
    registry.register("短剧", ["000003"])
    registry.set_stage("theme:短剧", "archived", reason="weak_streak_archived")
    out = registry.register("短剧", ["000003", "000004"])
    assert out["changed"] is False
    assert out["reason"] == "archived_tombstone"
    assert registry.get_theme("theme:短剧")["status"] == "archived"


def test_force_revives_archived_theme(registry):
    registry.register("短剧", ["000003"])
    registry.set_stage("theme:短剧", "archived", reason="weak_streak_archived")
    out = registry.register("短剧", ["000003", "000004"], force=True)
    assert out["changed"] is True
    assert registry.get_theme("theme:短剧")["status"] == "emerging"


def test_set_stage_records_history_and_is_directional(registry):
    reg = registry.register("AI算力", ["000005"])
    theme_id = reg["theme"]["id"]
    registry.set_stage(theme_id, "mainline", reason="resonance_confirmed")
    registry.set_stage(theme_id, "archived", reason="weak_streak_archived")
    # archived is a tombstone: further stage moves are refused.
    out = registry.set_stage(theme_id, "mainline", reason="revive_attempt")
    assert out["changed"] is False
    theme = registry.get_theme(theme_id)
    stages = [h["to"] for h in theme["stage_history"]]
    assert stages == ["mainline", "archived"]


def test_theme_stage_by_sector_and_for_code(registry):
    registry.register("机器人", ["600000", "000001"])
    registry.set_stage("theme:机器人", "mainline", reason="resonance_confirmed")
    by_sector = registry.theme_stage_by_sector()
    assert by_sector["机器人"]["stage"] == "mainline"
    for_code = registry.theme_stage_for_code("sh600000")
    assert for_code["stage"] == "mainline"
    assert for_code["name"] == "机器人"


def test_theme_stage_for_code_archived_excluded(registry):
    registry.register("退潮主题", ["600001"])
    registry.set_stage("theme:退潮主题", "archived", reason="weak_streak_archived")
    assert registry.theme_stage_for_code("600001") is None
