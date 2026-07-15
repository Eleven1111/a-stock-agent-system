from __future__ import annotations

import importlib.util
import sqlite3
import sys
import time
import types
from pathlib import Path

import pytest

import a_share_rules
import market_adapters
import market_snapshot
import provider_contract


def _load_data_cache(name: str = "p2_data_cache"):
    path = Path(__file__).parents[1] / "skills/stock-analyst/scripts/data_cache.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stage_policy(**overrides):
    values = {
        "stage": "open_confirmation",
        "cutoff_time": "09:30:00",
        "timezone_name": "Asia/Shanghai",
        "publication_delay_seconds": 120,
    }
    values.update(overrides)
    return market_snapshot.build_stage_policy(**values)


def test_pit_stage_policy_rejects_future_and_publication_late_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    policy = _stage_policy()

    with pytest.raises(market_snapshot.PointInTimeViolation, match="future_evidence"):
        market_snapshot.write_snapshot(
            "open-confirmation",
            {"price": 10.0},
            trading_date="2026-07-10",
            batch_id="batch-1",
            producer="test",
            event_asof="2026-07-10",
            evidence_time="2026-07-10T09:28:01+08:00",
            captured_at="2026-07-10T09:29:00+08:00",
            decision_mode="replay",
            stage_policy=policy,
        )

    valid = market_snapshot.write_snapshot(
        "open-confirmation",
        {"price": 10.0},
        trading_date="2026-07-10",
        batch_id="batch-1",
        producer="test",
        event_asof="2026-07-10",
        evidence_time="2026-07-10T09:28:00+08:00",
        captured_at="2026-07-10T09:30:00+08:00",
        decision_mode="replay",
        stage_policy=policy,
    )
    assert valid["point_in_time"]["schema"] == "pit_stage_contract_v1"
    assert valid["point_in_time"]["decision_mode"] == "replay"
    assert valid["point_in_time"]["event_asof"] == "2026-07-10"


def test_pit_stage_policy_enforces_timezone_and_capture_cutoff():
    policy = _stage_policy()
    with pytest.raises(market_snapshot.PointInTimeViolation, match="timezone_mismatch"):
        market_snapshot.validate_point_in_time(
            event_asof="2026-07-10",
            evidence_time="2026-07-10T01:28:00+00:00",
            captured_at="2026-07-10T09:29:00+08:00",
            decision_mode="replay",
            stage_policy=policy,
        )


def test_pit_contract_rejects_incomplete_or_naive_time(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    with pytest.raises(market_snapshot.PointInTimeViolation, match="incomplete"):
        market_snapshot.write_snapshot(
            "candidate-input",
            {},
            trading_date="2026-07-10",
            batch_id="batch-3",
            producer="test",
            event_asof="2026-07-10",
        )
    with pytest.raises(market_snapshot.PointInTimeViolation, match="timezone_missing"):
        market_snapshot.validate_point_in_time(
            event_asof="2026-07-10",
            evidence_time="2026-07-10T09:28:00",
            captured_at="2026-07-10T09:30:00+08:00",
            decision_mode="replay",
            stage_policy=_stage_policy(),
        )
    with pytest.raises(market_snapshot.PointInTimeViolation, match="capture_after_cutoff"):
        market_snapshot.validate_point_in_time(
            event_asof="2026-07-10",
            evidence_time="2026-07-10T09:28:00+08:00",
            captured_at="2026-07-10T09:30:01+08:00",
            decision_mode="live",
            stage_policy=_stage_policy(),
        )


def test_materialized_snapshot_propagates_point_in_time_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    result = market_snapshot.materialize_input_snapshot(
        "candidate-input",
        {"bars": []},
        trading_date="2026-07-10",
        batch_id="batch-2",
        producer="test",
        event_asof="2026-07-10",
        evidence_time="2026-07-10T09:28:00+08:00",
        captured_at="2026-07-10T09:30:00+08:00",
        decision_mode="live",
        stage_policy=_stage_policy(),
    )
    assert result["point_in_time"]["decision_mode"] == "live"
    assert market_snapshot.compact_ref(result)["point_in_time"]["stage_policy"]["stage"] == "open_confirmation"


def test_historical_replay_unsupported_never_calls_live_fetcher():
    calls = []

    result = market_adapters.fetch_with_replay_contract(
        lambda: calls.append("called") or [{"date": "2026-07-10"}],
        provider="current-only",
        event_asof="2026-07-09",
        supports_historical_replay=False,
        current_date="2026-07-10",
    )

    assert result["status"] == "historical_replay_unsupported"
    assert result["data"] is None
    assert calls == []
    future = market_adapters.fetch_with_replay_contract(
        lambda: calls.append("future"),
        provider="provider",
        event_asof="2026-07-11",
        supports_historical_replay=True,
        current_date="2026-07-10",
    )
    assert future["status"] == "future_event_asof"
    assert calls == []


def test_series_provenance_tracks_actual_fallback_and_safe_adjustment():
    result = market_adapters.select_series_provider(
        (
            ("provider_a", "a-v1", lambda: []),
            ("provider_b", "b-v2", lambda: [{
                "date": "2026-07-09", "open": 9.0, "high": 10.0,
                "low": 9.0, "close": 10.0, "volume": 100,
            }]),
        ),
        adjustment="unadjusted",
        event_asof="2026-07-09",
        fetched_at="2026-07-10T01:00:00+00:00",
        decision_mode="replay",
    )

    assert result["status"] == "ok"
    assert result["series_provenance"] == {
        "schema": "market_series_provenance_v1",
        "provider": "provider_b",
        "provider_version": "b-v2",
        "adjustment": "unadjusted",
        "event_asof": "2026-07-09",
        "fetched_at": "2026-07-10T01:00:00+00:00",
        "decision_mode": "replay",
    }


def test_canonical_daily_series_propagates_event_asof_and_actual_provider(monkeypatch):
    seen = {}

    def attempts(code, market, days, event_asof, adjustment, decision_mode):
        seen.update({
            "code": code,
            "market": market,
            "days": days,
            "event_asof": event_asof,
            "adjustment": adjustment,
            "decision_mode": decision_mode,
        })
        return (
            ("first", "first-v1", lambda: []),
            ("second", "second-v1", lambda: [{
                "date": event_asof, "open": 9.0, "high": 10.0,
                "low": 9.0, "close": 10.0, "volume": 100,
            }]),
        )

    monkeypatch.setattr(market_adapters, "_daily_series_attempts", attempts)
    result = market_adapters.fetch_a_share_daily_series(
        "600001",
        market="sh",
        days=5,
        event_asof="2026-07-09",
        adjustment="unadjusted",
        decision_mode="replay",
        fetched_at="2026-07-10T01:00:00+00:00",
        use_cache=False,
    )
    assert seen["event_asof"] == "2026-07-09"
    assert seen["decision_mode"] == "replay"
    assert result["series_provenance"]["provider"] == "second"
    assert result["directional_eligible"] is True

    monkeypatch.setattr(
        market_adapters,
        "fetch_a_share_daily_series",
        lambda *args, **kwargs: {"status": "ok", "data": [{"date": "2026-07-09"}]},
    )
    assert market_adapters.fetch_a_share_daily_kline(
        "600001", event_asof="2026-07-09", adjustment="hfq"
    ) == [{"date": "2026-07-09"}]


def test_daily_series_provider_attempts_are_date_bound(monkeypatch):
    monkeypatch.setattr(market_adapters.time, "sleep", lambda _: None)
    fake_akshare = types.SimpleNamespace(
        stock_zh_a_hist_tx=lambda **kwargs: [{
            "date": "2026-07-09", "open": 9, "close": 10,
            "high": 10, "low": 9, "volume": 100, "amount": 1000,
        }]
    )
    fake_adata = types.SimpleNamespace(
        stock=types.SimpleNamespace(
            market=types.SimpleNamespace(get_market=lambda *args, **kwargs: [{
                "date": "2026-07-09", "open": 9, "close": 10,
                "high": 10, "low": 9, "volume": 100, "amount": 1000,
            }])
        )
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_akshare)
    monkeypatch.setitem(sys.modules, "adata", fake_adata)
    monkeypatch.setattr(
        market_adapters,
        "_fetch_eastmoney_push2_kline",
        lambda *args, **kwargs: [{"date": kwargs["event_asof"], "close": 10}],
    )
    attempts = market_adapters._daily_series_attempts(
        "600001", "sh", 5, "2026-07-09", "hfq", "replay"
    )
    assert attempts[0][2]() == []
    assert attempts[1][2]()[0]["date"] == "2026-07-09"
    assert attempts[2][2]() is None        # mootdx: no TCP in CI
    assert attempts[3][2]() is None        # tencent_kline: not mocked
    assert attempts[4][2]()[0]["date"] == "2026-07-09"


def test_series_provider_failures_and_cache_hit_are_explicit(monkeypatch):
    failed = market_adapters.select_series_provider(
        (
            ("broken", "v1", lambda: (_ for _ in ()).throw(RuntimeError("secret"))),
            ("empty", "v1", lambda: []),
        ),
        adjustment="unadjusted",
        event_asof="2026-07-09",
        fetched_at="2026-07-10T01:00:00+00:00",
        decision_mode="replay",
    )
    assert failed["status"] == "data_unavailable"
    assert failed["provider_attempts"] == [
        {"provider": "broken", "error_type": "RuntimeError"},
        {"provider": "empty", "error_type": "empty"},
    ]
    cached = {
        "status": "ok",
        "directional_eligible": True,
        "data": [{
            "date": "2026-07-09", "open": 9.0, "high": 10.0,
            "low": 9.0, "close": 10.0, "volume": 100,
        }],
        "series_provenance": {
            "schema": "market_series_provenance_v1",
            "provider": "cached",
            "provider_version": "v1",
            "adjustment": "unadjusted",
            "event_asof": "2026-07-09",
            "fetched_at": "2026-07-10T01:00:00+00:00",
            "decision_mode": "replay",
        },
    }
    monkeypatch.setattr(market_adapters, "_cache_get", lambda *args, **kwargs: cached)
    assert market_adapters.fetch_a_share_daily_series(
        "600001", event_asof="2026-07-09", adjustment="unadjusted"
    ) is cached


@pytest.mark.parametrize("adjustment", [None, "unknown", "mixed"])
def test_unknown_or_incompatible_adjustment_blocks_direction(adjustment):
    result = market_adapters.validate_series_provenance({
        "schema": "market_series_provenance_v1",
        "provider": "provider_b",
        "provider_version": "b-v2",
        "adjustment": adjustment,
        "event_asof": "2026-07-09",
        "fetched_at": "2026-07-10T01:00:00+00:00",
        "decision_mode": "replay",
    })
    assert result["directional_eligible"] is False
    assert result["reason"] == "adjustment_unknown"


def test_invalid_cached_series_contract_is_not_reused(monkeypatch):
    cached = {
        "status": "blocked",
        "directional_eligible": False,
        "data": [{
            "date": "2026-07-09", "open": 9.0, "high": 10.0,
            "low": 9.0, "close": 10.0, "volume": 100,
        }],
        "series_provenance": {
            "schema": "market_series_provenance_v1",
            "provider": "cached",
            "provider_version": "v1",
            "adjustment": "unadjusted",
            "event_asof": "2026-07-09",
            "fetched_at": "not-a-time",
            "decision_mode": "replay",
        },
    }
    monkeypatch.setattr(market_adapters, "_cache_get", lambda *args, **kwargs: cached)
    monkeypatch.setattr(
        market_adapters,
        "_daily_series_attempts",
        lambda *args: (("fresh", "v2", lambda: [{
            "date": "2026-07-09", "open": 9.0, "high": 10.0,
            "low": 9.0, "close": 10.0, "volume": 100,
        }]),),
    )

    result = market_adapters.fetch_a_share_daily_series(
        "600001", event_asof="2026-07-09", adjustment="unadjusted"
    )

    assert result["status"] == "ok"
    assert result["series_provenance"]["provider"] == "fresh"
    assert result is not cached


def test_nonmapping_cached_provenance_is_rejected_and_refetched(monkeypatch):
    cached = {
        "status": "ok",
        "directional_eligible": True,
        "data": [{
            "date": "2026-07-09", "open": 9.0, "high": 10.0,
            "low": 9.0, "close": 10.0, "volume": 100,
        }],
        "series_provenance": "not-a-mapping",
    }
    monkeypatch.setattr(market_adapters, "_cache_get", lambda *args, **kwargs: cached)
    monkeypatch.setattr(
        market_adapters,
        "_daily_series_attempts",
        lambda *args: (("fresh", "v2", lambda: [{
            "date": "2026-07-09", "open": 9.0, "high": 10.0,
            "low": 9.0, "close": 10.0, "volume": 100,
        }]),),
    )

    result = market_adapters.fetch_a_share_daily_series(
        "600001", event_asof="2026-07-09", adjustment="unadjusted"
    )

    assert result["status"] == "ok"
    assert result["series_provenance"]["provider"] == "fresh"


def test_transport_contract_blocks_http_as_sole_directional_source():
    lower = provider_contract.transport_contract("http://qt.gtimg.cn/q=sh600001")
    secure = provider_contract.transport_contract("https://web.ifzq.gtimg.cn/path")

    assert lower["trust"] == "lower"
    assert lower["directional_eligible"] is False
    assert lower["reason"] == "transport_lower_trust"
    assert secure["trust"] == "authenticated"
    assert secure["directional_eligible"] is True
    with pytest.raises(ValueError, match="https_downgrade"):
        provider_contract.prevent_https_downgrade(
            "https://provider.example/path", "http://provider.example/path"
        )
    provider_contract.prevent_https_downgrade(
        "https://provider.example/path", "https://provider.example/next"
    )
    assert provider_contract.transport_contract("provider-without-scheme")["trust"] == "lower"


def test_data_cache_uses_https_for_tencent_kline_and_marks_http_quote_lower_trust(
    monkeypatch,
):
    module = _load_data_cache("p2_data_cache_transport")
    captured = {}

    class Result:
        fetched_at = "2026-07-10T01:00:00+00:00"

        def __init__(self, data):
            self.data = data

    def fake_json(url, **kwargs):
        captured["kline_url"] = url
        return Result({"data": {"sh600001": {"qfqday": []}}})

    monkeypatch.setattr(module, "request_json", fake_json)
    assert module.fetch_kline_from_tencent("600001", days=1) == []
    assert captured["kline_url"].startswith("https://")

    parts = [""] * 46
    parts[1], parts[2], parts[3], parts[4] = "示例", "600001", "10", "9"
    parts[5], parts[6], parts[31], parts[32] = "9.5", "100", "1", "11.1"
    parts[33], parts[34], parts[37], parts[38], parts[39], parts[45] = (
        "10", "9", "1", "2", "3", "4"
    )
    monkeypatch.setattr(
        module,
        "request_bytes",
        lambda *args, **kwargs: Result(f'v_sh600001="{"~".join(parts)}"'.encode("gbk")),
    )
    quote = module.fetch_realtime(["600001"])["600001"]
    assert quote["transport_trust"] == "lower"
    assert quote["directional_eligible"] is False
    assert quote["fetched_at"] == "2026-07-10T01:00:00+00:00"


def test_strict_price_limit_contract_blocks_unknown_rule_inputs():
    for kwargs in (
        {"listing_stage": None, "is_st": False, "direction": "buy"},
        {"listing_stage": "normal", "is_st": None, "direction": "buy"},
        {"listing_stage": "normal", "is_st": False, "direction": None},
        {"listing_stage": "normal", "is_st": False, "direction": "buy", "listing_date": None},
    ):
        result = a_share_rules.resolve_price_limit_rule(
            code="600001",
            asof="2026-07-10",
            listing_date=kwargs.pop("listing_date", "2020-01-01"),
            **kwargs,
        )
        assert result["status"] == "blocked"
        assert result["reason"] == "rule_unknown"

    known = a_share_rules.resolve_price_limit_rule(
        code="300750",
        asof="2026-07-10",
        listing_date="2018-06-11",
        listing_stage="normal",
        is_st=False,
        direction="buy",
    )
    assert known["status"] == "known"
    assert known["limit_pct"] == 20.0
    initial = a_share_rules.resolve_price_limit_rule(
        code="688001",
        asof="2026-07-10",
        listing_date="2026-07-10",
        listing_stage="initial_no_limit",
        is_st=False,
        direction="sell",
    )
    assert initial["status"] == "known"
    assert initial["limit_pct"] is None


def test_kline_cache_key_ttl_shared_path_and_sqlite_durability(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    module = _load_data_cache("p2_data_cache_valid")
    assert str(module.CACHE_DB).startswith(str(tmp_path))

    now = int(time.time())
    bars = [{"date": "2026-07-09", "open": 9.0, "close": 10.0}]
    module.save_kline_cache(
        "600001",
        bars,
        source="provider_a",
        adjustment="qfq",
        event_asof="2026-07-09",
        cached_at=now,
    )
    ok = module.read_kline_cache(
        "600001",
        provider="provider_a",
        adjustment="qfq",
        event_asof="2026-07-09",
        now_epoch=now + 10,
    )
    assert ok["status"] == "ok"
    assert ok["data"] == bars
    assert module.read_kline_cache(
        "600001",
        provider="provider_a",
        adjustment="hfq",
        event_asof="2026-07-09",
        now_epoch=now + 10,
    )["status"] == "cache_miss"

    with module.get_db() as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] in {1, 2, 3}


def test_kline_cache_stale_and_corrupt_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    module = _load_data_cache("p2_data_cache_degraded")
    now = int(time.time())
    module.save_kline_cache(
        "600001",
        [{"date": "2026-07-09", "close": 10.0}],
        source="provider_a",
        adjustment="qfq",
        event_asof="2026-07-09",
        cached_at=now - module.CACHE_TTL - 1,
    )
    stale = module.read_kline_cache(
        "600001",
        provider="provider_a",
        adjustment="qfq",
        event_asof="2026-07-09",
        now_epoch=now,
    )
    assert stale["status"] == "cache_stale"
    assert stale["data"] is None

    with sqlite3.connect(module.CACHE_DB) as conn:
        conn.execute("UPDATE kline_cache_v2 SET payload='not-json'")
    corrupt = module.read_kline_cache(
        "600001",
        provider="provider_a",
        adjustment="qfq",
        event_asof="2026-07-09",
        now_epoch=now,
    )
    assert corrupt["status"] == "cache_corrupt"
    assert corrupt["data"] is None


def test_execution_scenarios_never_fabricate_limit_queue_or_capacity():
    import execution_model

    result = execution_model.build_execution_scenarios(
        side="buy",
        quantity=1000,
        signal_price=10.0,
        limit_queue=True,
        executable_price=None,
        available_volume=None,
        adv_value=None,
        event_asof="2026-07-10",
    )

    assert result["signal"]["status"] == "signal_only"
    assert result["conditional_fill"]["status"] == "unknown"
    assert result["conservative"]["status"] == "unfilled"
    assert result["capacity"]["status"] == "capacity_unknown"

    evidenced = execution_model.build_execution_scenarios(
        side="sell",
        quantity=1000,
        signal_price=10.0,
        limit_queue=False,
        executable_price=9.9,
        available_volume=2000,
        adv_value=1_000_000,
        event_asof="2026-07-10",
    )
    assert evidenced["conditional_fill"]["status"] == "filled"
    assert evidenced["conservative"]["status"] == "filled"
    assert evidenced["capacity"]["status"] == "estimated"


def test_versioned_fees_and_estimate_only_pnl():
    import execution_model

    buy = execution_model.estimate_trade_cost("buy", 10_000, asof="2026-07-10")
    sell = execution_model.estimate_trade_cost("sell", 11_000, asof="2026-07-10")
    assert buy["rules"]["schema"] == "a_share_fee_schedule_v1"
    assert buy["rules"]["effective_date"] == "2023-08-28"
    assert buy["rules"]["source"]
    assert sell["stamp_duty"] > 0
    assert sell["total"] > buy["total"]

    pnl = execution_model.estimate_round_trip_pnl(
        entry_price=10.0,
        exit_price=11.0,
        quantity=1000,
        asof="2026-07-10",
        corporate_action_status="unknown",
    )
    assert pnl["status"] == "estimate_only"
    assert pnl["reconciliation_required"] is True
    assert pnl["authoritative_source"] == "broker_statement"
    with pytest.raises(ValueError, match="fee_schedule_unknown"):
        execution_model.fee_schedule_for("2023-08-27")
    with pytest.raises(ValueError, match="positive gross_value"):
        execution_model.estimate_trade_cost("buy", 0, asof="2026-07-10")
