from datetime import date

import pytest

import monitor_ledger
import monitor_registry as registry
import signal_ledger


def _wire(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(registry, "MIRROR_LEDGER_FILE", str(tmp_path / "monitor_ledger.jsonl"))
    monkeypatch.setattr(registry, "CHECKPOINT_FILE", str(tmp_path / "monitor_checkpoint.json"))
    registry.reset_verification_cache()


def _count_ledger_reads(monkeypatch):
    """Count full ledger replays; monitor_registry calls signal_ledger.read_events."""
    counter = {"reads": 0}
    real_read_events = signal_ledger.read_events

    def counting_read_events(ledger_file=None):
        counter["reads"] += 1
        return real_read_events(ledger_file)

    monkeypatch.setattr(signal_ledger, "read_events", counting_read_events)
    return counter


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


def test_monitor_lifecycle_is_written_to_canonical_and_compatibility_ledgers(
    tmp_path, monkeypatch
):
    _wire(tmp_path, monkeypatch)
    import signal_ledger

    registry.activate("theme", "AI算力", "AI算力", source="manual", force=True)
    registry.cancel("theme", "AI算力", reason="用户明确取消", manual=True)

    events = monitor_ledger.read_events(registry.MIRROR_LEDGER_FILE)
    assert [event["event_type"] for event in events] == [
        "monitor.activated",
        "monitor.cancelled",
    ]
    assert events[0]["links"]["monitor_id"] == "theme:AI算力"
    assert events[0]["schema"] == "monitor_ledger_event_v1"
    canonical = signal_ledger.read_events(registry.LEDGER_FILE)
    assert [event["event_type"] for event in canonical] == [
        "monitor.activated",
        "monitor.cancelled",
    ]


def test_reconcile_automatic_replaces_latest_batch_without_touching_protected_entries(
    tmp_path,
    monkeypatch,
):
    _wire(tmp_path, monkeypatch)
    registry.activate(
        "stock",
        "600001",
        "昨日标的",
        source="candidate_discovery",
        source_group="daily_observation",
        trading_date="2026-06-17",
        batch_id="batch-old",
    )
    registry.activate(
        "stock",
        "600002",
        "保留标的",
        source="candidate_discovery",
        source_group="daily_observation",
        trading_date="2026-06-17",
        batch_id="batch-old",
    )
    registry.activate("stock", "600003", "持仓标的", source="portfolio_sync")
    registry.activate("stock", "600004", "手动标的", source="manual", force=True)

    result = registry.reconcile_automatic(
        "stock",
        [
            {"code": "600002", "name": "保留标的", "metadata": {"rank": 1}},
            {"code": "600005", "name": "今日新标的", "metadata": {"rank": 2}},
        ],
        source="candidate_discovery",
        source_group="daily_observation",
        trading_date="2026-06-18",
        batch_id="batch-new",
        expires_at="2026-06-19",
    )

    assert result["activated"] == ["600002", "600005"]
    assert result["deactivated"] == ["600001"]
    assert registry.active_stock_map(asof="2026-06-18") == {
        "600002": "保留标的",
        "600003": "持仓标的",
        "600004": "手动标的",
        "600005": "今日新标的",
    }
    stale = registry.get_entry("stock", "600001")
    assert stale["status"] == "inactive"
    assert stale["reason"] == "not_in_latest_observation_batch"
    refreshed = registry.get_entry("stock", "600002")
    assert refreshed["source_group"] == "daily_observation"
    assert refreshed["last_seen_trading_date"] == "2026-06-18"
    assert refreshed["last_seen_batch_id"] == "batch-new"
    assert refreshed["metadata"]["rank"] == 1


def test_reconcile_automatic_can_replace_legacy_auto_sources(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    registry.activate("stock", "600001", "旧竞价标的", source="auction_finalize")
    registry.activate("stock", "600002", "旧催化标的", source="realtime_catalyst_trigger")

    registry.reconcile_automatic(
        "stock",
        [{"code": "600003", "name": "今日候选"}],
        source="candidate_discovery",
        source_group="daily_observation",
        replace_source_groups=[
            "daily_observation",
            "auction_finalize",
            "realtime_catalyst_trigger",
        ],
        trading_date="2026-06-18",
        batch_id="batch-new",
    )

    assert registry.get_entry("stock", "600001")["status"] == "inactive"
    assert registry.get_entry("stock", "600002")["status"] == "inactive"
    assert registry.active_stock_map(asof="2026-06-18") == {"600003": "今日候选"}


def test_reconcile_does_not_reactivate_manual_cancel_tombstone(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    registry.activate(
        "stock",
        "600001",
        "取消标的",
        source="candidate_discovery",
        source_group="daily_observation",
    )
    registry.cancel("stock", "600001", reason="用户取消", manual=True)

    result = registry.reconcile_automatic(
        "stock",
        [{"code": "600001", "name": "取消标的"}],
        source="candidate_discovery",
        source_group="daily_observation",
        trading_date="2026-06-18",
        batch_id="batch-new",
    )

    assert result["skipped"] == {"600001": "manual_cancel_tombstone"}
    assert registry.active_stock_map(asof="2026-06-18") == {}
    assert registry.get_entry("stock", "600001")["manual_cancelled"] is True


def test_gc_expired_marks_automatic_entries_inactive(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    registry.activate(
        "stock",
        "600001",
        "过期标的",
        source="realtime_catalyst_trigger",
        source_group="event_watch",
        expires_at="2026-06-17",
    )
    registry.activate("stock", "600002", "持仓标的", source="portfolio_sync")

    result = registry.gc_expired(asof="2026-06-18")

    assert result["expired"] == ["stock:600001"]
    assert registry.get_entry("stock", "600001")["status"] == "inactive"
    assert registry.active_stock_map(asof="2026-06-18") == {"600002": "持仓标的"}
    events = monitor_ledger.read_events(registry.MIRROR_LEDGER_FILE)
    assert events[-1]["event_type"] == "monitor.deactivated"


def test_first_load_replays_and_verifies_ledger_with_a_single_read(tmp_path, monkeypatch):
    """第一层：一次完整校验只允许读一遍账本（此前是 3 遍）。"""
    _wire(tmp_path, monkeypatch)
    registry.activate("stock", "600001", "示例", source="manual", force=True)
    registry.reset_verification_cache()

    counter = _count_ledger_reads(monkeypatch)
    assert registry.load_registry()[0]["status"] == "active"

    assert counter["reads"] == 1


def test_second_load_in_same_process_skips_ledger_replay(tmp_path, monkeypatch):
    """第二层：同进程内首次校验之后不再重放账本。"""
    _wire(tmp_path, monkeypatch)
    registry.activate("stock", "600001", "示例", source="manual", force=True)
    registry.reset_verification_cache()

    counter = _count_ledger_reads(monkeypatch)
    registry.load_registry()
    reads_after_first = counter["reads"]
    registry.load_registry()
    registry.load_registry()

    assert reads_after_first == 1
    assert counter["reads"] == reads_after_first


def test_reconcile_automatic_replays_ledger_once_for_the_whole_batch(tmp_path, monkeypatch):
    """issue #167 的放大链：N 只候选不应产生 N 次账本重放。"""
    _wire(tmp_path, monkeypatch)
    registry.activate("stock", "600001", "存量标的", source="manual", force=True)
    registry.reset_verification_cache()

    counter = _count_ledger_reads(monkeypatch)
    result = registry.reconcile_automatic(
        "stock",
        [{"code": f"60010{i}", "name": f"候选{i}"} for i in range(8)],
        source="candidate_discovery",
        source_group="daily_observation",
        trading_date="2026-06-18",
        batch_id="batch-new",
    )

    assert len(result["activated"]) == 8
    assert counter["reads"] == 1


def test_tampered_projection_still_fails_closed_in_a_fresh_process(tmp_path, monkeypatch):
    """fail-closed 语义未被缓存破坏：新进程（重置后）仍必须抛错。"""
    _wire(tmp_path, monkeypatch)
    registry.activate("stock", "600001", "示例", source="manual", force=True)
    records = registry.read_json(registry.REGISTRY_FILE, [])
    records[0]["status"] = "active-but-tampered"
    registry.mutate_json(registry.REGISTRY_FILE, lambda _old: records, [])
    # 篡改绕过了本模块的事务，等价于「另一个进程改过状态」——重置模拟进程重启。
    registry.reset_verification_cache()

    with pytest.raises(RuntimeError, match="projection"):
        registry.load_registry()


def test_failed_verification_is_not_cached_as_verified(tmp_path, monkeypatch):
    """校验失败不得置位标志：下一次调用仍要重放并再次抛错。"""
    _wire(tmp_path, monkeypatch)
    registry.activate("stock", "600001", "示例", source="manual", force=True)
    records = registry.read_json(registry.REGISTRY_FILE, [])
    records[0]["status"] = "active-but-tampered"
    registry.mutate_json(registry.REGISTRY_FILE, lambda _old: records, [])
    registry.reset_verification_cache()

    with pytest.raises(RuntimeError, match="projection"):
        registry.load_registry()

    counter = _count_ledger_reads(monkeypatch)
    with pytest.raises(RuntimeError, match="projection"):
        registry.load_registry()
    assert counter["reads"] >= 1


def test_failed_mutation_invalidates_the_verification_cache(tmp_path, monkeypatch):
    """事务中途失败会让投影落后于账本，缓存必须失效以便下次重放恢复。"""
    _wire(tmp_path, monkeypatch)
    registry.activate("stock", "600001", "示例", source="manual", force=True)
    real_mutate = registry.mutate_json

    def crash_after_event(path, mutator, default=None):
        current = registry.read_json(path, default)
        mutator(current)
        raise OSError("projection crash")

    monkeypatch.setattr(registry, "mutate_json", crash_after_event)
    with pytest.raises(OSError):
        registry.cancel("stock", "600001", reason="用户取消", manual=True)

    monkeypatch.setattr(registry, "mutate_json", real_mutate)
    counter = _count_ledger_reads(monkeypatch)
    recovered = registry.load_registry()

    assert counter["reads"] == 1
    assert recovered[0]["status"] == "cancelled"
