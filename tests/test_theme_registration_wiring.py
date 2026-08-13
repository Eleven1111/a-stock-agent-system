"""断链 2 接线：candidate_discovery.register_mainline_themes 主线板块注册。

theme_strength_daily 只评估已注册主题；注册表长期为空会让弱市交付门禁的
"窄主题前二龙头"豁免通道永远无证据可依。本组测试验证主线板块识别后
正确注册为 L3 动态主题（fail-closed、幂等刷新、宽标签拒绝）。
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def wiring(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    import theme_registry

    importlib.reload(theme_registry)

    import sys, os

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "stock-triage", "scripts"))
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "common"))
    import candidate_discovery as discovery

    yield theme_registry, discovery

    # 还原：把模块重新绑定回默认路径，避免污染后续测试
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    importlib.reload(theme_registry)


def _selection_state():
    return {
        "sectors": [
            {"sector": "通信设备", "rank": 1, "state": "confirmed",
             "limitup_count": 4, "evidence_count": 1, "evidence_types": ["limitup_cluster"]},
            {"sector": "通用设备", "rank": 2, "state": "emerging",
             "limitup_count": 8, "evidence_count": 2, "evidence_types": ["limitup_cluster", "sector_flow"]},
            {"sector": "半导体", "rank": 4, "state": "neutral",
             "limitup_count": 3, "evidence_count": 1, "evidence_types": ["limitup_cluster"]},
            {"sector": "专用设备", "rank": 3, "state": "weakening",
             "limitup_count": 8, "evidence_count": 1, "evidence_types": ["limitup_cluster"]},
        ],
        "stock_sectors": {
            "000001": "通信设备", "000002": "通信设备", "000003": "通信设备",
            "002001": "通用设备", "002002": "通用设备",
            "600001": "半导体",
            "601001": "专用设备",
        },
    }


def test_mainline_sectors_registered_with_members(wiring):
    registry, discovery = wiring
    summary = discovery.register_mainline_themes(_selection_state(), event_asof="2026-08-13")

    registered = {r["sector"] for r in summary["registered"]}
    assert registered == {"通信设备", "通用设备"}

    theme = registry.get_theme("theme:通信设备")
    assert theme is not None
    assert sorted(theme["members"]) == ["000001", "000002", "000003"]
    assert theme["status"] == "emerging"
    assert theme["source_evidence"][0]["kind"] == "limitup_commonality"

    # neutral / weakening 板块不进注册表
    assert registry.get_theme("theme:半导体") is None
    assert registry.get_theme("theme:专用设备") is None


def test_no_evidence_mainline_skipped(wiring):
    registry, discovery = wiring
    state = _selection_state()
    state["sectors"][0]["evidence_count"] = 0  # 量能主导、无共振证据 → 不注册
    summary = discovery.register_mainline_themes(state, event_asof="2026-08-13")

    skipped = {s["sector"]: s["reason"] for s in summary["skipped"]}
    assert skipped.get("通信设备") == "no_resonance_evidence"
    assert registry.get_theme("theme:通信设备") is None


def test_broad_label_mainline_rejected(wiring):
    registry, discovery = wiring
    state = _selection_state()
    state["sectors"].append({
        "sector": "金融业", "rank": 0, "state": "emerging",
        "limitup_count": 5, "evidence_count": 1, "evidence_types": ["limitup_cluster"],
    })
    state["stock_sectors"]["600016"] = "金融业"
    summary = discovery.register_mainline_themes(state, event_asof="2026-08-13")

    skipped = {s["sector"]: s["reason"] for s in summary["skipped"]}
    assert skipped.get("金融业") == "broad_label_rejected"
    assert registry.get_theme("theme:金融业") is None


def test_refresh_does_not_duplicate_evidence(wiring):
    registry, discovery = wiring
    state = _selection_state()
    first = discovery.register_mainline_themes(state, event_asof="2026-08-13")
    assert {r["sector"] for r in first["registered"]} == {"通信设备", "通用设备"}

    # 次日成员变化：幂等刷新 members，不重复堆证据
    state["stock_sectors"]["000004"] = "通信设备"
    second = discovery.register_mainline_themes(state, event_asof="2026-08-14")
    assert {r["sector"] for r in second["registered"]} == {"通信设备", "通用设备"}

    theme = registry.get_theme("theme:通信设备")
    assert "000004" in theme["members"]
    assert len(theme["source_evidence"]) == 1


def test_register_error_does_not_break_discovery(wiring, monkeypatch):
    registry, discovery = wiring

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(registry, "register", boom)
    summary = discovery.register_mainline_themes(_selection_state(), event_asof="2026-08-13")

    assert summary["registered"] == []
    assert {e["sector"] for e in summary["errors"]} == {"通信设备", "通用设备"}


def test_empty_selection_state_returns_empty_summary(wiring):
    _registry, discovery = wiring
    summary = discovery.register_mainline_themes({}, event_asof="2026-08-13")
    assert summary["registered"] == []
    assert summary["skipped"] == []
    assert summary["errors"] == []
