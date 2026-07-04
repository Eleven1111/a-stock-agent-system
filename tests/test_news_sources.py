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
