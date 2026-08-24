"""盘中异动监控 — 新一天首次运行不得清空刚生成的告警（顺序 bug 回归）。"""

from datetime import date
from types import SimpleNamespace

import intraday_monitor as im
import pytest
from http_client import DataSourceError
from state_store import atomic_write_json


@pytest.fixture(autouse=True)
def _isolate_shortlist(tmp_path, monkeypatch):
    monkeypatch.setattr(im, "SHORTLIST_FILE", str(tmp_path / "auction_shortlist_latest.json"))


def _stub_market(monkeypatch):
    monkeypatch.setattr(im, "TRACKED_CODES", ["600001"])
    monkeypatch.setattr(im, "TRACKED_NAMES", {"600001": "测试股"})
    monkeypatch.setattr(
        im, "fetch_realtime",
        lambda code: {"price": 11.0, "change_pct": 9.8, "turnover": 5.0, "amount": 1e8},
    )
    monkeypatch.setattr(
        im,
        "fetch_realtime_many",
        lambda codes: {
            str(code)[-6:]: {
                "price": 11.0,
                "change_pct": 9.8,
                "turnover": 5.0,
                "amount": 1e8,
            }
            for code in codes
        },
    )


def test_new_day_first_run_keeps_alerts(tmp_path, monkeypatch):
    """昨日缓存残留时，新一天首次运行应正常产出告警，而非循环后被一并清空。"""
    monkeypatch.setattr(im, "ALERT_CACHE", str(tmp_path / "intraday_alerts.json"))
    monkeypatch.setattr(im, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(im.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(im.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    _stub_market(monkeypatch)

    # 注入陈旧缓存（昨天 + 已记录的告警键）
    im.save_alert_cache({"_date": "20200101", "zt_600001": "09:30"})

    data = im.check_intraday()
    assert data["has_alerts"] is True, "新一天首次运行不应吞掉刚生成的告警"
    assert any(a["type"] == "涨停" for a in data["alerts"])


def test_same_day_dedup_still_works(tmp_path, monkeypatch):
    """同一天内重复触发应去重：第二次运行不再重复报同一涨停。"""
    monkeypatch.setattr(im, "ALERT_CACHE", str(tmp_path / "intraday_alerts.json"))
    monkeypatch.setattr(im, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(im.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(im.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    _stub_market(monkeypatch)

    first = im.check_intraday()
    assert first["has_alerts"] is True

    second = im.check_intraday()
    assert not any(a["type"] == "涨停" for a in second["alerts"]), "同日涨停应已去重"


def test_sector_acceleration_detects_breadth_before_individual_limit_up():
    quotes = {
        "600001": {"change_pct": 4.2},
        "600002": {"change_pct": 3.6},
        "600003": {"change_pct": 2.8},
        "600004": {"change_pct": -0.2},
    }
    members = {
        "600001": {"name": "甲", "sector": "半导体"},
        "600002": {"name": "乙", "sector": "半导体"},
        "600003": {"name": "丙", "sector": "半导体"},
        "600004": {"name": "丁", "sector": "电力"},
    }

    alerts, state = im.detect_sector_acceleration(
        quotes,
        members,
        previous={},
        min_members=3,
        min_positive_ratio=2 / 3,
        min_average_pct=2.5,
        min_acceleration_pct=0.8,
    )

    assert len(alerts) == 1
    assert alerts[0]["type"] == "板块加速"
    assert alerts[0]["sector"] == "半导体"
    assert alerts[0]["action"] == "watch"
    assert state["半导体"]["average_pct"] > 3.0


def test_batch_quote_fetch_keeps_successful_chunks_when_one_provider_call_fails(
    monkeypatch,
):
    calls = []

    def fake_fetch(codes):
        calls.append(list(codes))
        if codes == ["600003"]:
            raise DataSourceError("tencent", "temporary failure")
        return SimpleNamespace(data={
            f"sh{code}": {"price": 10.0, "change_pct": 1.0}
            for code in codes
        })

    monkeypatch.setattr(im, "INTRADAY_QUOTE_BATCH_SIZE", 2)
    monkeypatch.setattr(im, "fetch_tencent_quotes", fake_fetch)

    result = im.fetch_realtime_many(["600001", "600002", "600003"])

    assert calls == [["600001", "600002"], ["600003"]]
    assert set(result) == {"600001", "600002"}


def test_sector_acceleration_requires_increment_after_first_alert():
    quotes = {
        "600001": {"change_pct": 4.3},
        "600002": {"change_pct": 3.7},
        "600003": {"change_pct": 2.9},
    }
    members = {
        code: {"name": code, "sector": "半导体"}
        for code in quotes
    }

    alerts, _ = im.detect_sector_acceleration(
        quotes,
        members,
        previous={"半导体": {"average_pct": 3.6, "alerted": True}},
        min_members=3,
        min_positive_ratio=2 / 3,
        min_average_pct=2.5,
        min_acceleration_pct=0.8,
    )

    assert alerts == []


def test_intraday_check_consumes_same_day_auction_shortlist_for_sector_alert(
    tmp_path,
    monkeypatch,
):
    today = date.today().isoformat()
    monkeypatch.setattr(im, "TRACKED_CODES", [])
    monkeypatch.setattr(im, "TRACKED_NAMES", {})
    monkeypatch.setattr(im, "ALERT_CACHE", str(tmp_path / "intraday_alerts.json"))
    monkeypatch.setattr(im, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(im.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(im.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    atomic_write_json(im.PORTFOLIO_FILE, {"positions": []})
    atomic_write_json(
        im.SHORTLIST_FILE,
        {
            "asof": today,
            "shortlist": [
                {"code": f"60000{i}", "name": f"股票{i}", "sector": "半导体"}
                for i in range(1, 4)
            ],
        },
    )
    monkeypatch.setattr(
        im,
        "fetch_realtime_many",
        lambda codes: {
            str(code)[-6:]: {"price": 10.0, "change_pct": 3.5, "turnover": 2.0}
            for code in codes
        },
    )

    result = im.check_intraday()

    assert result["sector_member_count"] == 3
    assert result["sector_alerts"][0]["sector"] == "半导体"
    assert result["sector_alerts"][0]["action"] == "watch"


def test_degraded_shortlist_reports_sector_monitor_inactive(tmp_path, monkeypatch):
    """竞价降级时板块告警确实发不出来，但不能静默——运维要知道监控没在工作。"""
    today = date.today().isoformat()
    monkeypatch.setattr(im, "TRACKED_CODES", [])
    monkeypatch.setattr(im, "TRACKED_NAMES", {})
    monkeypatch.setattr(im, "ALERT_CACHE", str(tmp_path / "intraday_alerts.json"))
    monkeypatch.setattr(im, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(im.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(im.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    atomic_write_json(im.PORTFOLIO_FILE, {"positions": []})
    atomic_write_json(
        im.SHORTLIST_FILE,
        {
            "asof": today,
            "status": "degraded",
            "collection_status": "empty",
            "degraded_reasons": ["竞价采集为空（0 只标的），无盘中观测，拒绝输出可执行结论"],
            "shortlist": [],
        },
    )
    monkeypatch.setattr(im, "fetch_realtime_many", lambda codes: {})

    result = im.check_intraday()

    assert result["sector_member_count"] == 0
    assert result["sector_alerts"] == []          # 不猜测成员，确实不发板块告警
    assert result["sector_monitor_status"] == "degraded"
    degraded = [a for a in result["alerts"] if a["type"] == "板块监控降级"]
    assert len(degraded) == 1
    assert "竞价采集为空" in degraded[0]["msg"]
    assert result["has_alerts"] is True           # 必须让运维看见


def test_degraded_shortlist_alert_is_deduped_across_ticks(tmp_path, monkeypatch):
    """盘中每几分钟跑一次，降级提示不能每 tick 刷屏。"""
    today = date.today().isoformat()
    monkeypatch.setattr(im, "TRACKED_CODES", [])
    monkeypatch.setattr(im, "TRACKED_NAMES", {})
    monkeypatch.setattr(im, "ALERT_CACHE", str(tmp_path / "intraday_alerts.json"))
    monkeypatch.setattr(im, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(im.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(im.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    atomic_write_json(im.PORTFOLIO_FILE, {"positions": []})
    atomic_write_json(
        im.SHORTLIST_FILE,
        {
            "asof": today, "status": "degraded", "collection_status": "empty",
            "degraded_reasons": ["竞价采集为空"], "shortlist": [],
        },
    )
    monkeypatch.setattr(im, "fetch_realtime_many", lambda codes: {})

    first = im.check_intraday()
    second = im.check_intraday()

    assert len([a for a in first["alerts"] if a["type"] == "板块监控降级"]) == 1
    assert [a for a in second["alerts"] if a["type"] == "板块监控降级"] == []
    # 但状态字段仍如实反映，不因去重而消失
    assert second["sector_monitor_status"] == "degraded"


def test_sold_stock_is_removed_from_dynamic_universe(tmp_path, monkeypatch):
    monkeypatch.setattr(im, "TRACKED_CODES", [])
    monkeypatch.setattr(im, "TRACKED_NAMES", {})
    monkeypatch.setattr(im, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(im.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(im.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    atomic_write_json(im.PORTFOLIO_FILE, {"positions": []})
    im.monitor_registry.activate(
        "stock",
        "600011",
        "华能国际",
        source="portfolio_buy",
        force=True,
    )

    universe = im.tracked_universe()

    assert "600011" not in universe


def test_manual_cancel_tombstone_excludes_portfolio_stock(tmp_path, monkeypatch):
    monkeypatch.setattr(im, "TRACKED_CODES", [])
    monkeypatch.setattr(im, "TRACKED_NAMES", {})
    monkeypatch.setattr(im, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(im.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(im.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    atomic_write_json(
        im.PORTFOLIO_FILE,
        {"positions": [{"code": "600011", "name": "测试持仓"}]},
    )
    im.monitor_registry.cancel(
        "stock",
        "600011",
        reason="user_cancelled",
        manual=True,
    )

    universe = im.tracked_universe()

    assert "600011" not in universe


def test_exit_signal_respects_t1_lock_for_same_day_position(tmp_path, monkeypatch):
    monkeypatch.setattr(im, "TRACKED_CODES", [])
    monkeypatch.setattr(im, "TRACKED_NAMES", {})
    monkeypatch.setattr(im, "ALERT_CACHE", str(tmp_path / "intraday_alerts.json"))
    monkeypatch.setattr(im, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(im.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(im.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    atomic_write_json(
        im.PORTFOLIO_FILE,
        {
            "positions": [{
                "code": "600011",
                "name": "测试持仓",
                "entry_date": date.today().isoformat(),
                "entry_price": 12.0,
                "stop_price": 11.5,
                "target_price": 15.0,
            }],
        },
    )
    monkeypatch.setattr(
        im,
        "fetch_realtime",
        lambda code: {
            "price": 11.0,
            "change_pct": -1.0,
            "turnover": 1.0,
            "amount": 1e8,
        },
    )
    monkeypatch.setattr(
        im,
        "fetch_realtime_many",
        lambda codes: {
            str(code)[-6:]: {
                "price": 11.0,
                "change_pct": -1.0,
                "turnover": 1.0,
                "amount": 1e8,
            }
            for code in codes
        },
    )
    monkeypatch.setattr(im, "read_signal_context", lambda: {})
    monkeypatch.setattr(im, "read_catalyst_events", lambda code: [])

    data = im.check_intraday()

    assert data["exit_signals"]
    assert data["exit_signals"][0]["action"] == "hold_locked"
    assert "T+1" in data["exit_signals"][0]["msg"]


def _wire_position(tmp_path, monkeypatch, position, price):
    """真实持仓形状（portfolio_manager 落盘格式）+ 指定现价的最小盘中环境。"""
    monkeypatch.setattr(im, "TRACKED_CODES", [])
    monkeypatch.setattr(im, "TRACKED_NAMES", {})
    monkeypatch.setattr(im, "ALERT_CACHE", str(tmp_path / "intraday_alerts.json"))
    monkeypatch.setattr(im, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(im.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(im.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    atomic_write_json(im.PORTFOLIO_FILE, {"positions": [position]})
    quote = {"price": price, "change_pct": -3.0, "turnover": 1.0, "amount": 1e8}
    monkeypatch.setattr(im, "fetch_realtime", lambda code: dict(quote))
    monkeypatch.setattr(
        im, "fetch_realtime_many",
        lambda codes: {str(code)[-6:]: dict(quote) for code in codes},
    )
    monkeypatch.setattr(im, "read_signal_context", lambda: {})
    monkeypatch.setattr(im, "read_catalyst_events", lambda code: [])


def test_stop_loss_fires_for_real_portfolio_manager_position(tmp_path, monkeypatch):
    """issue #88 真坑回归锁：portfolio_manager 落盘字段是 cost/buy_date（无
    entry_price/entry_date/stop_price），字段错配曾导致真实持仓的止损在盘中
    从不触发（翔鹭钨业 6/30 止损建议未闭环的原因之一）。"""
    _wire_position(tmp_path, monkeypatch, {
        "code": "002842",
        "name": "翔鹭钨业",
        "cost": 49.5,
        "shares": 400,
        "buy_date": "2026-06-23",
        "peak_price": 51.4,
        "lots": [{"shares": 400, "cost": 49.5, "acquired_on": "2026-06-23"}],
    }, price=44.0)  # -11.1%，越过 cost×(1-8%)=45.54 的兜底止损位

    data = im.check_intraday()

    stop_signals = [
        s for s in data["exit_signals"] if "stop_loss" in s["type"]
    ]
    assert stop_signals, f"真实持仓形状必须触发止损信号: {data['exit_signals']}"
    assert stop_signals[0]["action"] == "sell"


def test_critical_exit_alert_realerts_hourly_not_once_per_day(tmp_path, monkeypatch):
    """止损建议只发一次然后沉默是 -5% 拖成 -25% 的直接原因：critical 且可执行
    的退出信号缓存键必须带小时后缀（每小时重报直到执行）。"""
    _wire_position(tmp_path, monkeypatch, {
        "code": "002842",
        "name": "翔鹭钨业",
        "cost": 49.5,
        "shares": 400,
        "buy_date": "2026-06-23",
        "lots": [{"shares": 400, "cost": 49.5, "acquired_on": "2026-06-23"}],
    }, price=44.0)

    data = im.check_intraday()

    assert data["exit_signals"]
    cache = im.load_alert_cache()
    hourly_keys = [
        key for key in cache
        if key.startswith("exit_002842_stop_loss_") and key.rsplit("_", 1)[-1].isdigit()
    ]
    assert hourly_keys, f"critical 退出信号缓存键必须带小时后缀: {list(cache)}"


# ---------------------------------------------------------------------------
# issue #260 §4.D: sector watchlist must also see local_theme_candidates /
# conditional_candidates, tagged distinctly, and never upgrade to a buy signal.
# ---------------------------------------------------------------------------


def test_load_sector_watchlist_merges_local_theme_candidates(tmp_path, monkeypatch):
    today = date.today().isoformat()
    monkeypatch.setattr(im, "SHORTLIST_FILE", str(tmp_path / "auction_shortlist_latest.json"))
    monkeypatch.setattr(im.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(im.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    atomic_write_json(
        im.SHORTLIST_FILE,
        {
            "asof": today,
            "shortlist": [{"code": "600001", "name": "执行池票", "sector": "半导体"}],
            "local_theme_candidates": [
                {"code": "600002", "name": "局部观察票", "sector": "贵金属"},
            ],
            "conditional_candidates": [
                {"code": "600003", "name": "条件候选票", "sector": "贵金属"},
            ],
        },
    )

    members = im.load_sector_watchlist(today)

    assert members["600001"]["source"] == "execution"
    assert members["600002"]["source"] == "local_theme"
    assert members["600003"]["source"] == "local_theme"
    assert members["600002"]["sector"] == "贵金属"


def test_load_sector_watchlist_execution_membership_wins_over_local_theme(tmp_path, monkeypatch):
    """同一只票若同时出现在 execution shortlist 和 local_theme 名单，
    execution 身份优先（互斥语义不应在盘中监控层被局部路径覆盖）。"""
    today = date.today().isoformat()
    monkeypatch.setattr(im, "SHORTLIST_FILE", str(tmp_path / "auction_shortlist_latest.json"))
    monkeypatch.setattr(im.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(im.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    atomic_write_json(
        im.SHORTLIST_FILE,
        {
            "asof": today,
            "shortlist": [{"code": "600001", "name": "双重出现", "sector": "半导体"}],
            "local_theme_candidates": [
                {"code": "600001", "name": "双重出现", "sector": "半导体"},
            ],
        },
    )

    members = im.load_sector_watchlist(today)

    assert members["600001"]["source"] == "execution"


def test_load_sector_watchlist_excludes_manually_cancelled_tombstone(tmp_path, monkeypatch):
    """issue #260 §4.D.3：手工取消的 tombstone 不得被 local_theme 发现重新激活。"""
    today = date.today().isoformat()
    monkeypatch.setattr(im, "SHORTLIST_FILE", str(tmp_path / "auction_shortlist_latest.json"))
    monkeypatch.setattr(im.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(im.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    atomic_write_json(
        im.SHORTLIST_FILE,
        {
            "asof": today,
            "shortlist": [],
            "local_theme_candidates": [
                {"code": "600009", "name": "已被取消票", "sector": "贵金属"},
            ],
        },
    )
    im.monitor_registry.cancel("stock", "600009", reason="user_cancelled", manual=True)

    members = im.load_sector_watchlist(today)

    assert "600009" not in members


def test_detect_sector_acceleration_tags_pure_local_theme_sector():
    quotes = {
        f"60000{i}": {"change_pct": 6.0 + i} for i in range(1, 4)
    }
    members = {
        f"60000{i}": {"name": f"贵金属{i}", "sector": "贵金属", "source": "local_theme"}
        for i in range(1, 4)
    }

    alerts, state = im.detect_sector_acceleration(
        quotes, members, previous={},
        min_members=3, min_positive_ratio=0.6, min_average_pct=3.0, min_acceleration_pct=1.0,
    )

    assert len(alerts) == 1
    assert alerts[0]["action"] == "watch"
    assert alerts[0]["participation_scope"] == "local_theme_only"
    assert "局部主题观察" in alerts[0]["msg"]
    assert state["贵金属"]["participation_scope"] == "local_theme_only"


def test_detect_sector_acceleration_mixed_sources_is_not_local_theme_only():
    quotes = {f"60000{i}": {"change_pct": 6.0 + i} for i in range(1, 4)}
    members = {
        "600001": {"name": "执行池", "sector": "半导体", "source": "execution"},
        "600002": {"name": "局部票A", "sector": "半导体", "source": "local_theme"},
        "600003": {"name": "局部票B", "sector": "半导体", "source": "local_theme"},
    }

    alerts, state = im.detect_sector_acceleration(
        quotes, members, previous={},
        min_members=3, min_positive_ratio=0.6, min_average_pct=3.0, min_acceleration_pct=1.0,
    )

    assert alerts[0]["participation_scope"] is None
    assert alerts[0]["action"] == "watch"


def test_intraday_check_alerts_pure_local_theme_sector_as_watch_only(tmp_path, monkeypatch):
    today = date.today().isoformat()
    monkeypatch.setattr(im, "TRACKED_CODES", [])
    monkeypatch.setattr(im, "TRACKED_NAMES", {})
    monkeypatch.setattr(im, "ALERT_CACHE", str(tmp_path / "intraday_alerts.json"))
    monkeypatch.setattr(im, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(im.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(im.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    atomic_write_json(im.PORTFOLIO_FILE, {"positions": []})
    atomic_write_json(
        im.SHORTLIST_FILE,
        {
            "asof": today,
            "shortlist": [],
            "local_theme_candidates": [
                {"code": f"60000{i}", "name": f"贵金属{i}", "sector": "贵金属"}
                for i in range(1, 4)
            ],
        },
    )
    monkeypatch.setattr(
        im,
        "fetch_realtime_many",
        lambda codes: {
            str(code)[-6:]: {"price": 10.0, "change_pct": 5.0, "turnover": 2.0}
            for code in codes
        },
    )

    result = im.check_intraday()

    assert result["sector_member_count"] == 3
    assert result["sector_alerts"][0]["sector"] == "贵金属"
    assert result["sector_alerts"][0]["action"] == "watch"
    assert result["sector_alerts"][0]["participation_scope"] == "local_theme_only"


# ---------------------------------------------------------------------------
# 板块强度盘中时序落盘（宿主机建议第3条）：check_intraday 每 tick 追加一槽，
# 且只把有界摘要放进 artifact。
# ---------------------------------------------------------------------------


def _wire_series_env(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(im, "TRACKED_CODES", [])
    monkeypatch.setattr(im, "TRACKED_NAMES", {})
    monkeypatch.setattr(im, "ALERT_CACHE", str(tmp_path / "intraday_alerts.json"))
    monkeypatch.setattr(im, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(im, "SHORTLIST_FILE", str(tmp_path / "auction_shortlist_latest.json"))
    monkeypatch.setattr(im.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(im.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    atomic_write_json(im.PORTFOLIO_FILE, {"positions": []})


def test_check_intraday_persists_a_sector_series_slot(tmp_path, monkeypatch):
    today = date.today().isoformat()
    _wire_series_env(tmp_path, monkeypatch)
    atomic_write_json(
        im.SHORTLIST_FILE,
        {
            "asof": today,
            "shortlist": [
                {"code": f"60000{i}", "name": f"股票{i}", "sector": "半导体"}
                for i in range(1, 4)
            ],
        },
    )
    monkeypatch.setattr(
        im,
        "fetch_realtime_many",
        lambda codes: {
            str(code)[-6:]: {"price": 10.0, "change_pct": 4.0, "turnover": 2.0}
            for code in codes
        },
    )

    result = im.check_intraday()

    day = im.sector_series.load_day(today)
    assert day["sectors"]["半导体"], "板块强度帧必须真的落盘，不能只算不存"
    assert len(day["slots"]) == 1
    # artifact 只拿到计数摘要，时序本体不进 stdout（max_output_chars=2500）
    assert result["sector_series"]["tracked_sector_count"] == 1
    assert "sectors" not in result["sector_series"]


def test_check_intraday_records_degraded_slot_instead_of_skipping(tmp_path, monkeypatch):
    """短名单降级时仍要留下"这一槽跑过了"的证据，否则与作业挂掉无法区分。"""
    today = date.today().isoformat()
    _wire_series_env(tmp_path, monkeypatch)
    atomic_write_json(
        im.SHORTLIST_FILE,
        {
            "asof": today,
            "status": "degraded",
            "collection_status": "empty",
            "degraded_reasons": ["竞价采集为空（0 只标的）"],
            "shortlist": [],
        },
    )
    monkeypatch.setattr(im, "fetch_realtime_many", lambda codes: {})

    result = im.check_intraday()

    day = im.sector_series.load_day(today)
    assert len(day["slots"]) == 1
    assert len(day["degraded_slots"]) == 1
    assert "竞价采集为空" in day["degraded_slots"][0]["reason"]
    assert result["sector_series"]["degraded_slot_count"] == 1


def test_series_write_failure_does_not_suppress_alerts(tmp_path, monkeypatch):
    """时序落盘是附加观测；写失败绝不能压制涨跌停/退出告警。"""
    today = date.today().isoformat()
    _wire_series_env(tmp_path, monkeypatch)
    atomic_write_json(
        im.SHORTLIST_FILE,
        {"asof": today, "shortlist": [{"code": "600001", "name": "涨停票", "sector": "半导体"}]},
    )
    monkeypatch.setattr(im, "TRACKED_CODES", ["600001"])
    monkeypatch.setattr(im, "TRACKED_NAMES", {"600001": "涨停票"})
    monkeypatch.setattr(
        im,
        "fetch_realtime_many",
        lambda codes: {
            str(code)[-6:]: {"price": 11.0, "change_pct": 10.0, "turnover": 12.0, "amount": 5e8}
            for code in codes
        },
    )

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(im.sector_series, "record_slot", _boom)

    result = im.check_intraday()

    assert result["sector_series"]["status"] == "write_failed"
    assert "disk full" in result["sector_series"]["error"]
    assert any(a["type"] == "涨停" for a in result["alerts"]), "落盘失败不得吞掉涨停告警"
