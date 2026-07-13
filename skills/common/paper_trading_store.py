"""Event-first persistence for the research-only paper trading account."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Any, Mapping

import paper_trading
from paths import data_file
import signal_ledger
from state_store import atomic_write_json, file_lock, read_json
import trading_discipline


LEDGER_FILE = signal_ledger.LEDGER_FILE
ACCOUNT_FILE = data_file("paper-trading", "paper_portfolio.json")
TRANSACTION_FILE = data_file("paper-trading", "paper_transaction")


@contextmanager
def account_transaction():
    """Serialize account read-decide-append-project transactions."""
    with file_lock(TRANSACTION_FILE, timeout=15.0):
        yield


def _links(idempotency_key: str, supplied: Mapping[str, Any] | None) -> dict[str, Any]:
    if supplied:
        links = dict(supplied)
        if links.get("correlation_id"):
            return links
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:20]
    return signal_ledger.make_links(signal_id=f"paper-{digest}")


def event_exists(event_type: str, idempotency_key: str) -> bool:
    for event in signal_ledger.read_events(LEDGER_FILE):
        payload = event.get("payload") or {}
        if (
            event.get("event_type") == event_type
            and isinstance(payload, Mapping)
            and payload.get("paper_idempotency_key") == idempotency_key
        ):
            return True
    return False


def load_account(config: Mapping[str, Any]) -> dict[str, Any]:
    """Recover the projection from the canonical ledger whenever it drifts."""
    account = paper_trading.account_from_events(
        signal_ledger.read_events(LEDGER_FILE), config
    )
    projected = read_json(ACCOUNT_FILE, None)
    if projected != account:
        atomic_write_json(ACCOUNT_FILE, account)
    return account


def append_paper_event(
    event_type: str,
    *,
    payload: Mapping[str, Any],
    idempotency_key: str,
    config: Mapping[str, Any],
    account_after: Mapping[str, Any] | None = None,
    links: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not event_type.startswith("paper."):
        raise ValueError("paper event type must use paper.* namespace")
    body = {
        **dict(payload),
        "paper_idempotency_key": idempotency_key,
        "research_only": True,
        "live_policy_effect": "none",
        "live_order_sent": False,
    }
    if account_after is not None:
        body["paper_account_after"] = dict(account_after)
    event = signal_ledger.append_event(
        event_type,
        _links(idempotency_key, links),
        body,
        idempotency_key=idempotency_key,
        ledger_file=LEDGER_FILE,
    )
    if event is None:
        return {"status": "reused", "event_type": event_type}
    if account_after is not None:
        atomic_write_json(ACCOUNT_FILE, dict(account_after))
    return {"status": "appended", "event": event}


def assess_paper_discipline(
    *,
    asof: str,
    total_assets: float,
    discipline_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Reuse the live discipline thresholds over paper trades only."""
    synthetic = []
    for event in signal_ledger.read_events(LEDGER_FILE):
        event_type = str(event.get("event_type") or "")
        if event_type not in {"paper.trade.filled", "paper.trade.closed"}:
            continue
        payload = dict(event.get("payload") or {})
        trade = dict(payload.get("trade") or payload)
        if event_type == "paper.trade.filled" and trade.get("side") == "buy":
            action = "open"
            pnl = None
        elif event_type == "paper.trade.closed" and trade.get("side") == "sell":
            action = "close"
            pnl = trade.get("realized_pnl")
        else:
            continue
        synthetic.append({
            "event_type": "trade.executed",
            "payload": {
                "action": action,
                "trade_date": trade.get("trade_date") or payload.get("asof"),
                "pnl": pnl,
            },
        })
    return trading_discipline.assess_discipline_state(
        synthetic,
        total_assets=total_assets,
        asof=asof,
        config=discipline_config,
    )
