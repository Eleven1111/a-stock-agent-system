"""Bounded 09:50/13:15 hot-money research checkpoint tests."""

import importlib.util
from pathlib import Path

import pytest
from state_store import atomic_write_json, read_json


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "daban-stock-picker"
    / "scripts"
    / "hot_money_checkpoint.py"
)
SPEC = importlib.util.spec_from_file_location("hot_money_checkpoint", SCRIPT)
checkpoint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checkpoint)


def _candidate(code="sh600001", sector="半导体", qualified=True):
    return {
        "code": code,
        "name": f"股票{code[-6:]}",
        "sector": sector,
        "strategy_id": "daban:mainline_leader_confirm",
        "hot_money_qualified": qualified,
        "open_score": 88.0,
        "selection_context": {
            "window": "09:35",
            "market_timing": {"tier": "发酵", "daban_ready": True},
            "sector": {"name": sector, "rank": 1, "state": "confirmed"},
            "leader": {"rank": 1, "role": "sector_leader", "qualified": qualified},
        },
    }


def _quote(code="sh600001", change_pct=6.0, price=10.6, open_price=10.4):
    return {
        "code": code,
        "name": f"股票{code[-6:]}",
        "price": price,
        "prev_close": 10.0,
        "open": open_price,
        "high": max(price, open_price),
        "low": min(price, open_price),
        "volume": 100_000,
        "change_pct": change_pct,
    }


def test_checkpoint_is_research_only_and_t1_safe():
    result = checkpoint.evaluate_checkpoint(
        [_candidate()],
        {"sh600001": _quote()},
        profile="morning_confirm",
        asof="2026-06-22",
    )

    item = result[0]
    assert item["research_state"] == "confirmed"
    assert item["execution_action"] == "none"
    assert item["same_day_sell_allowed"] is False
    assert item["earliest_sell_date"] > "2026-06-22"
    assert item["selection_context"]["window"] == "09:50"


def test_non_mainline_or_missing_quote_fails_closed():
    result = checkpoint.evaluate_checkpoint(
        [_candidate(qualified=False), _candidate("sh600002")],
        {"sh600001": _quote()},
        profile="morning_confirm",
        asof="2026-06-22",
    )

    by_code = {item["code"]: item for item in result}
    assert by_code["sh600001"]["research_state"] == "invalidated"
    assert by_code["sh600002"]["research_state"] == "invalidated"
    assert "行情" in "；".join(by_code["sh600002"]["reasons"])


def test_sector_relative_rank_can_downgrade_follower():
    candidates = [
        _candidate("sh600001"),
        _candidate("sh600002"),
        _candidate("sh600003"),
    ]
    quotes = {
        "sh600001": _quote("sh600001", 7.0, 10.7),
        "sh600002": _quote("sh600002", 6.0, 10.6),
        "sh600003": _quote("sh600003", 5.0, 10.5),
    }

    result = checkpoint.evaluate_checkpoint(
        candidates,
        quotes,
        profile="morning_confirm",
        asof="2026-06-22",
    )

    assert [item["checkpoint_sector_rank"] for item in result] == [1, 2, 3]
    assert result[2]["research_state"] == "watch"
    assert "板块内强度" in "；".join(result[2]["reasons"])


def test_run_checkpoint_reuses_open_surface_and_writes_immutable_snapshot(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    asof = "2026-06-22"
    atomic_write_json(
        checkpoint.open_confirmation_path(asof),
        {
            "schema": "open_confirmation_v3",
            "status": "ready",
            "asof": asof,
            "source_asof": "2026-06-19",
            "signals": [_candidate()],
        },
    )
    monkeypatch.setattr(
        checkpoint,
        "fetch_quotes",
        lambda codes: {code: _quote(code) for code in codes},
    )
    captured = {}
    monkeypatch.setattr(
        checkpoint.candidate_lifecycle,
        "transition",
        lambda *args, **kwargs: captured.update({"args": args, "kwargs": kwargs}) or {},
    )

    result = checkpoint.run_checkpoint("morning_confirm", asof)

    assert result["status"] == "ready"
    assert result["confirmed_count"] == 1
    assert result["input_snapshot"]["snapshot_id"].startswith("snap-")
    persisted = read_json(checkpoint.latest_output_path("morning_confirm"), {})
    assert persisted["observations"][0]["research_state"] == "confirmed"
    assert persisted["output_snapshot"]["snapshot_id"].startswith("snap-")
    assert captured["args"][1] == "morning_reconfirmed"


def test_load_open_confirmation_rejects_wrong_day(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    atomic_write_json(
        checkpoint.open_confirmation_path("2026-06-22"),
        {"status": "ready", "asof": "2026-06-21", "signals": [_candidate()]},
    )

    with pytest.raises(checkpoint.DataSourceError, match="日期"):
        checkpoint.load_open_confirmation("2026-06-22")
