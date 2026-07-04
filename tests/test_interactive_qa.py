import pytest

import interactive_qa as iq
from http_client import DataSourceError, HttpResult


@pytest.fixture(autouse=True)
def isolated_provider_state(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))


def _szse_fixture():
    return {
        "pageNo": 1,
        "pageSize": 200,
        "totalRecord": 3,
        "results": [
            {
                "indexId": "111",
                "stockCode": "002156",
                "companyShortName": "通富微电",
                "mainContent": "请问公司订单情况如何？",
                "attachedContent": "您好，公司在手订单饱满，谢谢关注。",
                "pubDate": "1782349908000",
                "attachedPubDate": "1782436308000",
            },
            {
                "indexId": "112",
                "stockCode": "002156",
                "companyShortName": "通富微电",
                "mainContent": "股价为什么跌了？",
                "attachedContent": "",
                "pubDate": "1782263508000",
                "attachedPubDate": None,
            },
            {
                "indexId": "113",
                "stockCode": "300394",
                "companyShortName": "天孚通信",
                "mainContent": "无关公司的问题",
                "attachedContent": "无关回复",
                "pubDate": "1782263508000",
                "attachedPubDate": "1782263600000",
            },
        ],
    }


def test_szse_fetch_filters_by_stock_code_and_grades_reply_presence(monkeypatch):
    monkeypatch.setattr(
        iq,
        "request_json",
        lambda request, **kwargs: HttpResult(_szse_fixture(), "2026-06-15T08:00:00+00:00", 1),
    )

    rows = iq.fetch_szse_interactive_qa("002156")

    assert len(rows) == 2
    assert all(row["platform"] == "szse_irm" for row in rows)
    replied = next(row for row in rows if row["has_reply"])
    unanswered = next(row for row in rows if not row["has_reply"])
    assert replied["reply"] == "您好，公司在手订单饱满，谢谢关注。"
    assert replied["question_date"] == "2026-06-25"
    assert replied["reply_date"] == "2026-06-26"
    assert replied["date"] == "2026-06-26"
    assert unanswered["reply"] is None
    assert unanswered["reply_date"] is None
    assert unanswered["date"] == "2026-06-24"


def test_szse_fetch_raises_on_invalid_response_shape(monkeypatch):
    monkeypatch.setattr(
        iq,
        "request_json",
        lambda request, **kwargs: HttpResult({"unexpected": True}, "2026-06-15T08:00:00+00:00", 1),
    )

    with pytest.raises(DataSourceError):
        iq.fetch_szse_interactive_qa("002156")


def test_szse_fetch_raises_on_network_failure(monkeypatch):
    def fail(*args, **kwargs):
        raise DataSourceError("cninfo", "boom")

    monkeypatch.setattr(iq, "request_json", fail)

    with pytest.raises(DataSourceError):
        iq.fetch_szse_interactive_qa("002156")


def test_route_by_market_szse_for_non_six_prefix(monkeypatch):
    monkeypatch.setattr(
        iq,
        "fetch_szse_interactive_qa",
        lambda code, page_size=200: [{"date": "2026-06-25", "has_reply": True}],
    )

    result = iq.fetch_interactive_qa("002156")

    assert result["market"] == "szse"
    assert result["status"] == "ok"
    assert len(result["rows"]) == 1


def test_route_by_market_sse_for_six_prefix(monkeypatch):
    monkeypatch.setattr(
        iq,
        "fetch_sse_interactive_qa",
        lambda code, page_size=10: [{"date": "2026-06-25", "has_reply": True}],
    )

    result = iq.fetch_interactive_qa("600519")

    assert result["market"] == "sse"
    assert result["status"] == "ok"


def test_sse_failure_degrades_to_sse_unavailable_not_exception(monkeypatch):
    def fail(*args, **kwargs):
        raise DataSourceError("sse", "uid not found")

    monkeypatch.setattr(iq, "fetch_sse_interactive_qa", fail)

    result = iq.fetch_interactive_qa("600519")

    assert result["status"] == "sse_unavailable"
    assert result["rows"] == []
    assert result["error"]["source"] == "sse"


def test_retention_truncates_to_configured_count(monkeypatch):
    rows = [
        {"date": f"2026-06-{i:02d}", "has_reply": True} for i in range(1, 21)
    ]
    monkeypatch.setattr(
        iq,
        "fetch_szse_interactive_qa",
        lambda code, page_size=200: rows,
    )

    result = iq.fetch_interactive_qa("002156", retention=5)

    assert len(result["rows"]) == 5


def test_sse_uid_resolution_then_feed_parse(monkeypatch):
    company_html = """
    <a href="ajax/userfeeds.do?typeCode=company&type=11&pageSize=10&uid=519&page=1">问答</a>
    """
    feed_html = """
    <div class="m_feed_item" id="item-1759812">
        <div class="m_feed_detail" style="border: none;">
            <div class="m_feed_txt">
                <a href='user.do?uid=519' >:贵州茅台(600519)</a>董秘你好，请问什么时候披露中报？
            </div>
            <div class="m_feed_from"><span>2026年06月29日 16:48</span></div>
        </div>
        <div class="m_feed_detail m_qa">
            <div class="m_feed_txt" id="m_feed_txt-1759812">
                尊敬的投资者，您好！公司2026年半年度报告的预约披露时间是2026年8月15日。
            </div>
            <div class="m_feed_from"><span>2026年06月30日 13:50</span></div>
        </div>
    </div>
    """

    responses = [company_html, feed_html]

    def fake_request_text(request, **kwargs):
        return HttpResult(responses.pop(0), "2026-06-30T08:00:00+00:00", 1)

    monkeypatch.setattr(iq, "request_text", fake_request_text)

    rows = iq.fetch_sse_interactive_qa("600519")

    assert len(rows) == 1
    assert rows[0]["platform"] == "sse_e_hudong"
    assert rows[0]["has_reply"] is True
    assert rows[0]["question_date"] == "2026-06-29"
    assert rows[0]["reply_date"] == "2026-06-30"
    assert "600519" in rows[0]["url"]


def test_sse_uid_not_found_raises(monkeypatch):
    monkeypatch.setattr(
        iq,
        "request_text",
        lambda request, **kwargs: HttpResult("<html>no uid here</html>", "2026-06-30T08:00:00+00:00", 1),
    )

    with pytest.raises(DataSourceError):
        iq.fetch_sse_interactive_qa("600519")
