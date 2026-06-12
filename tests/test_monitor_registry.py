from datetime import date

import monitor_registry as registry


def _wire(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))


def test_manual_cancel_is_not_reactivated_by_automation(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    registry.activate("theme", "AI算力", "AI算力", source="open_confirmation")
    registry.cancel("theme", "AI算力", reason="用户明确取消", manual=True)

    result = registry.activate("theme", "AI算力", "AI算力", source="scheduled_job")

    assert result["changed"] is False
    assert registry.active_entries("theme") == []


def test_portfolio_buy_can_reactivate_closed_stock(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    registry.cancel("stock", "600011", reason="已清仓", manual=False)

    registry.activate(
        "stock",
        "600011",
        "华能国际",
        source="portfolio_buy",
        force=True,
    )

    active = registry.active_stock_map()
    assert active == {"600011": "华能国际"}


def test_sync_positions_closes_stale_portfolio_monitors(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    registry.activate(
        "stock",
        "600011",
        "华能国际",
        source="portfolio_buy",
        force=True,
    )

    registry.sync_positions([], asof=date(2026, 6, 12))

    assert registry.active_stock_map() == {}
    record = registry.get_entry("stock", "600011")
    assert record["status"] == "closed"


def test_manual_stock_cancel_survives_portfolio_sync(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    registry.activate(
        "stock",
        "600011",
        "华能国际",
        source="portfolio_buy",
        force=True,
    )
    registry.cancel("stock", "600011", reason="用户不再需要推送", manual=True)

    registry.sync_positions([{"code": "600011", "name": "华能国际"}])

    assert registry.active_stock_map() == {}
    assert registry.get_entry("stock", "600011")["manual_cancelled"] is True


def test_open_rejection_deactivates_only_automatic_subscription(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    registry.activate("stock", "002156", "通富微电", source="auction_finalize")

    result = registry.deactivate_automatic("stock", "002156", "open_rejected")

    assert result["changed"] is True
    assert registry.active_stock_map() == {}
