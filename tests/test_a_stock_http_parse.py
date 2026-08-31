"""腾讯行情解析 — 字段下标与认证传输锁定测试。"""

from http_client import HttpResult

import a_stock_http
from a_stock_http import (
    _TENCENT_FIELDS,
    parse_sina_snapshot_line,
    parse_tencent_minute_response,
    parse_tencent_quote_line,
)


def _build_line(code="sz002156"):
    """构造一行合成腾讯报文，按 _TENCENT_FIELDS 下标填入可识别值。"""
    n = 50
    parts = [""] * n
    parts[_TENCENT_FIELDS["name"]] = "通富微电"
    parts[_TENCENT_FIELDS["price"]] = "23.45"
    parts[_TENCENT_FIELDS["prev_close"]] = "23.00"
    parts[_TENCENT_FIELDS["open"]] = "23.10"
    parts[_TENCENT_FIELDS["volume"]] = "123456"
    parts[_TENCENT_FIELDS["change_pct"]] = "1.96"
    parts[_TENCENT_FIELDS["high"]] = "23.80"
    parts[_TENCENT_FIELDS["low"]] = "23.00"
    parts[_TENCENT_FIELDS["amount"]] = "5000"       # 万元
    parts[_TENCENT_FIELDS["turnover"]] = "4.20"
    parts[_TENCENT_FIELDS["pe"]] = "35.2"
    parts[_TENCENT_FIELDS["market_cap"]] = "356.7"
    return f'v_{code}="' + "~".join(parts) + '"'


def test_parse_basic_fields():
    r = parse_tencent_quote_line(_build_line())
    assert r is not None
    assert r["code"] == "sz002156"
    f = r["fields"]
    assert f["name"] == "通富微电"
    assert f["price"] == 23.45
    assert f["prev_close"] == 23.00
    assert f["open"] == 23.10
    assert f["change_pct"] == 1.96
    assert f["high"] == 23.80
    assert f["low"] == 23.00
    assert f["turnover"] == 4.20
    assert f["pe"] == 35.2
    assert f["market_cap"] == 356.7


def test_amount_scaled_to_yuan():
    # 成交额字段单位万元 → 解析后应 ×10000 转为元
    r = parse_tencent_quote_line(_build_line())
    assert r["fields"]["amount"] == 5000 * 10000


def test_parse_rejects_short_line():
    assert parse_tencent_quote_line('v_sz002156="1~通富微电~002156"') is None


def test_parse_rejects_garbage():
    assert parse_tencent_quote_line("not a quote line") is None


def test_parse_handles_empty_fields():
    parts = [""] * 50
    parts[1] = "测试"
    line = 'v_sh600011="' + "~".join(parts) + '"'
    r = parse_tencent_quote_line(line)
    assert r is not None
    assert r["fields"]["price"] is None   # 空字段 → None，不崩溃


# ======================== 分时（minute/query）解析 ========================
# 真实响应实测于 web.ifzq.gtimg.cn/appstock/app/minute/query?code=sz000001

def test_parse_minute_response_real_shape():
    payload = {
        "data": {
            "sz000001": {
                "data": {
                    "data": [
                        "0930 10.20 17257 17602140.00",
                        "0931 10.25 71645 73234935.00",
                    ],
                    "date": None,
                },
                "qt": {},
            },
        },
    }
    rows = parse_tencent_minute_response(payload, "000001", "sz")
    assert rows == [
        {"time": "0930", "price": 10.20, "cum_volume": 17257.0, "cum_amount": 17602140.0},
        {"time": "0931", "price": 10.25, "cum_volume": 71645.0, "cum_amount": 73234935.0},
    ]


def test_parse_minute_response_wrong_code_key_returns_empty():
    payload = {"data": {"sz000001": {"data": {"data": ["0930 10.20 17257 17602140.00"]}}}}
    assert parse_tencent_minute_response(payload, "999999", "sz") == []


def test_parse_minute_response_skips_malformed_rows():
    payload = {"data": {"sz000001": {"data": {"data": ["0930 10.20 17257 17602140.00", "garbage"]}}}}
    rows = parse_tencent_minute_response(payload, "000001", "sz")
    assert len(rows) == 1


def test_parse_minute_response_handles_missing_data():
    assert parse_tencent_minute_response({}, "000001", "sz") == []


def test_snapshot_uses_https_and_is_directionally_eligible(monkeypatch):
    requested = {}

    def fake_request_text(url, **kwargs):
        requested["url"] = url
        return HttpResult(_build_line(), "2026-08-11T01:35:00+00:00", 1)

    monkeypatch.setattr(a_stock_http, "request_text", fake_request_text)
    quote = a_stock_http.fetch_tencent_snapshot(["sz002156"])["sz002156"]

    assert requested["url"].startswith("https://")
    assert quote["transport_trust"] == "authenticated"
    assert quote["directional_eligible"] is True


def _build_sina_line(code="sh600519"):
    parts = [""] * 33
    parts[0:10] = [
        "贵州茅台", "1297.99", "1297.40", "1296.08", "1305.00",
        "1286.00", "1295.90", "1296.09", "1300157", "1678884022",
    ]
    bids = [(1295.90, 500), (1295.83, 100), (1295.80, 100), (1295.57, 2100), (1295.56, 100)]
    asks = [(1296.09, 200), (1296.17, 100), (1296.19, 200), (1296.25, 200), (1296.28, 100)]
    for index, (price, volume) in enumerate(bids):
        parts[10 + index * 2] = str(volume)
        parts[11 + index * 2] = str(price)
    for index, (price, volume) in enumerate(asks):
        parts[20 + index * 2] = str(volume)
        parts[21 + index * 2] = str(price)
    parts[30] = "2026-08-31"
    parts[31] = "11:30:00"
    return f'var hq_str_{code}="' + ",".join(parts) + '";'


def test_parse_sina_snapshot_exposes_five_levels():
    parsed = parse_sina_snapshot_line(_build_sina_line())

    assert parsed["code"] == "sh600519"
    assert parsed["bids"][0] == (1295.90, 500.0)
    assert parsed["bids"][4] == (1295.56, 100.0)
    assert parsed["asks"][0] == (1296.09, 200.0)
    assert parsed["asks"][4] == (1296.28, 100.0)


def test_sina_snapshot_uses_referer_and_authenticated_https(monkeypatch):
    requested = {}

    def fake_request_text(url, **kwargs):
        requested.update(url=url, kwargs=kwargs)
        return HttpResult(_build_sina_line(), "2026-08-31T03:30:00+00:00", 1)

    monkeypatch.setattr(a_stock_http, "request_text", fake_request_text)
    quote = a_stock_http.fetch_sina_snapshot(["sh600519"])["sh600519"]

    assert requested["url"].startswith("https://")
    assert requested["kwargs"]["headers"]["Referer"] == "https://finance.sina.com.cn/"
    assert quote["provider"] == "sina"
    assert quote["directional_eligible"] is True
