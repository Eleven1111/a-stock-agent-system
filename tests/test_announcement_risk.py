import json
import urllib.parse

import announcement_risk


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_lookup_org_id_uses_cninfo_top_search(monkeypatch):
    announcement_risk.lookup_org_id.cache_clear()

    def fake_open(request, timeout):
        form = urllib.parse.parse_qs(request.data.decode("utf-8"))
        assert form["keyWord"] == ["002156"]
        assert form["maxNum"] == ["10"]
        return _Response([
            {"code": "002156", "orgId": "9900003427", "zwjc": "通富微电"},
        ])

    monkeypatch.setattr(announcement_risk.urllib.request, "urlopen", fake_open)

    assert announcement_risk.lookup_org_id("002156") == "9900003427"


def test_fetch_announcements_uses_resolved_org_id(monkeypatch):
    monkeypatch.setattr(
        announcement_risk,
        "lookup_org_id",
        lambda code, timeout=8: "9900003427",
    )

    def fake_open(request, timeout):
        form = urllib.parse.parse_qs(request.data.decode("utf-8"))
        assert form["stock"] == ["002156,9900003427"]
        return _Response({
            "announcements": [{
                "announcementTitle": "关于股票交易异常波动的公告",
                "announcementTime": 1781136000000,
            }]
        })

    monkeypatch.setattr(announcement_risk.urllib.request, "urlopen", fake_open)

    rows = announcement_risk.fetch_announcements("002156")

    assert rows[0]["title"] == "关于股票交易异常波动的公告"
