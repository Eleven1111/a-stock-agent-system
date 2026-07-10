"""Regression tests for business scripts migrated to the shared HTTP layer."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from http_client import DataSourceError, ErrorType, HttpResult


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _timeout(source: str) -> DataSourceError:
    return DataSourceError(
        source,
        "slow",
        error_type=ErrorType.TIMEOUT,
        attempts=2,
        timestamp="2026-06-12T00:00:00+00:00",
    )


def test_candidate_discovery_request_bytes_uses_shared_client(monkeypatch):
    module = _load(
        "candidate_discovery_http_migration",
        "skills/stock-triage/scripts/candidate_discovery.py",
    )
    calls = []

    def fake_request(url, **kwargs):
        calls.append((url, kwargs))
        return HttpResult(b"payload", "2026-06-12T00:00:00+00:00", 1)

    monkeypatch.setattr(module, "request_bytes", fake_request)

    assert module._request_bytes("https://example.test", attempts=9) == b"payload"
    assert calls == [(
        "https://example.test",
        {
            "source": "exchange_listing",
            "timeout": 20,
            "max_attempts": 2,
            "headers": {"User-Agent": "Mozilla/5.0 (Hermes A-Stock Agent)"},
        },
    )]


def test_candidate_discovery_caps_provider_attempts_at_two(monkeypatch):
    module = _load(
        "candidate_discovery_attempt_cap",
        "skills/stock-triage/scripts/candidate_discovery.py",
    )
    config = {
        "network": {
            "quote_batch_size": 1000,
            "quote_workers": 1,
            "request_retries": 9,
            "quote_min_coverage": 0.5,
            "kline_workers": 1,
            "kline_min_coverage": 1.0,
        }
    }
    monkeypatch.setattr(module, "load_config", lambda: config)
    monkeypatch.setattr(module.time, "sleep", lambda _delay: None)

    quote_calls = 0

    def fail_quotes(_codes):
        nonlocal quote_calls
        quote_calls += 1
        raise _timeout("tencent")

    monkeypatch.setattr(module, "fetch_tencent_quote", fail_quotes)
    with pytest.raises(DataSourceError, match="覆盖不足"):
        module.fetch_universe_quotes([{"code": f"60{i:04d}"} for i in range(1000)])
    assert quote_calls == 2

    kline_calls = 0

    def empty_klines(*args, **kwargs):
        nonlocal kline_calls
        kline_calls += 1
        return []

    monkeypatch.setattr(module, "fetch_a_share_daily_kline", empty_klines)
    with pytest.raises(DataSourceError, match="K线覆盖不足"):
        module.fetch_candidate_klines([{"code": "600011"}])
    assert kline_calls == 2


def test_capital_flow_eastmoney_adapter_preserves_empty_dict_fallback(monkeypatch):
    module = _load(
        "capital_flow_http_migration",
        "skills/stock-triage/scripts/capital_flow_monitor.py",
    )
    monkeypatch.setattr(
        module,
        "eastmoney_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(_timeout("eastmoney")),
    )

    assert module.fetch_eastmoney("https://example.test") == {}


def test_event_and_institution_adapters_preserve_empty_fallback(monkeypatch):
    event = _load(
        "event_calendar_http_migration",
        "skills/stock-triage/scripts/event_calendar.py",
    )
    institution = _load(
        "institution_tracker_http_migration",
        "skills/stock-triage/scripts/institution_tracker.py",
    )
    monkeypatch.setattr(
        event,
        "_fetch_dividend",
        lambda *args, **kwargs: (_ for _ in ()).throw(_timeout("eastmoney")),
    )
    monkeypatch.setattr(
        institution,
        "_fetch_research_visits",
        lambda *args, **kwargs: (_ for _ in ()).throw(_timeout("eastmoney")),
    )
    monkeypatch.setattr(
        institution,
        "_fetch_insider_trades",
        lambda *args, **kwargs: (_ for _ in ()).throw(_timeout("eastmoney")),
    )

    assert event.fetch_dividend("002156") is None
    assert institution.fetch_research_visits("002156") == []
    assert institution.fetch_insider_trades("002156") == []


def test_all_eastmoney_business_callers_use_the_unified_adapter():
    targets = [
        "skills/stock-triage/scripts/capital_flow_monitor.py",
        "skills/stock-triage/scripts/event_calendar.py",
        "skills/stock-triage/scripts/institution_tracker.py",
        "skills/news-to-sector/scripts/main.py",
        "scripts/news_monitor_v3.py",
        "skills/common/a_stock_http.py",
    ]
    for relative_path in targets:
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert 'source="eastmoney"' not in source
        assert "subprocess.run(" not in source
        assert '["curl"' not in source


def test_four_dim_serper_preserves_raw_news_shape(monkeypatch):
    module = _load(
        "four_dim_http_migration",
        "skills/stock-triage/scripts/four_dim_scorer.py",
    )
    monkeypatch.setattr(module, "_next_serper_key", lambda: "secret")
    monkeypatch.setattr(
        module,
        "_fetch_serper_news",
        lambda query, api_key, limit: HttpResult(
            [{
                "title": "重大订单",
                "snippet": "摘要",
                "source": "测试源",
                "date": "1 day ago",
                "link": "https://example.test/news",
            }],
            "2026-06-12T00:00:00+00:00",
            1,
        ),
    )

    assert module.fetch_serper_news("测试", 1) == [{
        "title": "重大订单",
        "snippet": "摘要",
        "source": "测试源",
        "date": "1 day ago",
        "link": "https://example.test/news",
    }]


def test_hk_a_linkage_preserves_quote_shape_and_error_fallback(monkeypatch):
    module = _load(
        "hk_a_http_migration",
        "skills/stock-triage/scripts/hk_a_linkage.py",
    )
    quote = {
        "price": 10.0,
        "prev_close": 9.5,
        "change_pct": 5.2,
        "high": 10.2,
        "low": 9.4,
        "amount": 100_000_000,
        "turnover": 3.1,
        "pe": 12.0,
        "market_cap": 50_000_000_000,
    }
    monkeypatch.setattr(module, "_http_quote", lambda codes: {codes[0]: quote})
    monkeypatch.setattr(module, "_http_hk_quote", lambda code: quote)

    assert module.fetch_tencent_realtime("600011", "sh") == {
        "price": 10.0,
        "prev_close": 9.5,
        "change_pct": 5.2,
        "high": 10.2,
        "low": 9.4,
        "amount": 100_000_000,
        "turnover": 3.1,
    }
    monkeypatch.setattr(
        module,
        "_http_hk_quote",
        lambda code: (_ for _ in ()).throw(_timeout("tencent")),
    )
    assert "error" in module.fetch_tencent_hk("hk00902")


def test_institution_news_preserves_shape_and_empty_fallback(monkeypatch):
    module = _load(
        "institution_news_http_migration",
        "skills/stock-triage/scripts/institution_tracker.py",
    )
    monkeypatch.setattr(module, "_next_serper_key", lambda: "secret")
    monkeypatch.setattr(
        module,
        "_fetch_serper_news",
        lambda query, api_key, limit: HttpResult(
            [{
                "title": "评级上调",
                "source": "测试券商",
                "date": "2 hours ago",
            }],
            "2026-06-12T00:00:00+00:00",
            1,
        ),
    )
    assert module.fetch_serper_inst_news("600011", "华能国际") == [{
        "title": "评级上调",
        "source": "测试券商",
        "date": "2 hours ago",
    }]

    monkeypatch.setattr(
        module,
        "_fetch_serper_news",
        lambda *args, **kwargs: (_ for _ in ()).throw(_timeout("serper")),
    )
    assert module.fetch_serper_inst_news("600011", "华能国际") == []


def test_hot_money_fallback_preserves_dataframe_and_empty_fallback(monkeypatch):
    import market_adapters

    module = _load(
        "hot_money_http_migration",
        "skills/hot-money-tactics/scripts/analyze.py",
    )
    raw = (
        'v_sh000001="1~上证指数~000001~3000~2990~2980~0~0~0~0~0~0~0~0~0~0~0~0~0~0~'
        '0~0~0~0~0~0~0~0~0~0~0~10~0.33~3010~2970~0~1234~5678~0~0~";'
    )
    monkeypatch.setattr(
        market_adapters,
        "request_bytes",
        lambda *args, **kwargs: HttpResult(
            raw.encode("gbk"),
            "2026-06-12T00:00:00+00:00",
            1,
        ),
    )
    result = market_adapters.fetch_tencent_index_overview()
    assert isinstance(result, pd.DataFrame)
    assert result.iloc[0]["名称"] == "上证指数"

    monkeypatch.setattr(
        module,
        "fetch_tencent_index_overview",
        lambda *args, **kwargs: (_ for _ in ()).throw(_timeout("tencent")),
    )
    assert module._tencent_market_fallback().empty


def test_data_cache_uses_shared_client_and_preserves_failure_semantics(monkeypatch):
    module = _load(
        "data_cache_http_migration",
        "skills/stock-analyst/scripts/data_cache.py",
    )
    monkeypatch.setattr(
        module,
        "request_json",
        lambda *args, **kwargs: HttpResult(
            {"data": {"sh600011": {"qfqday": [["2026-06-11", "1", "2", "3", "0.5", "10", "20"]]}}},
            "2026-06-12T00:00:00+00:00",
            1,
        ),
    )
    assert module.fetch_kline_from_tencent("600011", days=1)[0]["amount"] == 20.0

    monkeypatch.setattr(
        module,
        "request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(_timeout("tencent_kline")),
    )
    assert module.fetch_kline_from_tencent("600011", days=1) is None

    monkeypatch.setattr(
        module,
        "request_bytes",
        lambda *args, **kwargs: (_ for _ in ()).throw(_timeout("tencent")),
    )
    with pytest.raises(DataSourceError, match="slow"):
        module.fetch_realtime(["600011"])


def test_news_serper_adapter_preserves_response_contract(monkeypatch):
    module = _load(
        "stock_news_http_migration",
        "skills/stock-analyst/scripts/news.py",
    )
    monkeypatch.setattr(module, "_next_serper_key", lambda: "secret")
    monkeypatch.setattr(
        module,
        "_fetch_serper_news",
        lambda query, api_key, limit: HttpResult(
            [{
                "title": "新闻标题",
                "snippet": "摘要",
                "source": "来源",
                "date": "1 hour ago",
                "link": "https://example.test",
            }],
            "2026-06-12T00:00:00+00:00",
            1,
        ),
    )

    response = module._serper_request({"q": "A股", "num": 1})

    assert response == [{
        "title": "新闻标题",
        "snippet": "摘要",
        "source": "来源",
        "date": "1 hour ago",
        "link": "https://example.test",
    }]
