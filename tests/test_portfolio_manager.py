"""资金/仓位管理 — 迁移对账 + 并发安全 + 输入校验测试（纯逻辑，不触网）"""

import threading
from datetime import date

import portfolio_manager as pm
import signal_ledger
from a_share_rules import is_trading_day, previous_trading_day


def _n_trading_days_ago(n: int) -> str:
    """回退 n 个交易日，语义与 pm._trading_days_elapsed 的 (start, end] 区间对齐。

    非交易日（周末/节假日）先锚定到最近一个交易日再回退，
    否则区间内只剩 n-1 个交易日，测试随日历漂移失败。
    """
    cursor = date.today()
    if not is_trading_day(cursor):
        cursor = previous_trading_day(cursor)
    for _ in range(n):
        cursor = previous_trading_day(cursor)
    return cursor.isoformat()


def _wire(tmp_path, monkeypatch, initial=None):
    """把模块级文件常量指向临时目录，可选写入初始持仓。"""
    monkeypatch.setattr(pm, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(pm, "CASHFLOW_FILE", str(tmp_path / "cash_flow.json"))
    monkeypatch.setattr(pm, "HISTORY_FILE", str(tmp_path / "trade_history.json"))
    monkeypatch.setattr(pm.monitor_registry, "REGISTRY_FILE", str(tmp_path / "monitor_registry.json"))
    monkeypatch.setattr(pm, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(
        pm.monitor_registry, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl")
    )
    monkeypatch.setattr(
        pm.monitor_registry, "MIRROR_LEDGER_FILE", str(tmp_path / "monitor_ledger.jsonl")
    )
    if initial is not None:
        pm.save_portfolio(initial)


def _classification(
    sector="公用事业",
    industry="电力",
    source="candidate_snapshot",
    asof="2026-07-10",
):
    return {
        "sector": sector,
        "industry": industry,
        "classification_source": source,
        "classification_asof": asof,
    }


def test_new_position_without_sector_fails_closed(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 100000, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })

    result = pm.add_position("600011", "华能", 10.0, 1000)

    assert result["code"] == "UNKNOWN_SECTOR"
    assert "error" in result
    assert pm.load_portfolio()["positions"] == []


def test_new_position_requires_classification_provenance(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 100000, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })

    result = pm.add_position(
        "600011", "华能", 10.0, 1000, sector="公用事业", industry="电力",
    )

    assert result["code"] == "CLASSIFICATION_PROVENANCE_REQUIRED"
    assert pm.load_portfolio()["positions"] == []


def test_classification_asof_cannot_be_after_trade_date(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 100000, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })

    result = pm.add_position(
        "600011",
        "华能",
        10.0,
        1000,
        trade_date="2026-07-09",
        **_classification(asof="2026-07-10"),
    )

    assert result["code"] == "CLASSIFICATION_DATE_INVALID"
    assert pm.load_portfolio()["positions"] == []


def test_new_position_persists_sector_industry_source_and_asof(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 100000, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })

    result = pm.add_position(
        "600011", "华能", 10.0, 1000, **_classification(),
    )

    assert result["ok"] is True
    position = pm.load_portfolio()["positions"][0]
    assert position["sector"] == "公用事业"
    assert position["industry"] == "电力"
    assert position["classification_source"] == "candidate_snapshot"
    assert position["classification_asof"] == "2026-07-10"


def test_add_position_blocks_projected_sector_exposure_above_limit(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 60000,
        "positions": [{
            "code": "600001", "name": "同业", "cost": 10.0, "shares": 3500,
            "current_price": 10.0, "market_value": 35000.0,
            "sector": "半导体", "industry": "芯片",
            "classification_source": "candidate_snapshot",
            "classification_asof": "2026-07-10",
            "lots": [{"shares": 3500, "cost": 10.0, "acquired_on": "2026-07-01"}],
        }],
        "total_cost": 35000,
        "cash_reconciled": True,
    })

    result = pm.add_position(
        "600002", "新票", 10.0, 800,
        **_classification(sector="半导体", industry="设备"),
    )

    assert result["code"] == "SECTOR_EXPOSURE_LIMIT"
    assert "sector_exposure_limit" in result["blocking_reasons"]
    assert len(pm.load_portfolio()["positions"]) == 1


def test_add_position_blocks_when_existing_holding_sector_is_unknown(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 80000,
        "positions": [{
            "code": "600001", "name": "旧仓", "cost": 10.0, "shares": 2000,
            "current_price": 10.0,
            "lots": [{"shares": 2000, "cost": 10.0, "acquired_on": "2026-07-01"}],
        }],
        "total_cost": 20000,
        "cash_reconciled": True,
    })

    result = pm.add_position(
        "600002", "新票", 10.0, 500,
        **_classification(sector="半导体", industry="设备"),
    )

    assert result["code"] == "EXISTING_SECTOR_UNKNOWN"
    assert "existing_position_sector_unknown" in result["blocking_reasons"]
    assert len(pm.load_portfolio()["positions"]) == 1


def test_add_position_rejects_conflicting_reclassification(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 90000,
        "positions": [{
            "code": "600011", "name": "华能", "cost": 10.0, "shares": 1000,
            **_classification(),
            "lots": [{"shares": 1000, "cost": 10.0, "acquired_on": "2026-07-01"}],
        }],
        "total_cost": 10000,
        "cash_reconciled": True,
    })

    result = pm.add_position(
        "600011", "华能", 10.0, 500,
        **_classification(sector="煤炭", industry="动力煤"),
    )

    assert result["code"] == "SECTOR_CLASSIFICATION_CONFLICT"
    assert pm.load_portfolio()["positions"][0]["shares"] == 1000


def test_missing_portfolio_initializes_fail_closed_without_fake_capital(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)

    portfolio = pm.ensure_portfolio()

    assert portfolio["cash"] == 0
    assert portfolio["account_state"] == "unconfigured"


def test_reconcile_cash_records_verified_runtime_balance(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 100000, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })

    result = pm.reconcile_cash(20000, source="user_confirmed", asof="2026-06-23")

    assert result["ok"] is True
    assert pm.load_portfolio()["cash"] == 20000
    assert pm.load_portfolio()["cash_source"] == "user_confirmed"
    assert pm.load_portfolio()["cash_asof"] == "2026-06-23"
    assert pm.load_cashflow()[-1]["action"] == "reconcile_cash"


# ========== CRITICAL A：老文件一次性现金对账 ==========

def test_legacy_cash_reconciled_once(tmp_path, monkeypatch):
    """旧版 cash 是未扣减的总本金 → 首次加载按 本金-持仓成本 重算，且幂等。"""
    _wire(tmp_path, monkeypatch, initial={
        "cash": 100000,
        "positions": [{"code": "600011", "name": "华能", "cost": 9.1, "shares": 8000}],
        "total_cost": 72800,
    })  # 注意：无 cash_reconciled 标记 → 视为老文件

    pf = pm.ensure_portfolio()
    assert pf["cash"] == 27200, "可用现金应 = 100000 - 72800"
    assert pf["cash_reconciled"] is True

    # 幂等：再次加载不得二次扣减
    pf2 = pm.ensure_portfolio()
    assert pf2["cash"] == 27200


def test_new_file_not_double_reconciled(tmp_path, monkeypatch):
    """已带 cash_reconciled 的新文件不得被再次对账。"""
    _wire(tmp_path, monkeypatch, initial={
        "cash": 30000, "positions": [], "total_cost": 70000, "cash_reconciled": True,
    })
    pf = pm.ensure_portfolio()
    assert pf["cash"] == 30000


# ========== CRITICAL B：portfolio.json 读改写并发安全 ==========

def test_concurrent_add_no_loss(tmp_path, monkeypatch):
    """10 个并发开仓不得丢持仓、现金扣减必须精确、账实一致。"""
    _wire(tmp_path, monkeypatch, initial={
        "cash": 1000000, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })

    n = 10
    threads = [
        threading.Thread(
            target=pm.add_position,
            args=(f"60{i:04d}", f"股{i}", 10.0, 1000),
            kwargs=_classification(sector=f"行业{i}", industry=f"细分{i}"),
        )
        for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    pf = pm.load_portfolio()
    assert len(pf["positions"]) == n, f"并发开仓丢持仓: 期望{n} 实际{len(pf['positions'])}"
    assert pf["cash"] == 1000000 - n * 10000, "现金扣减不精确（丢更新）"

    buys = [c for c in pm.load_cashflow() if c["action"] == "buy"]
    assert len(buys) == n
    assert pf["cash"] == 1000000 - sum(c["amount"] for c in buys), "账本与余额不一致"


def test_concurrent_deposit_no_loss(tmp_path, monkeypatch):
    """20 个并发入金，最终现金必须等于全部入金之和。"""
    _wire(tmp_path, monkeypatch, initial={
        "cash": 0, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })
    n = 20
    threads = [threading.Thread(target=pm.deposit, args=(1000,)) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert pm.load_portfolio()["cash"] == n * 1000


# ========== 交易语义 ==========

def test_add_then_close_cash_roundtrip(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 100000, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })
    pm.add_position(
        "600011", "华能", 9.0, 2000, trade_date="2026-06-11",
        **_classification(asof="2026-06-11"),
    )  # -18000
    assert pm.load_portfolio()["cash"] == 82000
    r = pm.close_position("600011", 10.0, trade_date="2026-06-12")        # +20000
    assert r["ok"] and r["pnl"] == 2000
    assert pm.load_portfolio()["cash"] == 102000
    assert pm.load_portfolio()["positions"] == []
    assert pm.monitor_registry.active_stock_map() == {}
    events = signal_ledger.read_events(pm.LEDGER_FILE)
    event_types = [event["event_type"] for event in events]
    assert event_types.count("trade.executed") == 2
    assert "monitor.activated" in event_types
    assert "monitor.closed" in event_types
    monitor_event_types = [
        event["event_type"]
        for event in pm.monitor_registry.monitor_ledger.read_events(
            pm.monitor_registry.MIRROR_LEDGER_FILE
        )
    ]
    assert "monitor.activated" in monitor_event_types
    assert "monitor.closed" in monitor_event_types
    buy_event = next(
        event for event in events
        if event["event_type"] == "trade.executed" and event["payload"]["side"] == "buy"
    )
    sell_event = next(
        event for event in events
        if event["event_type"] == "trade.executed" and event["payload"]["side"] == "sell"
    )
    assert buy_event["links"]["correlation_id"] == sell_event["links"]["correlation_id"]
    assert buy_event["links"]["trade_id"] != sell_event["links"]["trade_id"]


def test_add_weighted_average_cost(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 100000, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })
    pm.add_position("600011", "华能", 10.0, 1000, **_classification())  # 10000
    r = pm.add_position("600011", "华能", 12.0, 1000)  # 12000 → 均价11
    assert r["action"] == "加仓"
    assert r["cost"] == 11.0 and r["shares"] == 2000


def test_identical_same_day_executions_are_not_deduplicated(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 100000, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })

    pm.add_position(
        "600011", "华能", 10.0, 100, trade_date="2026-06-11",
        **_classification(asof="2026-06-11"),
    )
    pm.add_position("600011", "华能", 10.0, 100, trade_date="2026-06-11")

    executions = [
        event for event in signal_ledger.read_events(pm.LEDGER_FILE)
        if event["event_type"] == "trade.executed"
    ]
    assert len(executions) == 2
    assert executions[0]["links"]["trade_id"] != executions[1]["links"]["trade_id"]


def test_same_day_close_is_rejected_by_t1(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 100000, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })
    pm.add_position(
        "600011", "华能", 9.0, 2000, trade_date="2026-06-12",
        **_classification(asof="2026-06-12"),
    )

    result = pm.close_position("600011", 10.0, trade_date="2026-06-12")

    assert "error" in result
    assert result["code"] == "T1_LOCKED"
    assert result["earliest_sell_date"] == "2026-06-15"
    assert pm.load_portfolio()["positions"][0]["shares"] == 2000


def test_stop_loss_report_does_not_claim_same_day_exit_for_locked_shares():
    today = date.today().isoformat()
    portfolio = {
        "cash": 0,
        "positions": [{
            "code": "600011",
            "name": "华能",
            "cost": 10.0,
            "shares": 1000,
            "peak_price": 10.0,
            **_classification(),
            "lots": [{"shares": 1000, "cost": 10.0, "acquired_on": today}],
        }],
    }

    result = pm._apply_prices(
        portfolio,
        {"600011": {"price": 9.0, "change_pct": -10.0}},
    )

    alert = result["alerts"][0]
    assert alert["execution_status"] == "t1_locked"
    assert "最早" in alert["msg"]
    assert "触发硬止损！" not in alert["msg"]


def test_fetch_price_treats_malformed_provider_response_as_missing(monkeypatch):
    monkeypatch.setattr(pm, "fetch_tencent_quote", lambda _code: None)

    assert pm.fetch_price("600011") is None


def test_missing_quote_blocks_new_risk_and_marks_portfolio_valuation_unknown():
    portfolio = {
        "cash": 5000,
        "positions": [{
            "code": "600011",
            "name": "华能",
            "cost": 10.0,
            "shares": 1000,
            "peak_price": 10.0,
            "lots": [{"shares": 1000, "cost": 10.0, "acquired_on": "2026-06-11"}],
        }],
    }

    result = pm._apply_prices(portfolio, {"600011": None})

    assert result["valuation_status"] == "unknown"
    assert result["total_value"] is None
    assert result["known_market_value"] == 0
    assert result["new_risk_blocked"] is True
    assert "valuation_unknown" in result["blocking_reasons"]
    alert = next(a for a in result["alerts"] if a.get("reason_code") == "valuation_unknown")
    assert alert["category"] == "data_quality"
    assert alert["blocks_new_risk"] is True
    assert portfolio["positions"][0]["weight_pct"] is None

    report = pm.format_check_report(portfolio, result)
    assert "valuation_unknown" in report
    assert "无风控警报，持仓正常" not in report
    assert "总资产: 估值未知" in report


def test_missing_quote_preserves_last_known_price_only_as_stale_reference():
    portfolio = {
        "cash": 5000,
        "positions": [{
            "code": "600011",
            "name": "华能",
            "cost": 10.0,
            "shares": 1000,
            "current_price": 10.5,
            "change_pct": 2.0,
            "market_value": 10500.0,
            "pnl": 500.0,
            "pnl_pct": 5.0,
            "weight_pct": 67.7,
            "price_fetched_at": "2026-07-09T15:00:00+08:00",
            "peak_price": 10.5,
            "lots": [{"shares": 1000, "cost": 10.0, "acquired_on": "2026-06-11"}],
        }],
    }

    result = pm._apply_prices(portfolio, {"600011": None})

    position = portfolio["positions"][0]
    assert position["current_price"] == 10.5
    assert position["market_value"] == 10500.0
    assert position["price_stale"] is True
    assert position["quote_status"] == "unavailable"
    assert position["weight_pct"] is None
    assert result["stale_market_value"] == 10500.0
    assert result["total_value"] is None

    report = pm.format_check_report(portfolio, result)
    assert "10.5（陈旧）" in report
    assert "陈旧参考市值" in report
    assert "总资产: **估值未知**" in pm.format_balance(portfolio)


def test_one_missing_quote_makes_all_portfolio_weights_unknown():
    portfolio = {
        "cash": 5000,
        "positions": [
            {
                "code": "600011", "name": "华能", "cost": 10.0, "shares": 1000,
                "peak_price": 10.0,
                "lots": [{"shares": 1000, "cost": 10.0, "acquired_on": "2026-06-11"}],
            },
            {
                "code": "000001", "name": "平安银行", "cost": 10.0, "shares": 1000,
                "peak_price": 10.0,
                "lots": [{"shares": 1000, "cost": 10.0, "acquired_on": "2026-06-11"}],
            },
        ],
    }

    result = pm._apply_prices(
        portfolio,
        {
            "600011": {"price": 11.0, "change_pct": 1.0},
            "000001": None,
        },
    )

    assert result["known_market_value"] == 11000.0
    assert result["total_value"] is None
    assert all(position["weight_pct"] is None for position in portfolio["positions"])


def test_persisted_unknown_valuation_blocks_opening_or_adding_risk(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 100000,
        "positions": [{
            "code": "600011", "name": "华能", "cost": 10.0, "shares": 1000,
            "peak_price": 10.0,
            **_classification(),
            "lots": [{"shares": 1000, "cost": 10.0, "acquired_on": "2026-06-11"}],
        }],
        "total_cost": 10000,
        "cash_reconciled": True,
    })
    portfolio = pm.load_portfolio()
    pm._apply_prices(portfolio, {"600011": None})
    pm.save_portfolio(portfolio)

    result = pm.add_position("000001", "平安银行", 10.0, 1000)

    assert result["code"] == "VALUATION_UNKNOWN"
    assert "error" in result
    assert len(pm.load_portfolio()["positions"]) == 1

    portfolio = pm.load_portfolio()
    pm._apply_prices(portfolio, {"600011": {"price": 10.0, "change_pct": 0.0}})
    pm.save_portfolio(portfolio)

    recovered = pm.add_position(
        "000001", "平安银行", 10.0, 1000,
        **_classification(sector="银行", industry="股份制银行"),
    )

    assert recovered["ok"] is True
    assert len(pm.load_portfolio()["positions"]) == 2


# ========== 输入校验 / 余额不足 ==========

def test_withdraw_insufficient_rejected(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 5000, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })
    r = pm.withdraw(9999)
    assert "error" in r and "ok" not in r
    assert pm.load_portfolio()["cash"] == 5000, "拒绝的出金不得改动余额"


def test_add_insufficient_rejected(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 1000, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })
    r = pm.add_position(
        "600011", "华能", 100.0, 1000, **_classification(),
    )  # 需要 100000
    assert "error" in r
    assert pm.load_portfolio()["positions"] == []


def test_non_positive_inputs_rejected(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 1000, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })
    assert "error" in pm.deposit(0)
    assert "error" in pm.deposit(-100)
    assert "error" in pm.withdraw(-100)
    assert "error" in pm.add_position("600011", "华能", -1.0, 1000)
    assert "error" in pm.add_position("600011", "华能", 1.0, 0)
    assert pm.load_portfolio()["cash"] == 1000, "全部非法输入应为 no-op"


# ========== 打板车道时间止损 + 止盈目标（P1） ==========

def test_add_position_detects_daban_lane_from_latest_signal(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 100000, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })
    monkeypatch.setattr(signal_ledger, "LEDGER_FILE", pm.LEDGER_FILE)
    signal_ledger.append_events([{
        "event_type": "signal.opened",
        "links": signal_ledger.make_links("rec-600011"),
        "payload": {"code": "600011", "strategy_id": "daban:first_board_reseal"},
    }], ledger_file=pm.LEDGER_FILE)

    pm.add_position("600011", "华能", 10.0, 1000, **_classification())

    pos = pm.load_portfolio()["positions"][0]
    assert pos["strategy_id"] == "daban:first_board_reseal"
    assert pos["lane"] == "daban"


def test_add_position_without_matching_signal_has_no_lane(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 100000, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })
    monkeypatch.setattr(signal_ledger, "LEDGER_FILE", pm.LEDGER_FILE)

    pm.add_position("600011", "华能", 10.0, 1000, **_classification())

    pos = pm.load_portfolio()["positions"][0]
    assert pos["strategy_id"] is None
    assert pos["lane"] is None


def test_daban_lane_position_past_time_stop_flags_regardless_of_pnl():
    old_buy_date = _n_trading_days_ago(pm.POSITION_TIME_STOP_DAYS)
    portfolio = {
        "cash": 0,
        "positions": [{
            "code": "600011", "name": "华能", "cost": 10.0, "shares": 1000,
            "peak_price": 10.0, "lane": "daban", "strategy_id": "daban:first_board_reseal",
            "lots": [{"shares": 1000, "cost": 10.0, "acquired_on": old_buy_date}],
            "buy_date": old_buy_date,
        }],
    }

    result = pm._apply_prices(portfolio, {"600011": {"price": 10.1, "change_pct": 1.0}})

    levels = [alert["level"] for alert in result["alerts"]]
    assert "🟠 时间止损" in levels


def test_trend_lane_position_is_not_time_stopped():
    old_buy_date = _n_trading_days_ago(pm.POSITION_TIME_STOP_DAYS + 5)
    portfolio = {
        "cash": 0,
        "positions": [{
            "code": "600011", "name": "华能", "cost": 10.0, "shares": 1000,
            "peak_price": 10.0, "lane": "trend", "strategy_id": "trend_pullback",
            "lots": [{"shares": 1000, "cost": 10.0, "acquired_on": old_buy_date}],
            "buy_date": old_buy_date,
        }],
    }

    result = pm._apply_prices(portfolio, {"600011": {"price": 10.1, "change_pct": 1.0}})

    levels = [alert["level"] for alert in result["alerts"]]
    assert "🟠 时间止损" not in levels


def test_take_profit_target_alert_fires_independently_of_trailing_stop():
    today = date.today().isoformat()
    portfolio = {
        "cash": 0,
        "positions": [{
            "code": "600011", "name": "华能", "cost": 10.0, "shares": 1000,
            "peak_price": 12.0,
            "lots": [{"shares": 1000, "cost": 10.0, "acquired_on": today}],
        }],
    }

    # 现价12.0：涨幅正好=止盈目标20%，且尚未从高点回落，回撤止盈不该触发
    result = pm._apply_prices(portfolio, {"600011": {"price": 12.0, "change_pct": 20.0}})

    levels = [alert["level"] for alert in result["alerts"]]
    assert "🟢 止盈目标" in levels
    assert "🟡 止盈" not in levels


# ========== 止损执行闭环（issue #88：建议→执行的纪律升级） ==========

def _stop_loss_portfolio(buy_date):
    return {
        "cash": 0,
        "positions": [{
            "code": "002842", "name": "翔鹭钨业", "cost": 49.5, "shares": 400,
            "peak_price": 51.4,
            "lots": [{"shares": 400, "cost": 49.5, "acquired_on": buy_date}],
            "buy_date": buy_date,
        }],
    }


def test_stop_loss_first_trigger_persists_trigger_date():
    portfolio = _stop_loss_portfolio(_n_trading_days_ago(5))

    result = pm._apply_prices(portfolio, {"002842": {"price": 44.0, "change_pct": -5.0}})

    pos = portfolio["positions"][0]
    assert pos["stop_loss_triggered_on"] == date.today().isoformat()
    alert = next(a for a in result["alerts"] if "止损" in a["level"])
    assert alert["level"] == "🔴 止损"
    assert alert["overdue_trading_days"] == 0
    assert "今日必须执行" in alert["msg"]


def test_stop_loss_unexecuted_escalates_next_trading_day():
    portfolio = _stop_loss_portfolio(_n_trading_days_ago(5))
    portfolio["positions"][0]["stop_loss_triggered_on"] = _n_trading_days_ago(2)

    result = pm._apply_prices(portfolio, {"002842": {"price": 40.0, "change_pct": -8.0}})

    alert = next(a for a in result["alerts"] if "止损" in a["level"])
    assert alert["level"] == "🔴🔴 止损逾期"
    assert alert["overdue_trading_days"] == 2
    assert "仍未执行" in alert["msg"]


def test_stop_loss_recovery_clears_trigger_state():
    portfolio = _stop_loss_portfolio(_n_trading_days_ago(5))
    portfolio["positions"][0]["stop_loss_triggered_on"] = _n_trading_days_ago(1)

    result = pm._apply_prices(portfolio, {"002842": {"price": 49.0, "change_pct": 2.0}})

    assert "stop_loss_triggered_on" not in portfolio["positions"][0]
    assert not any("止损逾期" in a["level"] for a in result["alerts"])


def test_deep_score_below_red_line_creates_review_only_alert():
    portfolio = _stop_loss_portfolio(_n_trading_days_ago(5))

    # 普通 agent 低分没有绑定硬风险证据，不得直接变成交易动作。
    result = pm._apply_prices(
        portfolio,
        {"002842": {"price": 50.0, "change_pct": 1.0}},
        deep_scores={"002842": 2.0},
    )

    alert = next(a for a in result["alerts"] if a["level"] == "🟠 深研复核")
    assert "研究复核" in alert["msg"]
    assert "必须清仓" not in alert["msg"]
    assert "必须减仓" not in alert["msg"]
    assert alert["deep_score"] == 2.0
    assert alert["review_required"] is True
    assert alert["execution_eligible"] is False
    assert alert["execution_status"] == "review_required"


def test_stale_deep_research_record_cannot_force_exit_alert():
    portfolio = _stop_loss_portfolio(_n_trading_days_ago(5))

    result = pm._apply_prices(
        portfolio,
        {"002842": {"price": 50.0, "change_pct": 1.0}},
        deep_scores={
            "002842": {
                "deep_score": 1.0,
                "stale": True,
                "age_days": 120,
                "freshness_status": "stale",
            }
        },
    )

    alert = next(a for a in result["alerts"] if a["level"] == "🟠 深研复核")
    assert alert["execution_eligible"] is False
    assert alert["freshness_status"] == "stale"
    assert "过期" in alert["msg"]
    assert not any(a["level"] == "🔴 深研红线" for a in result["alerts"])


def test_refresh_prices_preserves_cache_freshness_metadata(tmp_path, monkeypatch):
    import deep_research_cache as drc

    _wire(tmp_path, monkeypatch, initial=_stop_loss_portfolio(_n_trading_days_ago(5)))
    monkeypatch.setattr(
        pm,
        "fetch_price",
        lambda _code: {"price": 50.0, "change_pct": 1.0},
    )
    monkeypatch.setattr(
        drc,
        "read_deep_research",
        lambda _code: {
            "deep_score": 1.0,
            "stale": True,
            "freshness_status": "stale",
            "execution_eligible": False,
        },
    )

    _portfolio, result = pm.refresh_prices()

    alert = next(a for a in result["alerts"] if a["level"] == "🟠 深研复核")
    assert alert["freshness_status"] == "stale"
    assert alert["execution_eligible"] is False


def test_deep_score_above_red_line_no_alert():
    portfolio = _stop_loss_portfolio(_n_trading_days_ago(5))

    result = pm._apply_prices(
        portfolio,
        {"002842": {"price": 50.0, "change_pct": 1.0}},
        deep_scores={"002842": 7.5},
    )

    assert not any(a["level"] == "🔴 深研红线" for a in result["alerts"])
