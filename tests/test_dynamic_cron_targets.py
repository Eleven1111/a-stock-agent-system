import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_event_calendar_uses_runtime_stock_map(monkeypatch):
    module = load_module(
        "event_calendar_dynamic_targets_test",
        "skills/stock-triage/scripts/event_calendar.py",
    )
    monkeypatch.setattr(
        module.runtime_targets,
        "load_stock_targets",
        lambda: [{"code": "600001", "name": "动态持仓", "source": "portfolio"}],
    )

    assert module.load_runtime_codes() == {"600001": "动态持仓"}


def test_event_calendar_caps_targets_and_surfaces_truncation(monkeypatch):
    module = load_module(
        "event_calendar_target_cap_test",
        "skills/stock-triage/scripts/event_calendar.py",
    )
    targets = [
        {"code": f"{index:06d}", "name": f"监控{index}", "source": "monitor"}
        for index in range(1, module.MAX_STOCK_TARGETS + 5)
    ]
    targets.append({"code": "600001", "name": "持仓", "source": "portfolio"})
    monkeypatch.setattr(module.runtime_targets, "load_stock_targets", lambda: targets)
    monkeypatch.setattr(module, "fetch_lockups", lambda *args, **kwargs: {"upcoming": []})
    monkeypatch.setattr(module, "fetch_dividend", lambda *args, **kwargs: None)

    result = module.collect_events()

    assert result["targets_total"] == len(targets)
    assert result["targets_scanned"] == module.MAX_STOCK_TARGETS
    assert result["targets_truncated"] == 5
    assert result["stocks"][0]["code"] == "600001"


def test_event_calendar_rotates_non_portfolio_targets_across_runs(tmp_path, monkeypatch):
    module = load_module(
        "event_calendar_fair_rotation_test",
        "skills/stock-triage/scripts/event_calendar.py",
    )
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(module, "MAX_STOCK_TARGETS", 3)
    targets = [
        {"code": f"00000{index}", "name": f"监控{index}", "source": "monitor"}
        for index in range(1, 6)
    ] + [{"code": "600001", "name": "持仓", "source": "portfolio"}]
    monkeypatch.setattr(module.runtime_targets, "load_stock_targets", lambda: targets)
    monkeypatch.setattr(module, "fetch_lockups", lambda *args, **kwargs: {"upcoming": []})
    monkeypatch.setattr(module, "fetch_dividend", lambda *args, **kwargs: None)

    first = module.collect_events()
    second = module.collect_events()

    assert [row["code"] for row in first["stocks"]] == ["600001", "000001", "000002"]
    assert [row["code"] for row in second["stocks"]] == ["600001", "000003", "000004"]
    assert first["targets_deferred"] == second["targets_deferred"] == 3
    assert first["cursor_before"] is None
    assert first["cursor_after"] == second["cursor_before"] == "000003"


def test_capital_flow_uses_runtime_stocks_and_active_topics(monkeypatch):
    module = load_module(
        "capital_flow_dynamic_targets_test",
        "skills/stock-triage/scripts/capital_flow_monitor.py",
    )
    monkeypatch.setattr(
        module.runtime_targets,
        "load_stock_targets",
        lambda: [{"code": "000001", "name": "动态股票", "source": "monitor"}],
    )
    monkeypatch.setattr(
        module.runtime_targets,
        "load_topics",
        lambda: [
            {"kind": "sector", "key": "半导体", "label": "半导体"},
            {"kind": "theme", "key": "未知主题", "label": "未知主题"},
        ],
    )

    assert module.load_runtime_stocks() == [("000001", "sz", "动态股票")]
    sectors, unmapped = module.load_runtime_sectors()
    assert sectors == [("BK0477", "半导体")]
    assert unmapped == ["未知主题"]


def test_capital_flow_keeps_unmapped_sector_as_name_only_target(monkeypatch):
    module = load_module(
        "capital_flow_name_only_sector_test",
        "skills/stock-triage/scripts/capital_flow_monitor.py",
    )
    monkeypatch.setattr(
        module.runtime_targets,
        "load_topics",
        lambda: [
            {"kind": "sector", "key": "通信设备", "label": "通信设备"},
            {"kind": "theme", "key": "未知主题", "label": "未知主题"},
        ],
    )
    monkeypatch.setattr(module, "resolve_sector_code", lambda _name: None)

    sectors, unmapped = module.load_runtime_sectors()

    assert sectors == [("", "通信设备")]
    assert unmapped == ["通信设备", "未知主题"]


def test_institution_tracker_uses_runtime_targets(monkeypatch):
    module = load_module(
        "institution_dynamic_targets_test",
        "skills/stock-triage/scripts/institution_tracker.py",
    )
    monkeypatch.setattr(
        module.runtime_targets,
        "load_stock_targets",
        lambda: [{"code": "600001", "name": "动态股票", "source": "portfolio"}],
    )

    assert module.load_runtime_targets() == {"600001": "动态股票"}


def test_institution_tracker_caps_targets_and_surfaces_truncation(monkeypatch):
    module = load_module(
        "institution_target_cap_test",
        "skills/stock-triage/scripts/institution_tracker.py",
    )
    targets = [
        {"code": f"{index:06d}", "name": f"监控{index}", "source": "monitor"}
        for index in range(1, module.MAX_STOCK_TARGETS + 3)
    ]
    targets.append({"code": "600001", "name": "持仓", "source": "portfolio"})
    monkeypatch.setattr(module.runtime_targets, "load_stock_targets", lambda: targets)
    monkeypatch.setattr(module, "read_stock_intelligence", lambda code: {})
    monkeypatch.setattr(module, "fetch_research_visits", lambda code: [])
    monkeypatch.setattr(module, "fetch_analyst_reports", lambda code: [])
    monkeypatch.setattr(module, "fetch_insider_trades", lambda code: [])
    monkeypatch.setattr(module, "fetch_serper_inst_news", lambda code, name: [])

    result = module.collect_institution_data()

    assert result["targets_total"] == len(targets)
    assert result["targets_scanned"] == module.MAX_STOCK_TARGETS
    assert result["targets_truncated"] == 3
    assert result["stocks"][0]["code"] == "600001"


def test_institution_rotation_keeps_portfolio_and_survives_target_changes(tmp_path, monkeypatch):
    module = load_module(
        "institution_fair_rotation_test",
        "skills/stock-triage/scripts/institution_tracker.py",
    )
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(module, "MAX_STOCK_TARGETS", 3)
    current = [
        {"code": code, "name": code, "source": "monitor"}
        for code in ("000001", "000002", "000003", "000004")
    ] + [{"code": "600001", "name": "持仓", "source": "portfolio"}]
    monkeypatch.setattr(module.runtime_targets, "load_stock_targets", lambda: current)
    monkeypatch.setattr(module, "read_stock_intelligence", lambda code: {})
    monkeypatch.setattr(module, "fetch_research_visits", lambda code: [])
    monkeypatch.setattr(module, "fetch_analyst_reports", lambda code: [])
    monkeypatch.setattr(module, "fetch_insider_trades", lambda code: [])
    monkeypatch.setattr(module, "fetch_serper_inst_news", lambda code, name: [])

    first = module.collect_institution_data()
    current[:] = [
        {"code": code, "name": code, "source": "monitor"}
        for code in ("000001", "000003", "000004", "000005")
    ] + [{"code": "600001", "name": "持仓", "source": "portfolio"}]
    second = module.collect_institution_data()

    assert [row["code"] for row in first["stocks"]] == ["600001", "000001", "000002"]
    assert [row["code"] for row in second["stocks"]] == ["600001", "000003", "000004"]
    assert second["cursor_before"] == "000003"
    assert second["cursor_after"] == "000005"


def test_institution_rotation_recovers_from_corrupt_cursor(tmp_path, monkeypatch):
    module = load_module(
        "institution_corrupt_rotation_test",
        "skills/stock-triage/scripts/institution_tracker.py",
    )
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(module, "MAX_STOCK_TARGETS", 2)
    path = module.rotation_cursor_file()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(
        module.runtime_targets,
        "load_stock_targets",
        lambda: [
            {"code": "000002", "name": "二", "source": "monitor"},
            {"code": "000001", "name": "一", "source": "monitor"},
        ],
    )
    monkeypatch.setattr(module, "read_stock_intelligence", lambda code: {})
    monkeypatch.setattr(module, "fetch_research_visits", lambda code: [])
    monkeypatch.setattr(module, "fetch_analyst_reports", lambda code: [])
    monkeypatch.setattr(module, "fetch_insider_trades", lambda code: [])
    monkeypatch.setattr(module, "fetch_serper_inst_news", lambda code, name: [])

    result = module.collect_institution_data()

    assert [row["code"] for row in result["stocks"]] == ["000001", "000002"]
    assert result["cursor_state"] == "invalid_reset"


def test_rotation_always_includes_portfolio_and_explicit_high_priority(tmp_path):
    module = load_module(
        "institution_priority_rotation_test",
        "skills/stock-triage/scripts/institution_tracker.py",
    )

    plan = module.plan_fair_rotation(
        [
            {"code": "000003", "name": "普通", "source": "monitor"},
            {"code": "000002", "name": "高优", "source": "monitor", "priority": 95},
            {"code": "600001", "name": "持仓", "source": "portfolio"},
        ],
        max_targets=2,
        job_id="test-priority",
        cursor_path=str(tmp_path / "cursor.json"),
    )

    assert [row["code"] for row in plan["targets"]] == ["000002", "600001"]
    assert plan["priority_scanned"] == 2
    assert plan["targets_deferred"] == 1


def test_hk_a_linkage_contains_only_real_ah_pairs_and_dynamic_supported_targets():
    module = load_module(
        "hk_a_dynamic_targets_test",
        "skills/stock-triage/scripts/hk_a_linkage.py",
    )

    pairs = module.load_ah_pairs([
        {"code": "600011", "name": "动态名称", "source": "monitor"},
        {"code": "000001", "name": "无H股", "source": "monitor"},
    ])

    assert all(hk_code for _, _, hk_code in pairs)
    assert ("600011", "动态名称", "hk00902") in pairs
    assert not any(code == "000001" for code, _, _ in pairs)
