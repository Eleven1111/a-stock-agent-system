"""Cron scripts that remove Gateway-side template injection."""

import importlib.util
from datetime import date, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_batch_four_dim_targets_parse_pool_and_custom(tmp_path, monkeypatch):
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试状态
    # 无条件设置了它，这里要用 HERMES_HOME 驱动本用例的 tmp_path，
    # 必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    batch = load_module("batch_four_dim_scorer_test", "skills/stock-triage/scripts/batch_four_dim_scorer.py")
    pool_path = tmp_path / "skills" / "stock-triage" / "data" / "candidate_pool_latest.json"
    pool_path.parent.mkdir(parents=True)
    pool_path.write_text(
        f'{{"status":"ready","asof":"{date.today().isoformat()}","candidates":['
        '{"code":"002156","name":"通富微电"},'
        '{"code":"600011","name":"华能国际"}]}',
        encoding="utf-8",
    )

    assert batch.parse_targets(None, limit=1) == [{"code": "002156", "name": "通富微电"}]
    assert batch.parse_targets("002156:通富微电,600011:华能国际") == [
        {"code": "002156", "name": "通富微电", "strategy_id": "four_dim"},
        {"code": "600011", "name": "华能国际", "strategy_id": "four_dim"},
    ]


def test_scheduled_news_monitor_fails_closed_without_serper(tmp_path, monkeypatch):
    env_file = tmp_path / "empty.env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("A_STOCK_ENV_FILE", str(env_file))
    monkeypatch.delenv("SERPER_API_KEYS", raising=False)
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monitor = load_module("scheduled_news_monitor_test", "skills/news-to-sector/scripts/scheduled_monitor.py")
    monkeypatch.setattr(monitor, "fetch_fallback_news", lambda limit: [])

    result = monitor.run_monitor(["半导体 A股"], limit=1)

    assert result["status"] == "insufficient_data"
    assert result["signals"] == []


def test_scheduled_news_monitor_all_provider_errors_are_not_no_signal(monkeypatch):
    monitor = load_module("scheduled_news_monitor_all_fail_test", "skills/news-to-sector/scripts/scheduled_monitor.py")
    monkeypatch.setattr(
        monitor,
        "fetch_news",
        lambda *args, **kwargs: (_ for _ in ()).throw(monitor.DataSourceError("serper", "down")),
    )
    monkeypatch.setattr(
        monitor,
        "fetch_fallback_news",
        lambda limit: (_ for _ in ()).throw(monitor.DataSourceError("public_news", "down")),
    )

    result = monitor.run_monitor(["半导体 A股"], limit=1)

    assert result["status"] == "insufficient_data"
    assert result["signals"] == []
    assert len(result["errors"]) == 2


def test_scheduled_news_monitor_uses_public_fallback_as_descriptive_only(monkeypatch):
    monitor = load_module("scheduled_news_monitor_fallback_test", "skills/news-to-sector/scripts/scheduled_monitor.py")
    monkeypatch.setattr(
        monitor,
        "fetch_news",
        lambda *args, **kwargs: (_ for _ in ()).throw(monitor.DataSourceError("serper", "down")),
    )
    monkeypatch.setattr(
        monitor,
        "fetch_fallback_news",
        lambda limit: [{
            "title": "国务院政策支持先进制造",
            "snippet": "",
            "source": "新浪财经",
            "provider": "sina",
            "date": "刚刚",
            "link": "https://example.com/fallback",
        }],
    )

    result = monitor.run_monitor(["A股 政策"], limit=1, now=datetime(2026, 6, 23, 10, 0))

    assert result["status"] == "degraded"
    assert result["event_count"] == 1
    assert result["signals"] == []
    assert result["events"][0]["provider"] == "sina"



def test_scheduled_news_monitor_filters_irrelevant_public_fallback(monkeypatch):
    monitor = load_module("scheduled_news_monitor_fallback_filter_test", "skills/news-to-sector/scripts/scheduled_monitor.py")
    monkeypatch.setattr(
        monitor,
        "fetch_news",
        lambda *args, **kwargs: (_ for _ in ()).throw(monitor.DataSourceError("serper", "down")),
    )
    monkeypatch.setattr(
        monitor,
        "fetch_fallback_news",
        lambda limit: [
            {
                "title": "巴西VS日本赛前，12家AI全部看好巴西",
                "snippet": "世界杯淘汰赛焦点对决",
                "source": "新浪财经",
                "provider": "sina",
                "date": "刚刚",
                "link": "https://example.com/sports",
            },
            {
                "title": "证监会优化并购重组监管安排",
                "snippet": "支持上市公司提升质量",
                "source": "新浪财经",
                "provider": "sina",
                "date": "刚刚",
                "link": "https://example.com/market",
            },
            {
                "title": "ServiceNow与埃森哲联手推出AI风险服务",
                "snippet": "加速企业告别遗留平台",
                "source": "新浪财经",
                "provider": "sina",
                "date": "刚刚",
                "link": "https://example.com/us-ai",
            },
        ],
    )

    result = monitor.run_monitor(["A股 政策"], limit=1, now=datetime(2026, 6, 23, 10, 0))

    assert result["status"] == "degraded"
    assert result["event_count"] == 1
    assert result["events"][0]["link"] == "https://example.com/market"


def test_scheduled_news_monitor_adds_active_registry_queries(monkeypatch):
    monitor = load_module("scheduled_news_monitor_registry_test", "skills/news-to-sector/scripts/scheduled_monitor.py")
    monkeypatch.setattr(
        monitor,
        "active_entries",
        lambda kind=None: [
            {"kind": "theme", "key": "AI算力", "label": "AI算力"},
            {"kind": "stock", "key": "002156", "label": "通富微电"},
        ],
    )

    queries = monitor.build_queries()

    assert any("AI算力" in query for query in queries)
    assert any("通富微电" in query and "澄清" in query for query in queries)
    assert not any("封测" in query or "高温" in query for query in queries)


def test_scheduled_news_monitor_has_no_static_user_topic_queries():
    monitor = load_module(
        "scheduled_news_monitor_no_static_topics_test",
        "skills/news-to-sector/scripts/scheduled_monitor.py",
    )

    assert monitor.DEFAULT_QUERIES == [
        "国务院 发改委 工信部 证监会 A股 产业政策",
        "地缘冲突 制裁 关税 大宗商品 A股 风险",
    ]


def test_scheduled_news_monitor_marks_clarification_as_risk():
    monitor = load_module("scheduled_news_monitor_risk_test", "skills/news-to-sector/scripts/scheduled_monitor.py")

    event = monitor.classify_event({
        "title": "公司澄清AI订单传闻",
        "snippet": "相关消息不属实，尚未形成收入",
    })

    assert event["risk_classification"]["is_risk"] is True
    assert "澄清" in event["risk_classification"]["clarification_hits"]
    assert "澄清" in event["risk_classification"]["review_hits"]
    assert "不属实" in event["risk_classification"]["thesis_invalidation_hits"]


def test_scheduled_news_monitor_keeps_abnormal_volatility_as_warning_only():
    monitor = load_module(
        "scheduled_news_monitor_warning_test",
        "skills/news-to-sector/scripts/scheduled_monitor.py",
    )

    event = monitor.classify_event({
        "title": "股票交易异常波动公告",
        "snippet": "公司经营正常，不存在应披露而未披露事项",
    })

    assert event["risk_classification"]["is_risk"] is False
    assert event["risk_classification"]["warning_only_hits"] == ["异常波动"]


def test_scheduled_news_monitor_parses_event_time_and_latency(monkeypatch):
    monitor = load_module("scheduled_news_monitor_freshness_test", "skills/news-to-sector/scripts/scheduled_monitor.py")
    now = datetime(2026, 6, 17, 10, 0)
    monkeypatch.setattr(
        monitor,
        "fetch_news",
        lambda *args, **kwargs: [{
            "title": "公司中标大额订单",
            "snippet": "订单金额显著",
            "source": "测试源",
            "date": "5 minutes ago",
            "link": "https://example.com/news/1",
        }],
    )
    monkeypatch.setattr(monitor, "update_catalyst_context", lambda events: {})

    result = monitor.run_monitor(
        ["测试股 600001 公告"],
        limit=1,
        freshness_sla_minutes=10,
        now=now,
    )

    event = result["events"][0]
    assert event["stock_code"] == "600001"
    assert event["published_at"] == "2026-06-17T09:55:00"
    assert event["latency_minutes"] == 5
    assert result["freshness"]["status"] == "fresh"
    assert result["signals"] == result["events"]


def test_scheduled_news_monitor_fails_closed_on_stale_news(monkeypatch):
    monitor = load_module("scheduled_news_monitor_stale_test", "skills/news-to-sector/scripts/scheduled_monitor.py")
    monkeypatch.setattr(
        monitor,
        "fetch_news",
        lambda *args, **kwargs: [{
            "title": "公司获得订单",
            "snippet": "订单金额显著",
            "source": "测试源",
            "date": "3 hours ago",
            "link": "https://example.com/news/2",
        }],
    )
    monkeypatch.setattr(monitor, "update_catalyst_context", lambda events: {})

    result = monitor.run_monitor(
        ["测试股 600001 公告"],
        limit=1,
        freshness_sla_minutes=30,
        now=datetime(2026, 6, 17, 10, 0),
    )

    assert result["status"] == "stale_data"
    assert result["freshness"]["status"] == "stale"
    assert result["events"][0]["latency_minutes"] == 180
    assert result["signals"] == []


def test_scheduled_news_monitor_drops_news_older_than_24_hours(monkeypatch):
    monitor = load_module("scheduled_news_monitor_24h_filter_test", "skills/news-to-sector/scripts/scheduled_monitor.py")
    monkeypatch.setattr(
        monitor,
        "fetch_news",
        lambda *args, **kwargs: [{
            "title": "公司两天前获得订单",
            "snippet": "订单金额显著",
            "source": "测试源",
            "date": "2 days ago",
            "link": "https://example.com/news/old",
        }],
    )
    monkeypatch.setattr(monitor, "update_catalyst_context", lambda events: {})

    result = monitor.run_monitor(
        ["测试股 600001 公告"],
        limit=1,
        freshness_sla_minutes=30,
        now=datetime(2026, 6, 17, 10, 0),
    )

    assert result["status"] == "no_signal"
    assert result["event_count"] == 0
    assert result["signal_count"] == 0
    assert result["age_filter"]["dropped_stale_count"] == 1


def test_intraday_news_mode_uses_high_risk_stock_queries(monkeypatch):
    monitor = load_module("scheduled_news_monitor_intraday_test", "skills/news-to-sector/scripts/scheduled_monitor.py")
    monkeypatch.setattr(monitor, "load_stock_targets", lambda candidate_limit=0: [])
    monkeypatch.setattr(
        monitor,
        "active_entries",
        lambda kind=None: [{"kind": "stock", "key": "600001", "label": "测试股"}],
    )

    queries = monitor.build_queries(mode="intraday")

    assert any("测试股 600001" in query for query in queries)
    assert any("异动公告" in query and "减持" in query and "停牌" in query for query in queries)


def test_intraday_news_mode_includes_runtime_stock_targets(monkeypatch):
    monitor = load_module(
        "scheduled_news_monitor_intraday_targets_test",
        "skills/news-to-sector/scripts/scheduled_monitor.py",
    )
    monkeypatch.setattr(monitor, "active_entries", lambda kind=None: [])
    monkeypatch.setattr(
        monitor,
        "load_stock_targets",
        lambda candidate_limit=0: [{"code": "000001", "name": "平安银行"}],
    )

    queries = monitor.build_queries(mode="intraday")

    assert any("平安银行 000001" in query for query in queries)
    assert any("监管问询" in query and "停牌" in query for query in queries)


def test_social_attention_collection_writes_snapshot_and_signal_context(
    tmp_path,
    monkeypatch,
):
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试状态
    # 无条件设置了它，这里要用 HERMES_HOME 驱动本用例的 tmp_path，
    # 必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    collector = load_module(
        "social_attention_collect_test",
        "skills/social-sentiment/scripts/collect.py",
    )
    rankings = {
        "eastmoney": [{
            "code": "SZ002156",
            "name": "通富微电",
            "rank": 3,
            "rank_change": 18,
        }],
        "xueqiu_discussion": [{
            "code": "SZ002156",
            "name": "通富微电",
            "rank": 8,
            "metric_value": 4200,
            "price_change_pct": 5.2,
        }],
        "xueqiu_follow": [],
    }

    result = collector.run_collection(
        asof="2026-06-15",
        batch_id="test-batch",
        ranking_collector=lambda: (
            rankings,
            {
                "eastmoney": {"status": "ok"},
                "xueqiu": {"status": "ok"},
                "baidu": {"status": "disabled"},
            },
        ),
        metadata_loader=lambda: {"002156": {"sector": "半导体"}},
    )

    assert result["status"] == "ready"
    assert result["snapshot_ref"]["snapshot_id"].startswith("snap-")
    assert result["top_stocks"][0]["code"] == "002156"
    cache = tmp_path / "skills" / "stock-triage" / "cache" / "social_attention.json"
    signal = tmp_path / "skills" / "stock-triage" / "cache" / "signal_context.json"
    assert cache.exists()
    assert signal.exists()


def test_serenity_refresh_planner_uses_runtime_state_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    planner = load_module(
        "serenity_refresh_queue_test",
        "skills/common/serenity_refresh_queue.py",
    )
    monkeypatch.setattr(planner, "read_deep_research", lambda code, today=None: None)
    monkeypatch.setattr(planner.monitor_registry, "active_entries", lambda kind=None: [])

    portfolio = tmp_path / "skills" / "stock-triage" / "data" / "portfolio.json"
    portfolio.parent.mkdir(parents=True)
    portfolio.write_text(
        '{"positions":[{"code":"600001","name":"持仓股"}]}',
        encoding="utf-8",
    )

    result = planner.plan_and_save(asof="2026-06-13", limit=1)

    assert result["created"] == 1
    assert result["created_requests"][0]["code"] == "600001"
