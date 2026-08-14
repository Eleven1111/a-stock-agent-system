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
        "stock_map",
        lambda: {"600001": "动态持仓"},
    )

    assert module.load_runtime_codes() == {"600001": "动态持仓"}


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
