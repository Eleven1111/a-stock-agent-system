from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

import paper_trading
import paper_trading_store
import signal_ledger


def _config():
    return {
        "version": "paper-chanlun-gate-v1",
        "account": {"initial_cash": 100_000.0},
    }


def test_account_events_are_canonical_and_projection_recovers(tmp_path, monkeypatch):
    ledger = tmp_path / "signal_ledger.jsonl"
    projection = tmp_path / "paper_portfolio.json"
    monkeypatch.setattr(paper_trading_store, "LEDGER_FILE", str(ledger))
    monkeypatch.setattr(paper_trading_store, "ACCOUNT_FILE", str(projection))

    account = paper_trading.default_account({
        "version": "paper-chanlun-gate-v1",
        "account": {"initial_cash": 100_000.0},
    })
    account["cash"] = 90_000.0
    first = paper_trading_store.append_paper_event(
        "paper.trade.filled",
        payload={"trade": {"code": "600001", "side": "buy"}},
        idempotency_key="paper.trade.filled:2026-07-13:600001:buy",
        account_after=account,
        config=_config(),
    )
    duplicate = paper_trading_store.append_paper_event(
        "paper.trade.filled",
        payload={"trade": {"code": "600001", "side": "buy"}},
        idempotency_key="paper.trade.filled:2026-07-13:600001:buy",
        account_after=account,
        config=_config(),
    )

    assert first["status"] == "appended"
    assert duplicate["status"] == "reused"
    assert len(signal_ledger.read_events(str(ledger))) == 1
    projection.unlink()
    recovered = paper_trading_store.load_account(_config())
    assert recovered["cash"] == 90_000.0
    assert projection.exists()


def test_non_account_audit_event_does_not_replace_account(tmp_path, monkeypatch):
    ledger = tmp_path / "signal_ledger.jsonl"
    projection = tmp_path / "paper_portfolio.json"
    monkeypatch.setattr(paper_trading_store, "LEDGER_FILE", str(ledger))
    monkeypatch.setattr(paper_trading_store, "ACCOUNT_FILE", str(projection))

    before = paper_trading_store.load_account(_config())
    result = paper_trading_store.append_paper_event(
        "paper.candidate_evaluated",
        payload={"code": "600001", "allowed": False},
        idempotency_key="paper.candidate_evaluated:2026-07-13:600001",
        config=_config(),
    )
    after = paper_trading_store.load_account(_config())

    assert result["status"] == "appended"
    assert after == before


def test_event_exists_uses_type_and_idempotency(tmp_path, monkeypatch):
    ledger = tmp_path / "signal_ledger.jsonl"
    monkeypatch.setattr(paper_trading_store, "LEDGER_FILE", str(ledger))
    paper_trading_store.append_paper_event(
        "paper.order.rejected",
        payload={"code": "600001"},
        idempotency_key="reject:1",
        config=_config(),
    )
    assert paper_trading_store.event_exists("paper.order.rejected", "reject:1") is True
    assert paper_trading_store.event_exists("paper.trade.filled", "reject:1") is False


def test_paper_discipline_uses_only_paper_account_trades(tmp_path, monkeypatch):
    ledger = tmp_path / "signal_ledger.jsonl"
    monkeypatch.setattr(paper_trading_store, "LEDGER_FILE", str(ledger))
    for day, code in (("2026-07-13", "600001"), ("2026-07-14", "600002"), ("2026-07-15", "600003")):
        paper_trading_store.append_paper_event(
            "paper.trade.filled",
            payload={"trade": {"code": code, "side": "buy", "trade_date": day}},
            idempotency_key=f"buy:{day}:{code}",
            config=_config(),
        )
    signal_ledger.append_event(
        "trade.executed",
        signal_ledger.make_links(signal_id="real-trade"),
        {"action": "close", "trade_date": "2026-07-15", "pnl": -99999},
        ledger_file=str(ledger),
    )

    state = paper_trading_store.assess_paper_discipline(
        asof="2026-07-15",
        total_assets=100_000,
        discipline_config={
            "week_trades_max": 3,
            "day_loss_pct_stop": -2,
            "week_loss_pct_freeze": -5,
            "consecutive_losses_max": 3,
        },
    )
    assert state["blocked"] is True
    assert state["reasons"] == ["week_trade_cap"]


def test_concurrent_duplicate_event_is_appended_once(tmp_path, monkeypatch):
    ledger = tmp_path / "signal_ledger.jsonl"
    projection = tmp_path / "paper_portfolio.json"
    monkeypatch.setattr(paper_trading_store, "LEDGER_FILE", str(ledger))
    monkeypatch.setattr(paper_trading_store, "ACCOUNT_FILE", str(projection))
    account = paper_trading.default_account({
        "version": "paper-chanlun-gate-v1",
        "account": {"initial_cash": 100_000.0},
    })

    def append():
        return paper_trading_store.append_paper_event(
            "paper.account.opened",
            payload={"initial_cash": 100_000.0},
            idempotency_key="paper.account.opened:paper-chanlun-gate-v1",
            account_after=account,
            config=_config(),
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: append(), range(8)))

    assert sum(result["status"] == "appended" for result in results) == 1
    assert len(signal_ledger.read_events(str(ledger))) == 1


def test_transaction_lock_and_event_namespace_are_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(
        paper_trading_store,
        "TRANSACTION_FILE",
        str(tmp_path / "paper_transaction"),
    )
    with paper_trading_store.account_transaction():
        assert (tmp_path / "paper_transaction.lock").exists()

    with pytest.raises(ValueError, match=r"paper\.\*"):
        paper_trading_store.append_paper_event(
            "trade.executed",
            payload={},
            idempotency_key="invalid",
            config=_config(),
        )
