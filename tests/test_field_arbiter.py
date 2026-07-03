"""Field-level multi-source arbitration."""

from datetime import datetime, timedelta, timezone

import field_arbiter
import provider_health


def _dt(offset_seconds: int = 0) -> datetime:
    return datetime(2026, 7, 3, 9, 30, tzinfo=timezone.utc) + timedelta(seconds=offset_seconds)


def test_resolve_uses_first_source_in_priority_order(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    fetchers = [
        ("eastmoney", lambda: {"flow": 1.0}),
        ("tencent", lambda: {"flow": 2.0}),
    ]

    result = field_arbiter.resolve("capital_flow", fetchers, now=_dt())

    assert result["status"] == "ok"
    assert result["provider"] == "eastmoney"
    assert result["data"] == {"flow": 1.0}
    assert "degraded" not in result


def test_resolve_falls_back_to_next_source_on_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    def _broken():
        raise RuntimeError("eastmoney down")

    fetchers = [
        ("eastmoney", _broken),
        ("tencent", lambda: {"flow": 2.0}),
    ]

    result = field_arbiter.resolve("capital_flow", fetchers, now=_dt())

    assert result["status"] == "ok"
    assert result["provider"] == "tencent"
    assert result["degraded"]["failures"][0]["provider"] == "eastmoney"


def test_resolve_skips_sources_with_open_circuit(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    for i in range(10):
        provider_health.record_result("eastmoney", "default", False, now=_dt(i))
    assert provider_health.health_score("eastmoney", "default")["state"] == provider_health.STATE_OPEN

    calls = []

    def _eastmoney():
        calls.append("eastmoney")
        return {"flow": 1.0}

    fetchers = [
        ("eastmoney", _eastmoney),
        ("tencent", lambda: {"flow": 2.0}),
    ]

    result = field_arbiter.resolve("capital_flow", fetchers, now=_dt(10))

    assert calls == []  # circuit-open source must never be called
    assert result["status"] == "ok"
    assert result["provider"] == "tencent"
    assert result["degraded"]["skipped"][0]["provider"] == "eastmoney"
    assert result["degraded"]["skipped"][0]["reason"] == "circuit_open"


def test_resolve_fails_closed_when_all_sources_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    def _broken_a():
        raise RuntimeError("a down")

    def _broken_b():
        raise ValueError("b down")

    fetchers = [("a", _broken_a), ("b", _broken_b)]

    result = field_arbiter.resolve("capital_flow", fetchers, now=_dt())

    assert result["status"] == "error"
    assert result["data"] is None
    assert result["provider"] == "capital_flow_chain"
    assert len(result["degraded"]["failures"]) == 2


def test_resolve_with_no_fetchers_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    result = field_arbiter.resolve("capital_flow", [], now=_dt())

    assert result["status"] == "error"
    assert result["data"] is None


def test_field_chain_reads_configured_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    chain = field_arbiter.field_chain("capital_flow")

    assert chain == ["eastmoney", "tencent"]


def test_field_chain_unknown_type_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    assert field_arbiter.field_chain("does_not_exist") == []


def test_compare_sources_consistent_within_tolerance():
    result = field_arbiter.compare_sources(
        "capital_flow", {"eastmoney": 100.0, "tencent": 101.0}, tolerance_pct=5.0,
    )

    assert result["consistent"] is True
    assert result["quality_flag"] is None


def test_compare_sources_flags_mismatch_beyond_tolerance():
    result = field_arbiter.compare_sources(
        "capital_flow", {"eastmoney": 100.0, "tencent": 150.0}, tolerance_pct=5.0,
    )

    assert result["consistent"] is False
    assert result["quality_flag"] == "cross_source_mismatch"
    assert result["mismatches"][0]["providers"] == ["eastmoney", "tencent"]


def test_compare_sources_with_single_value_is_trivially_consistent():
    result = field_arbiter.compare_sources("capital_flow", {"eastmoney": 100.0}, tolerance_pct=5.0)

    assert result["consistent"] is True
    assert result["quality_flag"] is None


def test_compare_sources_ignores_non_numeric_values():
    result = field_arbiter.compare_sources(
        "capital_flow",
        {"eastmoney": 100.0, "tencent": "n/a"},
        tolerance_pct=5.0,
    )

    assert result["consistent"] is True
    assert result["values"] == {"eastmoney": 100.0}


def test_resolve_runs_fetchers_inside_transport_suppression(tmp_path, monkeypatch):
    """H2: one physical request is recorded once, in the arbiter bucket only."""
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    observed = []

    def fetcher():
        observed.append(provider_health.transport_recording_suppressed())
        return {"flow": 1.0}

    result = field_arbiter.resolve("capital_flow", [("tencent", fetcher)], now=_dt())

    assert result["status"] == "ok"
    assert observed == [True]
    assert provider_health.health_score("tencent", "default")["samples"] == 1


def test_single_http_request_via_arbiter_is_not_double_counted(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    import io
    from http_client import HttpClient

    client = HttpClient("tencent", opener=lambda request, timeout=None: io.BytesIO(b"payload"))

    def fetcher():
        return client.request_bytes("https://example.invalid/quote").data

    result = field_arbiter.resolve(
        "quote", [("tencent", fetcher)], endpoint_class="quote", now=_dt(),
    )

    assert result["status"] == "ok"
    assert provider_health.health_score("tencent", "quote")["samples"] == 1
    assert provider_health.health_score("tencent", "default")["samples"] == 0


def test_resolve_passes_probe_token_so_probe_success_closes_circuit(tmp_path, monkeypatch):
    """H1 integration: the arbiter's admitted probe carries the token through."""
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    for i in range(10):
        provider_health.record_result("eastmoney", "default", False, now=_dt(i))
    assert provider_health.health_score("eastmoney", "default")["state"] == provider_health.STATE_OPEN

    after_cooldown = _dt(10 + 301)
    result = field_arbiter.resolve(
        "capital_flow", [("eastmoney", lambda: {"flow": 1.0})], now=after_cooldown,
    )

    assert result["status"] == "ok"
    assert result["provider"] == "eastmoney"
    assert provider_health.health_score("eastmoney", "default")["state"] == provider_health.STATE_CLOSED
