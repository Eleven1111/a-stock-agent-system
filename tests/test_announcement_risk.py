import urllib.parse

import announcement_risk
from http_client import HttpResult


def _result(payload):
    return HttpResult(payload, "2026-06-12T06:00:00+00:00", 1)


def test_lookup_org_id_uses_cninfo_top_search(monkeypatch):
    announcement_risk.lookup_org_id.cache_clear()

    def fake_request_json(request, **kwargs):
        assert kwargs == {
            "source": "cninfo",
            "timeout": 8,
            "encoding": "utf-8",
        }
        assert request.full_url == announcement_risk.TOP_SEARCH_URL
        assert request.get_method() == "POST"
        headers = {key.lower(): value for key, value in request.header_items()}
        assert headers["content-type"] == "application/x-www-form-urlencoded; charset=UTF-8"
        assert headers["referer"] == "https://www.cninfo.com.cn/"
        form = urllib.parse.parse_qs(request.data.decode("utf-8"))
        assert form["keyWord"] == ["002156"]
        assert form["maxNum"] == ["10"]
        return _result([
            {"code": "002156", "orgId": "9900003427", "zwjc": "通富微电"},
        ])

    monkeypatch.setattr(announcement_risk, "request_json", fake_request_json)

    assert announcement_risk.lookup_org_id("002156") == "9900003427"


def test_fetch_announcements_uses_resolved_org_id(monkeypatch):
    monkeypatch.setattr(
        announcement_risk,
        "lookup_org_id",
        lambda code, timeout=8: "9900003427",
    )
    monkeypatch.setattr(announcement_risk, "_extract_pdf_text", lambda *args, **kwargs: "")

    def fake_request_json(request, **kwargs):
        assert kwargs == {
            "source": "cninfo",
            "timeout": 8,
            "encoding": "utf-8",
        }
        assert request.full_url == announcement_risk.QUERY_URL
        assert request.get_method() == "POST"
        form = urllib.parse.parse_qs(request.data.decode("utf-8"))
        assert form["stock"] == ["002156,9900003427"]
        return _result({
            "announcements": [{
                "announcementTitle": "关于股票交易异常波动的公告",
                "announcementTime": 1781136000000,
            }]
        })

    monkeypatch.setattr(announcement_risk, "request_json", fake_request_json)

    rows = announcement_risk.fetch_announcements("002156")

    assert rows[0]["title"] == "关于股票交易异常波动的公告"
