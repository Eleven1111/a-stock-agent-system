import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_digest_module():
    spec = importlib.util.spec_from_file_location(
        "behavioral_finance_digest_test",
        ROOT / "scripts/behavioral_finance_digest.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_digest_writes_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("A_STOCK_TRADING_DATE", "2026-07-07")
    monkeypatch.setenv("A_STOCK_BATCH_ID", "a-share-20260707")
    digest = load_digest_module()
    monkeypatch.setattr(digest, "_latest_payload", lambda job_id: {"sentiment_score": 82} if "social" in job_id else {})

    result = digest.run_digest("preopen")

    path = tmp_path / "skills/stock-triage/cache/behavioral_finance_context.json"
    assert result["stage"] == "preopen"
    assert result["trading_date"] == "2026-07-07"
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == "behavioral_finance_context_v1"

