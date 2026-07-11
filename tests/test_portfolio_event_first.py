import json

import portfolio_manager as pm
import signal_ledger


def _paths(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(pm, "HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr(pm, "CASHFLOW_FILE", str(tmp_path / "cash.json"))
    monkeypatch.setattr(pm, "LEDGER_FILE", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setattr(
        pm, "PROJECTION_CHECKPOINT_FILE", str(tmp_path / "portfolio_checkpoint.json")
    )
    monkeypatch.setattr(pm.monitor_registry, "activate", lambda *a, **k: {})


def test_cash_event_is_durable_before_cash_projection(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    pm.save_portfolio(pm._default_portfolio())
    order = []
    real_mutate = pm.mutate_json

    def append_event(*args, **kwargs):
        order.append(("event", args[0]))
        return {"event_type": args[0], "links": args[1], "sequence": 1}

    def mutate(*args, **kwargs):
        value = real_mutate(*args, **kwargs)
        order.append(("projection", "portfolio"))
        return value

    monkeypatch.setattr(pm.signal_ledger, "append_event", append_event)
    monkeypatch.setattr(pm, "mutate_json", mutate)
    assert pm.deposit(1000)["ok"] is True
    assert order[:2] == [("event", "cash.deposited"), ("projection", "portfolio")]


def test_trade_event_is_durable_before_position_projection(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    pm.save_portfolio({**pm._default_portfolio(), "cash": 100000, "account_state": "verified"})
    order = []
    real_mutate = pm.mutate_json

    def append_event(*args, **kwargs):
        order.append(("event", args[0]))
        return {"event_type": args[0], "links": args[1], "sequence": 1}

    def mutate(*args, **kwargs):
        value = real_mutate(*args, **kwargs)
        order.append(("projection", "portfolio"))
        return value

    monkeypatch.setattr(pm.signal_ledger, "append_event", append_event)
    monkeypatch.setattr(pm, "mutate_json", mutate)
    result = pm.add_position(
        "600001",
        "示例",
        10,
        100,
        trade_date="2026-07-10",
        sector="半导体",
        classification_source="fixture",
        classification_asof="2026-07-10",
    )
    assert result["ok"] is True
    assert order.index(("event", "trade.executed")) < order.index(
        ("projection", "portfolio")
    )


def test_deposit_projection_crash_recovers_cash_from_canonical_ledger(
    tmp_path, monkeypatch
):
    _paths(tmp_path, monkeypatch)
    pm.save_portfolio(pm._default_portfolio())
    real_mutate = pm.mutate_json

    def crash_after_event(path, mutator, default=None):
        current = pm.read_json(path, default)
        mutator(current)
        raise OSError("projection crash")

    monkeypatch.setattr(pm, "mutate_json", crash_after_event)
    try:
        pm.deposit(1000)
    except OSError:
        pass

    assert pm.read_json(pm.PORTFOLIO_FILE, {})["cash"] == 0
    event = signal_ledger.read_events(pm.LEDGER_FILE)[-1]
    assert event["event_type"] == "cash.deposited"
    assert event["payload"]["portfolio_after"]["cash"] == 1000

    monkeypatch.setattr(pm, "mutate_json", real_mutate)
    assert pm.load_portfolio()["cash"] == 1000
    checkpoint = json.loads(
        (tmp_path / "portfolio_checkpoint.json").read_text(encoding="utf-8")
    )
    assert checkpoint["sequence"] == event["sequence"]


def test_buy_projection_crash_recovers_position_lot_and_trade_once(
    tmp_path, monkeypatch
):
    _paths(tmp_path, monkeypatch)
    pm.save_portfolio(
        {**pm._default_portfolio(), "cash": 100000, "account_state": "verified"}
    )
    real_mutate = pm.mutate_json

    def crash_after_event(path, mutator, default=None):
        current = pm.read_json(path, default)
        mutator(current)
        raise OSError("projection crash")

    monkeypatch.setattr(pm, "mutate_json", crash_after_event)
    try:
        pm.add_position(
            "600001",
            "示例",
            10,
            100,
            trade_date="2026-07-10",
            sector="半导体",
            classification_source="fixture",
            classification_asof="2026-07-10",
        )
    except OSError:
        pass

    event = signal_ledger.read_events(pm.LEDGER_FILE)[-1]
    assert event["event_type"] == "trade.executed"
    assert event["payload"]["portfolio_after"]["positions"][0]["lots"] == [
        {"shares": 100, "cost": 10, "acquired_on": "2026-07-10"}
    ]

    monkeypatch.setattr(pm, "mutate_json", real_mutate)
    first = pm.load_portfolio()
    second = pm.load_portfolio()
    assert first == second
    assert first["cash"] == 99000
    assert first["positions"][0]["shares"] == 100
    assert len(signal_ledger.read_events(pm.LEDGER_FILE)) == 1


def test_portfolio_reconciliation_blocks_new_risk_after_projection_tamper(
    tmp_path, monkeypatch
):
    _paths(tmp_path, monkeypatch)
    pm.save_portfolio(
        {**pm._default_portfolio(), "cash": 100000, "account_state": "verified"}
    )
    assert pm.deposit(1000)["ok"] is True
    portfolio = pm.read_json(pm.PORTFOLIO_FILE, {})
    portfolio["cash"] = 1
    pm.save_portfolio(portfolio)

    result = pm.add_position(
        "600001",
        "示例",
        10,
        100,
        trade_date="2026-07-10",
        sector="半导体",
        classification_source="fixture",
        classification_asof="2026-07-10",
    )

    assert result["code"] == "EVENT_PROJECTION_BLOCKED"
    assert result["blocking_reasons"] == ["event_projection_unreconciled"]


def test_missing_portfolio_projection_rebuilds_from_latest_canonical_snapshot(
    tmp_path, monkeypatch
):
    _paths(tmp_path, monkeypatch)
    pm.save_portfolio(pm._default_portfolio())
    assert pm.deposit(1000)["ok"] is True
    (tmp_path / "portfolio.json").unlink()

    recovered = pm.load_portfolio()

    assert recovered["cash"] == 1000
    assert recovered["event_projection_sequence"] >= 1


def test_cash_mutation_cannot_canonicalize_tampered_projection(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    pm.save_portfolio(pm._default_portfolio())
    assert pm.deposit(1000)["ok"] is True
    events_before = pm.signal_ledger.read_events(pm.LEDGER_FILE)
    pm.save_portfolio({**pm.load_portfolio(), "cash": 1})

    result = pm.deposit(100)

    assert result["code"] == "EVENT_PROJECTION_BLOCKED"
    assert pm.signal_ledger.read_events(pm.LEDGER_FILE) == events_before
