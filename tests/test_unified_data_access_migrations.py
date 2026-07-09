"""Target scripts preserve their public behavior while using shared providers."""

import importlib.util
from pathlib import Path

import intraday_monitor as intraday
import portfolio_manager as portfolio
from http_client import DataSourceError, ErrorType


ROOT = Path(__file__).resolve().parents[1]


def _load_scheduled_monitor():
    path = ROOT / "skills" / "news-to-sector" / "scripts" / "scheduled_monitor.py"
    spec = importlib.util.spec_from_file_location("scheduled_monitor_data_access_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_portfolio_price_adapter_preserves_shape_and_adds_timestamp(monkeypatch):
    monkeypatch.setattr(
        portfolio,
        "fetch_tencent_quote",
        lambda code: {
            "price": 9.8,
            "change_pct": 1.2,
            "name": "华能国际",
            "fetched_at": "2026-06-12T05:45:00+00:00",
        },
    )

    result = portfolio.fetch_price("600011")

    assert result == {
        "price": 9.8,
        "change_pct": 1.2,
        "name": "华能国际",
        "fetched_at": "2026-06-12T05:45:00+00:00",
    }


def test_intraday_adapter_preserves_market_fields(monkeypatch):
    monkeypatch.setattr(
        intraday,
        "fetch_tencent_quote",
        lambda code: {
            "price": 23.45,
            "change_pct": 1.96,
            "high": 23.8,
            "low": 23.0,
            "volume": 123456,
            "amount": 50_000_000,
            "turnover": 4.2,
            "prev_close": 23.0,
            "fetched_at": "2026-06-12T05:45:00+00:00",
        },
    )

    result = intraday.fetch_realtime("002156")

    assert result["price"] == 23.45
    assert result["amount"] == 50_000_000
    assert result["fetched_at"] == "2026-06-12T05:45:00+00:00"


def test_scheduled_monitor_serializes_typed_provider_errors(monkeypatch):
    monitor = _load_scheduled_monitor()
    monkeypatch.setattr(
        monitor,
        "fetch_news",
        lambda query, limit: (_ for _ in ()).throw(
            DataSourceError(
                "serper",
                "slow",
                error_type=ErrorType.TIMEOUT,
                attempts=2,
                timestamp="2026-06-12T05:45:00+00:00",
            )
        ),
    )
    monkeypatch.setattr(
        monitor,
        "fetch_fallback_news",
        lambda limit: (_ for _ in ()).throw(
            DataSourceError(
                "public_news",
                "down",
                error_type=ErrorType.NETWORK,
                attempts=1,
                timestamp="2026-06-12T05:45:00+00:00",
            )
        ),
    )

    result = monitor.run_monitor(["半导体 A股"], limit=1)

    assert result["status"] == "insufficient_data"
    assert result["errors"][0] == {
        "query": "半导体 A股",
        "source": "serper",
        "error_type": "timeout",
        "error": "slow",
        "attempts": 2,
        "timestamp": "2026-06-12T05:45:00+00:00",
    }
