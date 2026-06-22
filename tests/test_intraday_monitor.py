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
