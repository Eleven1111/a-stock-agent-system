"""Scheduled producer contract for the unified six-strategy evidence."""

import json

from scripts import strategy_evidence_daily as producer


def _write_inputs(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(state))
    stock = state / "skills" / "stock-triage" / "data"
    auction = state / "skills" / "daban-stock-picker" / "data"
    stock.mkdir(parents=True)
    auction.mkdir(parents=True)
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps({
        "asof": "2026-08-22",
        "candidates": [{
            "code": "600001", "sector": "通信", "board_height": 2,
            "market_space_height": 2, "price": 10.0, "first_seal": "09:35",
        }],
    }), encoding="utf-8")
    (auction / "auction_shortlist_latest.json").write_text(json.dumps({
        "asof": "2026-08-22", "shortlist": [{"code": "600001"}],
    }), encoding="utf-8")
    (stock / "hot_money_selection_latest.json").write_text(json.dumps({
        "asof": "2026-08-22", "market_state": {
            "available": True, "dominant_state": "S2", "deteriorating": False,
        },
    }), encoding="utf-8")
    return candidate


def test_producer_fetches_each_bounded_code_once_and_is_immutable(tmp_path, monkeypatch):
    candidate = _write_inputs(tmp_path, monkeypatch)
    minute_calls = []
    pool_calls = []

    def limitups(day):
        pool_calls.append(day)
        return [{"代码": "600001", "所属行业": "通信", "首次封板时间": 93500}]

    def minutes(code, *, market):
        minute_calls.append((code, market))
        return []

    kwargs = dict(
        asof="2026-08-22",
        candidate_path=str(candidate),
        minute_delay_seconds=0,
        limitup_fetcher=limitups,
        minute_fetcher=minutes,
        bar_loader=lambda codes, end, lookback: [],
        sentiment_loader=lambda: [],
    )
    first = producer.run(**kwargs)
    second = producer.run(**kwargs)

    assert first["cohort_codes"] == ["600001"]
    assert first["minute_requested_count"] == 1
    assert first["minute_missing_codes"] == ["600001"]
    assert minute_calls == [("600001", "sh")]
    assert pool_calls == ["20260822"]
    assert second["result_sha256"] == first["result_sha256"]
    assert first["research_only"] is True
    assert first["execution_eligible"] is False


def test_current_market_top_is_carried_into_the_tracking_cohort(tmp_path, monkeypatch):
    candidate = _write_inputs(tmp_path, monkeypatch)
    result = producer.run(
        asof="2026-08-22",
        candidate_path=str(candidate),
        minute_delay_seconds=0,
        limitup_fetcher=lambda day: [
            {"代码": "600001", "所属行业": "通信", "首次封板时间": 93500}
        ],
        minute_fetcher=lambda code, market: [],
        bar_loader=lambda codes, end, lookback: [],
        sentiment_loader=lambda: [],
    )
    assert result["tracked_leaders"] == {
        "600001": {"first_seen": "2026-08-22", "last_seen": "2026-08-22"}
    }
    assert result["cohort_codes"] == ["600001"]


def test_empty_official_pool_fails_closed_when_candidates_show_limitups(tmp_path, monkeypatch):
    candidate = _write_inputs(tmp_path, monkeypatch)
    try:
        producer.run(
            asof="2026-08-22",
            candidate_path=str(candidate),
            minute_delay_seconds=0,
            limitup_fetcher=lambda day: [],
            minute_fetcher=lambda code, market: [],
            bar_loader=lambda codes, end, lookback: [],
            sentiment_loader=lambda: [],
        )
    except ValueError as exc:
        assert "official limitup pool unavailable" in str(exc)
    else:
        raise AssertionError("missing official event source must not look like a zero-event day")


def test_market_prefix_keeps_beijing_codes_out_of_the_shenzhen_endpoint():
    assert producer._market("600001") == "sh"
    assert producer._market("000001") == "sz"
    assert producer._market("920895") == "bj"
