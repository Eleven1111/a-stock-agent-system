import json

import pytest

import news_sources
from http_client import DataSourceError


SOURCE = {
    "id": "gov_test_rss",
    "name": "测试官方源",
    "url": "https://example.gov.cn/rss",
    "kind": "rss",
    "source_rank": "S5",
    "source_type": "central_news",
}

RSS_DOC = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>要闻</title>
  <item>
    <title>国务院部署进一步扩大内需若干措施</title>
    <link>https://example.gov.cn/zhengce/2026-07/03/content_1.htm</link>
    <pubDate>Fri, 03 Jul 2026 08:30:00 +0800</pubDate>
  </item>
  <item>
    <title>央行开展公开市场操作</title>
    <link>https://example.gov.cn/zhengce/2026-07/03/content_2.htm</link>
  </item>
</channel></rss>
"""

ATOM_DOC = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>公告</title>
  <entry>
    <title>关于修订上市规则的公告</title>
    <link href="https://example.org/notice/1"/>
    <updated>2026-07-03T09:00:00+08:00</updated>
  </entry>
</feed>
"""


def test_parse_rss_extracts_items_with_source_fields():
    items = news_sources.parse_rss(RSS_DOC, SOURCE)
    assert len(items) == 2
    first = items[0]
    assert first["title"] == "国务院部署进一步扩大内需若干措施"
    assert first["url"].startswith("https://example.gov.cn/zhengce/")
    assert first["source_id"] == "gov_test_rss"
    assert first["source_rank"] == "S5"
    assert first["published_hint"] and first["published_hint"].startswith("2026-07-03")


def test_parse_rss_handles_atom_feeds():
    items = news_sources.parse_rss(ATOM_DOC, SOURCE)
    assert len(items) == 1
    assert items[0]["title"] == "关于修订上市规则的公告"
    assert items[0]["url"] == "https://example.org/notice/1"


def test_parse_rss_invalid_xml_raises_data_source_error():
    with pytest.raises(DataSourceError):
        news_sources.parse_rss("<rss><channel><item>", SOURCE)


def test_parse_html_anchors_drops_nav_and_resolves_relative_urls():
    html_doc = """
    <html><body>
      <a href="/zhengce/2026-07/03/content_9.htm">财政部下达新增专项债券额度 2026-07-03</a>
      <a href="#">更多</a>
      <a href="javascript:void(0)">首页</a>
      <a href="/zhengce/2026-07/03/content_9.htm">财政部下达新增专项债券额度 2026-07-03</a>
    </body></html>
    """
    source = {**SOURCE, "kind": "html", "url": "https://example.gov.cn/list.htm"}
    items = news_sources.parse_html_anchors(html_doc, source)
    assert len(items) == 1
    assert items[0]["url"] == "https://example.gov.cn/zhengce/2026-07/03/content_9.htm"
    assert items[0]["published_hint"] == "2026-07-03"


def test_load_catalog_requires_sources(tmp_path):
    good = tmp_path / "catalog.json"
    good.write_text(json.dumps({"sources": [SOURCE]}), encoding="utf-8")
    catalog = news_sources.load_catalog(good)
    assert catalog["sources"][0]["id"] == "gov_test_rss"

    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"sources": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        news_sources.load_catalog(empty)


def test_repo_catalog_is_loadable_and_ranked():
    catalog = news_sources.load_catalog()
    ranks = {src.get("source_rank") for src in catalog["sources"]}
    assert ranks <= {"S5", "S4", "S3", "S2", "S1", "S0"}
    ids = [src["id"] for src in catalog["sources"]]
    assert len(ids) == len(set(ids))


NEWSNOW_SOURCE = {
    "id": "cls_hot_newsnow",
    "name": "财联社热门",
    "url": "https://newsnow.busiyi.world/api/s?id=cls-hot",
    "kind": "newsnow",
    "source_rank": "S2",
    "source_type": "financial_hotlist",
}

NEWSNOW_DOC = json.dumps({
    "status": "success",
    "id": "cls-hot",
    "items": [
        {
            "title": "两部门发布新能源产业支持政策",
            "url": "https://www.cls.cn/detail/100001",
            "pubDate": 1783123200000,
        },
        {
            "title": "某上市公司公告重大资产重组",
            "mobileUrl": "https://m.cls.cn/detail/100002",
            "extra": {"date": "2026-07-03T09:30:00+08:00"},
        },
        {
            "title": "缺少链接的条目应被跳过",
            "pubDate": 1783123200000,
        },
        {
            "title": "没有任何时间字段的条目",
            "url": "https://www.cls.cn/detail/100003",
        },
    ],
})


def test_parse_newsnow_extracts_items_with_source_fields():
    items = news_sources.parse_newsnow(NEWSNOW_DOC, NEWSNOW_SOURCE)
    assert len(items) == 3
    first = items[0]
    assert first["title"] == "两部门发布新能源产业支持政策"
    assert first["url"] == "https://www.cls.cn/detail/100001"
    assert first["published_hint"] == "2026-07-04"
    assert first["source_id"] == "cls_hot_newsnow"
    assert first["source_rank"] == "S2"
    assert first["source_type"] == "financial_hotlist"


def test_parse_newsnow_mobile_url_and_extra_date_fallback():
    items = news_sources.parse_newsnow(NEWSNOW_DOC, NEWSNOW_SOURCE)
    second = items[1]
    assert second["url"] == "https://m.cls.cn/detail/100002"
    assert second["published_hint"] == "2026-07-03"


def test_parse_newsnow_missing_date_yields_none_hint():
    items = news_sources.parse_newsnow(NEWSNOW_DOC, NEWSNOW_SOURCE)
    assert items[2]["published_hint"] is None


def test_parse_newsnow_invalid_json_raises_data_source_error():
    with pytest.raises(DataSourceError):
        news_sources.parse_newsnow("<html>not json</html>", NEWSNOW_SOURCE)


def test_parse_newsnow_missing_items_list_raises_data_source_error():
    with pytest.raises(DataSourceError):
        news_sources.parse_newsnow(json.dumps({"status": "success"}), NEWSNOW_SOURCE)


class _FakeResult:
    def __init__(self, data):
        self.data = data
        self.fetched_at = "2026-07-04T10:00:00+08:00"


def test_fetch_source_dispatches_newsnow_kind(monkeypatch):
    captured = {}

    def fake_request_text(url, **kwargs):
        captured["url"] = url
        return _FakeResult(NEWSNOW_DOC)

    monkeypatch.setattr(news_sources, "request_text", fake_request_text)
    result = news_sources.fetch_source(NEWSNOW_SOURCE)
    assert result["status"] == "ok"
    assert len(result["items"]) == 3
    assert captured["url"] == NEWSNOW_SOURCE["url"]


def test_fetch_source_newsnow_env_base_override(monkeypatch):
    captured = {}

    def fake_request_text(url, **kwargs):
        captured["url"] = url
        return _FakeResult(NEWSNOW_DOC)

    monkeypatch.setattr(news_sources, "request_text", fake_request_text)
    monkeypatch.setenv("NEWSNOW_BASE_URL", "https://self-hosted.example.com")
    news_sources.fetch_source(NEWSNOW_SOURCE)
    assert captured["url"] == "https://self-hosted.example.com/api/s?id=cls-hot"

    monkeypatch.setattr(news_sources, "request_text", fake_request_text)
    news_sources.fetch_source(SOURCE)
    assert captured["url"] == SOURCE["url"]


def test_fetch_source_newsnow_bad_payload_is_isolated_error(monkeypatch):
    def fake_request_text(url, **kwargs):
        return _FakeResult("not json at all")

    monkeypatch.setattr(news_sources, "request_text", fake_request_text)
    result = news_sources.fetch_source(NEWSNOW_SOURCE)
    assert result["status"] == "error"
    assert result["items"] == []
    assert result["error"]["error_type"] == "decode"


def test_repo_catalog_includes_newsnow_defaults():
    catalog = news_sources.load_catalog()
    newsnow = {
        src["id"]: src for src in catalog["sources"] if src.get("kind") == "newsnow"
    }
    expected = {
        "newsnow_cls_hot",
        "newsnow_xueqiu_hotstock",
        "newsnow_wallstreetcn_quick",
        "newsnow_jin10",
        "newsnow_gelonghui",
    }
    assert set(newsnow) == expected
    assert newsnow["newsnow_xueqiu_hotstock"]["source_rank"] == "S1"
    for sid in expected - {"newsnow_xueqiu_hotstock"}:
        assert newsnow[sid]["source_rank"] == "S2"
    for src in newsnow.values():
        assert "/api/s?id=" in src["url"]
