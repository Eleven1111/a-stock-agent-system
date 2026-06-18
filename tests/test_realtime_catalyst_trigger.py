"""Realtime catalyst trigger should not empty-run after matching candidates."""

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_realtime_catalyst_trigger_reads_candidate_pool_dict_and_writes_matched_context(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    trigger = load_module("realtime_catalyst_trigger_test", "scripts/realtime_catalyst_trigger.py")
    pool = tmp_path / "skills" / "stock-triage" / "data" / "candidate_pool_latest.json"
    pool.parent.mkdir(parents=True)
    pool.write_text(
        json.dumps({
            "status": "ready",
            "candidates": [{"code": "600001", "name": "测试股"}],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        trigger,
        "scan_fresh_catalysts",
        lambda: [{
            "title": "测试股获得重大突破",
            "snippet": "公司披露关键技术进展",
            "source": "测试源",
            "date": "5 minutes ago",
            "link": "https://example.com/catalyst/1",
            "tier": "T1",
        }],
    )
    captured = {}
    monkeypatch.setattr(
        trigger,
        "update_catalyst_context",
        lambda events, generated_at=None: captured.setdefault("events", events),
    )

    result = trigger.run_trigger(force=True)

    assert result["status"] == "ok"
    assert result["matched_stocks"] == 1
    assert result["alerts"][0]["code"] == "600001"
    assert captured["events"][0]["stock_code"] == "600001"
    assert captured["events"][0]["stock_name"] == "测试股"
