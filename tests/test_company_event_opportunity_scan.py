import importlib.util
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_scan_module():
    spec = importlib.util.spec_from_file_location(
        "company_event_scan_test",
        ROOT / "skills/company-event-opportunities/scripts/scan.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scan_script_writes_latest_and_history(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("A_STOCK_TRADING_DATE", "2026-07-07")
    monkeypatch.setenv("A_STOCK_BATCH_ID", "a-share-20260707")
    scan = load_scan_module()
    monkeypatch.setattr(scan, "load_stock_targets", lambda candidate_limit=80: [
        {"code": "600000", "name": "测试股份"},
    ])
    monkeypatch.setattr(scan, "load_default_source_payloads", lambda: [{
        "events": [{"code": "600000", "title": "测试股份重大资产重组"}],
    }])

    result = scan.run_scan()

    latest = tmp_path / "skills/company-event-opportunities/data/latest.json"
    history = tmp_path / "skills/company-event-opportunities/data/history/2026-07-07.json"
    assert result["batch_id"] == "a-share-20260707"
    assert result["has_signal"] is True
    assert latest.exists()
    assert history.exists()
    assert json.loads(latest.read_text(encoding="utf-8"))["opportunities"][0]["event_type"] == "mna_restructuring"

