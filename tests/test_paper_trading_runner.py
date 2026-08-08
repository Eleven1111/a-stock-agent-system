from __future__ import annotations

import importlib.util
from pathlib import Path

import paper_trading


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "paper-trading" / "scripts" / "paper_trading_runner.py"
SPEC = importlib.util.spec_from_file_location("paper_trading_runner", SCRIPT)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(runner)


def _config():
    return {
        "schema": "paper_trading_config_v1",
        "version": "paper-chanlun-gate-v1",
        "account": {"initial_cash": 100_000.0, "lot_size": 100, "max_positions": 5, "cash_buffer_pct": 5.0},
        "entry_gate": {
            "minimum_open_score": 80.0,
            "positive_recommendations": ["buy", "add", "conditional_buy"],
            "bullish_chanlun_types": ["third_buy", "bottom_divergence"],
            "bearish_chanlun_types": ["third_sell", "top_divergence"],
            "max_signal_age_bars": 3,
        },
        "execution": {"open_confirmation_not_before": "09:35:00", "maximum_quote_age_seconds": 120, "slippage_bps": 20.0},
    }


def _candidate(code="600001", decision="buy"):
    return {
        "code": code,
        "name": "示例",
        "decision": decision,
        "open_score": 85,
        "strategy_id": "trend:test",
        "sector": "算力",
        "quality_report": {"status": "passed"},
        "execution_controls": {"status": "estimate_only"},
        "execution_plan": {"decision": decision, "position_pct": 10, "max_chase_price": 11, "stop_price": 9, "target_price": 12},
        "research_evidence": {"chanlun": {"signals": [{"type": "third_buy", "idx": 119, "date": "2026-07-13", "signal_age_bars": 0}]}},
    }


def _surface():
    return {
        "schema": "open_confirmation_v3",
        "asof": "2026-07-13",
        "generated_at": "2026-07-13T09:35:20+08:00",
        "status": "ready",
        "input_snapshot": {"snapshot_id": "snap", "source_versions": {"quote": "v1"}},
        "signals": [_candidate(), _candidate("600002", decision="watch")],
    }


def test_open_run_evaluates_every_recommendation_but_only_buys_passed_gate(monkeypatch):
    events = []
    account = paper_trading.default_account(_config())
    monkeypatch.setattr(runner.store, "load_account", lambda config: account)
    monkeypatch.setattr(runner.store, "event_exists", lambda *args: False)

    def append(event_type, *, payload, idempotency_key, config, account_after=None, links=None):
        events.append((event_type, payload, account_after))
        return {"status": "appended"}

    monkeypatch.setattr(runner.store, "append_paper_event", append)
    quotes = {
        "sh600001": {"price": 10, "prev_close": 9.8, "open": 9.9, "high": 10.1, "low": 9.8, "volume": 100_000, "fetched_at": "2026-07-13T09:36:00+08:00"}
    }
    result = runner.run_open(
        _surface(),
        quotes,
        asof="2026-07-13",
        observed_at="2026-07-13T09:36:10+08:00",
        config=_config(),
        risk={"max_single_position_pct": 25, "max_sector_exposure_pct": 40},
    )

    assert result["filled"] == 1
    assert result["rejected"] == 1
    assert any(kind == "paper.account.opened" for kind, _, _ in events)
    assert [kind for kind, _, _ in events].count("paper.candidate_evaluated") == 2
    assert any(kind == "paper.trade.filled" for kind, _, _ in events)
    assert all(payload.get("live_order_sent") is not True for _, payload, _ in events)


def test_open_run_is_idempotent_after_trade_was_recorded(monkeypatch):
    account = paper_trading.default_account(_config())
    monkeypatch.setattr(runner.store, "load_account", lambda config: account)
    monkeypatch.setattr(
        runner.store,
        "event_exists",
        lambda event_type, key: event_type == "paper.trade.filled" and key.endswith("600001:buy"),
    )
    recorded = []
    monkeypatch.setattr(runner.store, "append_paper_event", lambda event_type, **kwargs: recorded.append(event_type) or {"status": "appended"})

    result = runner.run_open(
        _surface(),
        {},
        asof="2026-07-13",
        observed_at="2026-07-13T09:36:10+08:00",
        config=_config(),
        risk={"max_single_position_pct": 25, "max_sector_exposure_pct": 40},
    )
    assert result["reused"] == 1
    assert "paper.trade.filled" not in recorded


def test_paper_account_circuit_breaker_blocks_new_entries(monkeypatch):
    account = paper_trading.default_account(_config())
    monkeypatch.setattr(runner.store, "load_account", lambda config: account)
    monkeypatch.setattr(runner.store, "event_exists", lambda *args: False)
    recorded = []
    monkeypatch.setattr(runner.store, "append_paper_event", lambda event_type, **kwargs: recorded.append((event_type, kwargs["payload"])) or {"status": "appended"})

    result = runner.run_open(
        _surface(),
        {},
        asof="2026-07-13",
        observed_at="2026-07-13T09:36:10+08:00",
        config=_config(),
        risk={"max_single_position_pct": 25, "max_sector_exposure_pct": 40},
        discipline_state={"blocked": True, "reasons": ["week_trade_cap"]},
    )

    assert result["filled"] == 0
    assert result["discipline_state"]["blocked"] is True
    assert any(
        kind == "paper.order.rejected" and payload["reason"] == "paper_discipline_blocked"
        for kind, payload in recorded
    )


def test_monitor_persists_pending_t1_state(monkeypatch):
    account = paper_trading.default_account(_config())
    account["positions"] = [{
        "code": "600001",
        "name": "示例",
        "shares": 100,
        "average_cost": 10.0,
        "cost": 10.0,
        "buy_date": "2026-07-13",
        "peak_price": 10.0,
        "sector": "算力",
        "lane": "trend",
        "stop_price": 9.2,
        "target_price": 12.0,
    }]
    monkeypatch.setattr(runner.store, "load_account", lambda config: account)
    monkeypatch.setattr(
        paper_trading,
        "t1_constraint",
        lambda acquired_on, asof: {"sell_allowed": False, "earliest_sell_date": "2026-07-14"},
    )
    recorded = []
    monkeypatch.setattr(
        runner.store,
        "append_paper_event",
        lambda event_type, **kwargs: recorded.append((event_type, kwargs.get("account_after"))) or {"status": "appended"},
    )
    quote = {"600001": {"price": 9.1, "prev_close": 9.8, "open": 9.5, "high": 9.5, "low": 9.1, "volume": 100_000, "fetched_at": "2026-07-13T14:00:00+08:00"}}

    runner.run_monitor(
        quote,
        asof="2026-07-13",
        observed_at="2026-07-13T14:00:10+08:00",
        config=_config(),
        risk={"stop_loss_pct": -8, "take_profit_pct": 20, "trailing_stop_pct": 5},
        time_stop_sessions=2,
    )

    event_type, snapshot = recorded[0]
    assert event_type == "paper.exit.pending_t1"
    assert snapshot["positions"][0]["pending_exit"]["reason"] == "hard_stop"


def _wire(monkeypatch, account=None):
    account = account or paper_trading.default_account(_config())
    monkeypatch.setattr(runner.store, "load_account", lambda config: account)
    monkeypatch.setattr(runner.store, "event_exists", lambda *args: False)
    monkeypatch.setattr(
        runner.store, "append_paper_event",
        lambda *a, **k: {"status": "appended"},
    )
    return account


def test_run_open_reports_the_real_rejection_reason_not_the_gate_verdict(monkeypatch):
    """门禁通过、后续因缺行情被拒时，运行报告必须写真实原因。

    此前 evaluation 固定携带门禁结论 recommendation_then_chanlun_passed，真实
    拒绝原因（quote_unavailable 等）只进账本、从不进运行报告 —— 于是报告显示
    「allowed=True」却 filled=0，看起来自相矛盾。
    """
    _wire(monkeypatch)

    result = runner.run_open(
        _surface(), {},  # 完全没有行情 → quote_unavailable
        asof="2026-07-13", observed_at="2026-07-13T09:36:10+08:00",
        config=_config(),
        risk={"max_single_position_pct": 25, "max_sector_exposure_pct": 40},
    )

    reasons = {item["code"]: item["reason"] for item in result["evaluations"]}
    assert reasons["600001"] == "quote_unavailable"
    assert reasons["600002"] == "recommendation_not_positive"


def test_run_open_flags_missing_quotes_as_actionable_zero_fill(monkeypatch):
    _wire(monkeypatch)

    result = runner.run_open(
        _surface(), {},
        asof="2026-07-13", observed_at="2026-07-13T09:36:10+08:00",
        config=_config(),
        risk={"max_single_position_pct": 25, "max_sector_exposure_pct": 40},
    )

    assert result["filled"] == 0
    assert result["zero_fill_class"] == "data_anomaly"
    assert result["zero_fill_actionable"] is True
    assert result["zero_fill"]["anomaly_reasons"] == ["quote_unavailable"]


def test_run_open_zero_fill_from_designed_gates_is_not_actionable(monkeypatch):
    """全部候选都是 watch —— 这正是 #174 观察到的形态，属正常 fail-closed。"""
    _wire(monkeypatch)
    surface = {**_surface(), "signals": [
        _candidate("600001", decision="watch"),
        _candidate("600002", decision="watch"),
    ]}

    result = runner.run_open(
        surface, {},
        asof="2026-07-13", observed_at="2026-07-13T09:36:10+08:00",
        config=_config(),
        risk={"max_single_position_pct": 25, "max_sector_exposure_pct": 40},
    )

    assert result["filled"] == 0
    assert result["zero_fill_class"] == "upstream_gate"
    assert result["zero_fill_actionable"] is False
    assert result["zero_fill"]["breakdown"] == {"recommendation_not_positive": 2}


def test_reused_fills_are_not_reported_as_a_data_anomaly(monkeypatch):
    """重跑幂等：当天已成交的候选走 reused 分支，filled 计数为 0 —— 这不是空仓，
    更不是数据异常。若把 reused 的门禁结论当作拒绝原因，重跑就会天天误报。
    """
    _wire(monkeypatch)
    monkeypatch.setattr(
        runner.store, "event_exists",
        lambda event_type, key=None: event_type == "paper.trade.filled",
    )
    quotes = {"sh600001": {"price": 10, "prev_close": 9.8, "open": 9.9, "high": 10.1,
                           "low": 9.8, "volume": 100_000,
                           "fetched_at": "2026-07-13T09:36:00+08:00"}}

    result = runner.run_open(
        _surface(), quotes,
        asof="2026-07-13", observed_at="2026-07-13T09:36:10+08:00",
        config=_config(),
        risk={"max_single_position_pct": 25, "max_sector_exposure_pct": 40},
    )

    assert result["reused"] == 1
    assert result["filled"] == 0
    assert result["zero_fill_class"] is None
    assert result["zero_fill_actionable"] is False


def test_zero_fill_class_reaches_the_cron_artifact_summary(monkeypatch):
    """summarize_output 只保留 schema/status/message + 标量与列表计数，
    zero_fill_class 是字符串会被丢掉 —— 必须借 message 送到运维面上，
    否则这次归因在 cron 产物里看不见，等于没做。"""
    import runtime_context

    _wire(monkeypatch)
    surface = {**_surface(), "signals": [_candidate("600001", decision="watch")]}

    result = runner.run_open(
        surface, {},
        asof="2026-07-13", observed_at="2026-07-13T09:36:10+08:00",
        config=_config(),
        risk={"max_single_position_pct": 25, "max_sector_exposure_pct": 40},
    )
    summary = runtime_context.summarize_output(result, "")

    assert "upstream_gate" in summary["message"]
    assert summary["zero_fill_actionable"] is False


def test_close_report_explains_an_empty_account(monkeypatch):
    """收盘报告要说清账户为什么是空的。

    #174：close 输出 nav=100000 / positions=[] / return_pct=0，没有任何解释。
    打开推送后若不带原因，等于每天推一条「还是 100000」的噪音。
    """
    _wire(monkeypatch)
    monkeypatch.setattr(
        runner, "_day_rejections",
        lambda asof: [{"reason": "recommendation_not_positive"}] * 5,
    )

    result = runner.run_close(
        {}, asof="2026-07-13", observed_at="2026-07-13T15:25:00+08:00", config=_config(),
    )

    assert result["positions"] == []
    assert result["zero_fill_class"] == "upstream_gate"
    assert result["zero_fill_actionable"] is False
    assert "upstream_gate" in result["message"]


def test_close_report_flags_a_data_anomaly_day(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(
        runner, "_day_rejections",
        lambda asof: [{"reason": "quote_unavailable"}],
    )

    result = runner.run_close(
        {}, asof="2026-07-13", observed_at="2026-07-13T15:25:00+08:00", config=_config(),
    )

    assert result["zero_fill_actionable"] is True
    assert "需人工核查" in result["message"]


def test_close_report_stays_quiet_about_zero_fill_when_holding_positions(monkeypatch):
    account = paper_trading.default_account(_config())
    account["positions"] = [{"code": "600001", "shares": 100, "cost": 10.0}]
    _wire(monkeypatch, account)
    monkeypatch.setattr(runner, "_day_rejections", lambda asof: [{"reason": "quote_unavailable"}])

    result = runner.run_close(
        {"sh600001": {"price": 11, "prev_close": 10, "open": 10.5, "high": 11, "low": 10,
                      "volume": 1000, "fetched_at": "2026-07-13T15:25:00+08:00"}},
        asof="2026-07-13", observed_at="2026-07-13T15:25:00+08:00", config=_config(),
    )

    assert result["zero_fill_class"] is None
    # 有持仓时不报归因，但推送标签必须保留
    assert result["message"].startswith("[模拟盘·研究专用]")


def test_day_rejections_reads_the_ledger_for_real(monkeypatch):
    """不 mock _day_rejections 本身 —— 上面三条用例都替换了它，函数体从未被执行，
    曾因此漏掉 signal_ledger 未导入（NameError）。这条真跑函数体。
    """
    events = [
        {"event_type": "paper.order.rejected",
         "payload": {"asof": "2026-07-13", "reason": "quote_unavailable"}},
        {"event_type": "paper.order.rejected",
         "payload": {"asof": "2026-07-12", "reason": "recommendation_not_positive"}},
        {"event_type": "paper.candidate_evaluated",
         "payload": {"asof": "2026-07-13", "reason": "ignored"}},
    ]
    monkeypatch.setattr(runner.signal_ledger, "read_events", lambda path: events)

    assert runner._day_rejections("2026-07-13") == [{"reason": "quote_unavailable"}]


def test_every_pushed_close_report_carries_the_simulated_account_label(monkeypatch):
    """收盘报告是唯一会被推送的一份，会和 portfolio-check 的真实账户并排出现。
    每条都必须带模拟盘标签 —— JSON 里的 research_only 字段挡不住误读。"""
    _wire(monkeypatch)
    monkeypatch.setattr(runner, "_day_rejections", lambda asof: [])

    result = runner.run_close(
        {}, asof="2026-07-13", observed_at="2026-07-13T15:25:00+08:00", config=_config(),
    )

    assert result["message"].startswith("[模拟盘·研究专用]")
    assert result["research_only"] is True
