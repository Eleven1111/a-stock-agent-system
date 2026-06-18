"""盘中异动监控 — 新一天首次运行不得清空刚生成的告警（顺序 bug 回归）。"""

from datetime import date

import intraday_monitor as im
from state_store import atomic_write_json


def _stub_market(monkeypatch):
    monkeypatch.setattr(im, "TRACKED_CODES", ["600001"])
    monkeypatch.setattr(im, "TRACKED_NAMES", {"600001": "测试股"})
    monkeypatch.setattr(
        im, "fetch_realtime",
        lambda code: {"price": 11.0, "change_pct": 9.8, "turnover": 5.0, "amount": 1e8},
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
    monkeypatch.setattr(im, "read_signal_context", lambda: {})
    monkeypatch.setattr(im, "read_catalyst_events", lambda code: [])

    data = im.check_intraday()

    assert data["exit_signals"]
    assert data["exit_signals"][0]["action"] == "hold_locked"
    assert "T+1" in data["exit_signals"][0]["msg"]
