import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "policy-intent-decoder" / "scripts" / "watch_official_policy.py"


def load_module():
    spec = importlib.util.spec_from_file_location("policy_watcher", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_extract_links_normalizes_relative_urls():
    watcher = load_module()
    source = {
        "id": "gov",
        "name": "中国政府网",
        "url": "https://www.gov.cn/zhengce/",
        "source_rank": "S5",
    }
    html = '<a href="/zhengce/202606/content_123.htm">国务院关于促进人工智能发展的意见</a>'
    links = watcher.extract_links(html, source)
    assert links == [
        {
            "title": "国务院关于促进人工智能发展的意见",
            "url": "https://www.gov.cn/zhengce/202606/content_123.htm",
            "published_hint": "2026-06-01",
            "source_id": "gov",
            "source_name": "中国政府网",
            "source_rank": "S5",
            "source_type": None,
        }
    ]


def test_extract_links_drops_template_and_nav_links():
    watcher = load_module()
    source = {
        "id": "nfra",
        "name": "金融监管总局",
        "url": "https://www.nfra.gov.cn/cn/view/pages/index/index.html",
        "source_rank": "S4",
    }
    html = """
    <a href="{{index_gonggaotongzhi_item}}">公告通知</a>
    <a href="/cn/view/pages/item.htm">金融监管总局发布银行业风险监管办法</a>
    """
    links = watcher.extract_links(html, source)
    assert len(links) == 1
    assert links[0]["title"] == "金融监管总局发布银行业风险监管办法"


def test_score_item_requires_policy_or_tool_keyword():
    watcher = load_module()
    item = {
        "title": "中国证监会会同财政部发布资本市场改革试点通知",
        "url": "https://www.csrc.gov.cn/example.html",
        "source_id": "csrc",
        "source_name": "证监会",
        "source_rank": "S4",
        "source_type": "capital_market_regulator",
    }
    scored = watcher.score_item(item)
    assert scored is not None
    assert scored["should_decode"] is True
    assert scored["coordination_level"] == "L2"
    assert "资本市场" in scored["matched_keywords"]
    assert "通知" in scored["tool_keywords"]
    assert watcher.score_item({**item, "title": "今日天气晴朗"}) is None


def test_parse_date_hint_and_freshness_gate():
    watcher = load_module()
    assert watcher.parse_date_hint("https://x.example/202606/t20260618_1.html") == "2026-06-18"
    assert watcher.parse_date_hint("2026年6月5日发布") == "2026-06-05"
    assert watcher.freshness("2026-06-18", checked_at="2026-06-25T09:00:00+08:00", lookback_days=45) == "recent"
    assert watcher.freshness("2021-01-01", checked_at="2026-06-25T09:00:00+08:00", lookback_days=45) == "stale"
    assert watcher.freshness(None, checked_at="2026-06-25T09:00:00+08:00", lookback_days=45) == "undated"


def test_build_watch_result_marks_only_unseen_items(monkeypatch, tmp_path):
    watcher = load_module()
    catalog = {
        "sources": [
            {"id": "gov", "name": "Gov", "url": "https://gov.example/", "source_rank": "S5"}
        ]
    }
    item = {
        "title": "国务院印发资本市场高质量发展意见",
        "url": "https://gov.example/policy.html",
        "published_hint": "2026-06-25",
        "source_id": "gov",
        "source_name": "Gov",
        "source_rank": "S5",
        "source_type": None,
    }
    scored = watcher.score_item(item)
    assert scored is not None

    monkeypatch.setattr(
        watcher,
        "fetch_source",
        lambda source, timeout, max_links: {
            "source_id": source["id"],
            "status": "ok",
            "fetched_at": "2026-06-25T09:00:00+08:00",
            "items": [scored],
        },
    )
    monkeypatch.setattr(watcher, "skill_data_dir", lambda _skill: str(tmp_path))

    first = watcher.build_watch_result(
        catalog,
        timeout=1,
        max_per_source=5,
        no_state=False,
        max_seen=100,
        lookback_days=45,
    )
    second = watcher.build_watch_result(
        catalog,
        timeout=1,
        max_per_source=5,
        no_state=False,
        max_seen=100,
        lookback_days=45,
    )
    assert first["status"] == "ready"
    assert first["summary"]["new_count"] == 1
    assert second["status"] == "no_new_signal"
    assert second["summary"]["new_count"] == 0
    seen = json.loads((tmp_path / "seen_policy_items.json").read_text(encoding="utf-8"))
    assert scored["fingerprint"] in seen["fingerprints"]


def test_watch_result_only_promotes_decode_ready_signals(monkeypatch):
    watcher = load_module()
    catalog = {
        "sources": [
            {"id": "sse", "name": "SSE", "url": "https://sse.example/", "source_rank": "S2"}
        ]
    }
    low_signal = watcher.score_item({
        "title": "关于开展沪市公司提质增效专项行动的倡议",
        "url": "https://sse.example/item.html",
        "source_id": "sse",
        "source_name": "SSE",
        "source_rank": "S2",
        "source_type": "exchange",
    })
    assert low_signal is not None
    assert low_signal["should_decode"] is False
    monkeypatch.setattr(
        watcher,
        "fetch_source",
        lambda source, timeout, max_links: {
            "source_id": source["id"],
            "status": "ok",
            "fetched_at": "2026-06-25T09:00:00+08:00",
            "items": [low_signal],
        },
    )
    result = watcher.build_watch_result(
        catalog,
        timeout=1,
        max_per_source=5,
        no_state=True,
        max_seen=100,
        lookback_days=45,
    )
    assert result["status"] == "no_new_signal"
    assert result["signals"] == []
    assert result["summary"]["candidate_count"] == 1
    assert len(result["candidate_preview"]) == 1
    assert "candidates" not in result


def test_watch_result_does_not_promote_stale_or_undated_items(monkeypatch):
    watcher = load_module()
    catalog = {
        "sources": [
            {"id": "csrc", "name": "CSRC", "url": "https://csrc.example/", "source_rank": "S4"}
        ]
    }
    recent = watcher.score_item({
        "title": "证监会发布资本市场高质量发展意见",
        "url": "https://csrc.example/20260625/item.html",
        "published_hint": "2026-06-25",
        "source_id": "csrc",
        "source_name": "CSRC",
        "source_rank": "S4",
        "source_type": "capital_market_regulator",
    })
    stale = watcher.score_item({
        "title": "证监会发布资本市场高质量发展意见",
        "url": "https://csrc.example/20210101/item.html",
        "published_hint": "2021-01-01",
        "source_id": "csrc",
        "source_name": "CSRC",
        "source_rank": "S4",
        "source_type": "capital_market_regulator",
    })
    undated = watcher.score_item({
        "title": "证监会发布资本市场高质量发展意见",
        "url": "https://csrc.example/item.html",
        "published_hint": None,
        "source_id": "csrc",
        "source_name": "CSRC",
        "source_rank": "S4",
        "source_type": "capital_market_regulator",
    })
    monkeypatch.setattr(
        watcher,
        "fetch_source",
        lambda source, timeout, max_links: {
            "source_id": source["id"],
            "status": "ok",
            "fetched_at": "2026-06-25T09:00:00+08:00",
            "items": [recent, stale, undated],
        },
    )
    result = watcher.build_watch_result(
        catalog,
        timeout=1,
        max_per_source=5,
        no_state=True,
        max_seen=100,
        lookback_days=45,
    )
    assert [item["freshness"] for item in result["signals"]] == ["recent"]
    assert sorted(item["freshness"] for item in result["candidate_preview"]) == ["recent", "stale", "undated"]
