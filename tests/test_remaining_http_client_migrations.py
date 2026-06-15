"""Regression coverage for the remaining direct HTTP call migrations."""

from __future__ import annotations

import importlib.util
import runpy
import sys
import urllib.parse
from pathlib import Path

import http_client
import eastmoney_intelligence
from http_client import HttpResult


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative_path: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cninfo_fetch_preserves_post_form_and_download_contract(monkeypatch, tmp_path):
    cninfo = load_module(
        "cninfo_fetch_http_client_test",
        "skills/serenity-investment-research/scripts/cninfo_fetch.py",
    )
    calls = []

    def fake_request_json(request, **kwargs):
        calls.append(("json", request, kwargs))
        return HttpResult({"announcements": []}, "2026-06-12T06:00:00+00:00", 1)

    def fake_request_bytes(url, **kwargs):
        calls.append(("bytes", url, kwargs))
        return HttpResult(b"%PDF-test", "2026-06-12T06:00:00+00:00", 1)

    monkeypatch.setattr(cninfo, "request_json", fake_request_json)
    monkeypatch.setattr(cninfo, "request_bytes", fake_request_bytes)

    assert cninfo.post_form(cninfo.QUERY_URL, {"stock": "002156,org"}) == {"announcements": []}
    request = calls[0][1]
    form = urllib.parse.parse_qs(request.data.decode("utf-8"))
    headers = {key.lower(): value for key, value in request.header_items()}
    assert request.get_method() == "POST"
    assert form == {"stock": ["002156,org"]}
    assert headers["content-type"] == "application/x-www-form-urlencoded; charset=UTF-8"
    assert calls[0][2] == {"source": "cninfo", "timeout": 30, "encoding": "utf-8"}

    output = tmp_path / "notice.pdf"
    cninfo.download("https://static.cninfo.com.cn/notice.pdf", output)
    assert output.read_bytes() == b"%PDF-test"
    assert calls[1][2] == {
        "source": "cninfo",
        "timeout": 60,
        "headers": {"User-Agent": "Mozilla/5.0 serenity-investment-research"},
    }


def test_news_monitor_fetchers_use_shared_json_and_text_clients(monkeypatch):
    calls = []
    eastmoney_calls = []

    def fake_request_json(url, **kwargs):
        calls.append(("json", url, kwargs))
        if "feed.mix.sina.com.cn" in url:
            return HttpResult({"result": {"data": []}}, "2026-06-12T06:00:00+00:00", 1)
        return HttpResult({"data": {"list": []}}, "2026-06-12T06:00:00+00:00", 1)

    def fake_request_bytes(url, **kwargs):
        calls.append(("bytes", url, kwargs))
        return HttpResult(b"", "2026-06-12T06:00:00+00:00", 1)

    monkeypatch.setattr(http_client, "request_json", fake_request_json)
    monkeypatch.setattr(http_client, "request_bytes", fake_request_bytes)
    monkeypatch.setattr(
        eastmoney_intelligence,
        "eastmoney_json",
        lambda url, **kwargs: (
            eastmoney_calls.append((url, kwargs))
            or {"data": {"list": []}}
        ),
    )
    monkeypatch.setattr(sys, "argv", ["news_monitor_v3.py", "--silent"])

    namespace = runpy.run_path(str(ROOT / "scripts/news_monitor_v3.py"))

    assert namespace["fetch_sina_finance"]() == []
    assert namespace["fetch_eastmoney_news"]() == []
    assert namespace["fetch_baidu_hot"]() == []
    assert any(
        kind == "json"
        and kwargs == {
            "source": "sina",
            "timeout": 10,
            "headers": namespace["UA"],
        }
        for kind, _url, kwargs in calls
    )
    assert any(
        kwargs == {
            "required_path": ("data", "list"),
            "required_type": list,
            "headers": namespace["UA"],
        }
        for _url, kwargs in eastmoney_calls
    )
    assert any(
        kind == "bytes"
        and kwargs == {
            "source": "baidu",
            "timeout": 10,
            "headers": namespace["UA"],
        }
        for kind, _url, kwargs in calls
    )


def test_fractal_chart_uses_shared_json_client(monkeypatch, capsys):
    captured = {}
    klines = [
        [f"2026-06-{day:02d}", "10", str(10 + day / 10), str(11 + day / 10), "9", "1000"]
        for day in range(1, 7)
    ]

    def fake_request_json(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return HttpResult(
            {"data": {"sz300255": {"qfqday": klines}}},
            "2026-06-12T06:00:00+00:00",
            1,
        )

    monkeypatch.setattr(http_client, "request_json", fake_request_json)
    monkeypatch.setattr(sys, "argv", ["fractal_chart.py", "300255", "常山药业", "--days", "6"])

    runpy.run_path(str(ROOT / "skills/chanlun-backtest/scripts/fractal_chart.py"))

    assert "param=sz300255,day,,,12,qfq" in captured["url"]
    assert captured["kwargs"] == {
        "source": "tencent",
        "timeout": 15,
        "encoding": "utf-8",
        "headers": {"User-Agent": "Mozilla/5.0"},
    }
    assert "缠论分形图" in capsys.readouterr().out


def test_target_files_no_longer_call_urlopen_directly():
    targets = [
        "skills/global-market-monitor/scripts/monitor.py",
        "skills/common/announcement_risk.py",
        "skills/serenity-investment-research/scripts/cninfo_fetch.py",
        "skills/a-stock-commands/scripts/check_alerts.py",
        "scripts/news_monitor_v3.py",
        "skills/chanlun-backtest/scripts/fractal_chart.py",
    ]
    for relative_path in targets:
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "urlopen" not in source
        assert "urllib.request.Request(" not in source
        assert "from urllib.request import Request" not in source
