"""Regression tests for the scheduled end-of-day anomaly scanner."""

import importlib.util
from pathlib import Path

from a_stock_http import DataSourceError


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "stock-triage" / "scripts" / "eod_anomaly_scanner.py"
SPEC = importlib.util.spec_from_file_location("eod_anomaly_scanner", SCRIPT)
scanner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scanner)


def test_fetch_minute_signals_drops_only_failed_provider_requests(monkeypatch):
    def fetch_minute(code, *, market):
        if code == "600001":
            raise DataSourceError("tencent", "down")
        return [
            {"time": "1429", "price": 10.0, "cum_volume": 700.0},
            {"time": "1500", "price": 10.3, "cum_volume": 1_000.0},
        ]

    monkeypatch.setattr(scanner, "fetch_tencent_minute", fetch_minute)

    signals, coverage = scanner._fetch_minute_signals(["600001", "600002"])

    assert coverage == {"fetched": 1, "closed": 1}
    assert set(signals) == {"600002"}


def test_fetch_universe_retries_data_source_error(monkeypatch):
    attempts = 0

    def fetch_spot():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise DataSourceError("sina", "temporary failure")

        class Frame:
            @staticmethod
            def to_dict(_orient):
                return [{"代码": "600000"}]

        return Frame()

    monkeypatch.setattr(scanner, "fetch_a_share_spot", fetch_spot)
    monkeypatch.setattr(scanner.time, "sleep", lambda _seconds: None)

    assert scanner._fetch_universe_with_retry() == [{"代码": "600000"}]
    assert attempts == 3
