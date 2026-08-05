import urllib.parse

import pytest

import cninfo_client
from http_client import HttpResult


def _result(payload):
    return HttpResult(payload, "2026-08-04T06:00:00+00:00", 1)


def _ann(ann_id, code="000001", title="关于回购股份的公告", ts=1785547800000):
    return {
        "announcementId": ann_id,
        "secCode": code,
        "secName": "平安银行",
        "announcementTitle": title,
        "announcementTime": ts,
        "announcementType": "01",
        "adjunctUrl": f"finalpage/2026-08-01/{ann_id}.PDF",
    }


def _page(items, *, has_more, total_pages=0):
    return _result({
        "announcements": items,
        "hasMore": has_more,
        "totalpages": total_pages,
    })


def test_fetch_day_bills_bulk_source_and_passes_date_range(monkeypatch):
    seen = []

    def fake_request_json(request, **kwargs):
        seen.append(kwargs["source"])
        form = urllib.parse.parse_qs(request.data.decode("utf-8"))
        assert form["seDate"] == ["2026-08-01~2026-08-01"]
        assert form["pageSize"] == [str(cninfo_client.PAGE_SIZE)]
        return _page([_ann("A1")], has_more=False)

    monkeypatch.setattr(cninfo_client, "request_json", fake_request_json)

    rows = cninfo_client.fetch_day("2026-08-01", columns=("szse",))

    assert seen == [cninfo_client.SOURCE_BULK]
    assert rows[0]["ann_date"] == "2026-08-01"
    assert rows[0]["url"] == "https://static.cninfo.com.cn/finalpage/2026-08-01/A1.PDF"


def test_fetch_day_dedupes_across_columns(monkeypatch):
    monkeypatch.setattr(
        cninfo_client,
        "request_json",
        lambda request, **kwargs: _page([_ann("SAME")], has_more=False),
    )

    rows = cninfo_client.fetch_day("2026-08-01", columns=("szse", "sse", "bj"))

    assert [r["ann_id"] for r in rows] == ["SAME"]


def test_fetch_day_follows_pages_until_short_page(monkeypatch):
    pages = [
        _page([_ann(f"P1-{i}") for i in range(cninfo_client.PAGE_SIZE)], has_more=True),
        _page([_ann("P2-0")], has_more=True),
    ]
    calls = {"n": 0}

    def fake_request_json(request, **kwargs):
        page = pages[calls["n"]]
        calls["n"] += 1
        return page

    monkeypatch.setattr(cninfo_client, "request_json", fake_request_json)

    rows = cninfo_client.fetch_day("2026-08-01", columns=("szse",))

    # 第二页不足 PAGE_SIZE，即使 hasMore 仍为 True 也必须停
    assert calls["n"] == 2
    assert len(rows) == cninfo_client.PAGE_SIZE + 1


def test_fetch_day_filters_by_watchlist(monkeypatch):
    monkeypatch.setattr(
        cninfo_client,
        "request_json",
        lambda request, **kwargs: _page(
            [_ann("A", code="000001"), _ann("B", code="600519")],
            has_more=False,
        ),
    )

    rows = cninfo_client.fetch_day("2026-08-01", columns=("szse",), codes={"600519"})

    assert [r["code"] for r in rows] == ["600519"]


def test_fetch_day_fails_loud_on_runaway_pagination(monkeypatch):
    monkeypatch.setattr(cninfo_client, "MAX_PAGES_PER_COLUMN", 3)
    counter = {"n": 0}

    def fake_request_json(request, **kwargs):
        counter["n"] += 1
        return _page(
            [_ann(f"X{counter['n']}-{i}") for i in range(cninfo_client.PAGE_SIZE)],
            has_more=True,
        )

    monkeypatch.setattr(cninfo_client, "request_json", fake_request_json)

    with pytest.raises(cninfo_client.CninfoPaginationError):
        cninfo_client.fetch_day("2026-08-01", columns=("szse",))


def test_clean_title_strips_highlight_markup():
    assert cninfo_client.clean_title("关于<em>回购</em>股份　的公告") == "关于回购股份 的公告"


def test_millis_to_date_uses_beijing_time_not_utc():
    # 2026-08-01 00:30 CST —— UTC 下会被记成 07-31，必须仍归到 08-01
    assert cninfo_client.millis_to_date(1785515400000) == "2026-08-01"


def test_millis_to_date_returns_empty_on_garbage():
    assert cninfo_client.millis_to_date(None) == ""
    assert cninfo_client.millis_to_date("not-a-number") == ""


def test_lookup_org_id_uses_cninfo_top_search(monkeypatch):
    cninfo_client.lookup_org_id.cache_clear()

    def fake_request_json(request, **kwargs):
        assert kwargs == {
            "source": "cninfo",
            "timeout": 8,
            "encoding": "utf-8",
        }
        assert request.full_url == cninfo_client.TOP_SEARCH_URL
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

    monkeypatch.setattr(cninfo_client, "request_json", fake_request_json)

    assert cninfo_client.lookup_org_id("002156") == "9900003427"
