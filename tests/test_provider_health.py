"""Provider SLO ledger and circuit breaker behaviour."""

import io
import threading
from datetime import datetime, timedelta, timezone

import provider_health
from http_client import HttpClient


def _dt(offset_seconds: int = 0) -> datetime:
    return datetime(2026, 7, 3, 9, 30, tzinfo=timezone.utc) + timedelta(seconds=offset_seconds)


def _seed(provider, endpoint_class, outcomes, *, start=0):
    for i, ok in enumerate(outcomes):
        provider_health.record_result(provider, endpoint_class, ok, now=_dt(start + i))


def test_health_score_reports_samples_and_success_rate(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    _seed("tencent", "quote", [True, True, False, True])

    score = provider_health.health_score("tencent", "quote")

    assert score["samples"] == 4
    assert score["successes"] == 3
    assert score["success_rate"] == 0.75
    assert score["state"] == provider_health.STATE_CLOSED


def test_health_score_with_no_samples_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    score = provider_health.health_score("nobody", "default")

    assert score["samples"] == 0
    assert score["success_rate"] is None
    assert score["state"] == provider_health.STATE_CLOSED


def test_circuit_opens_after_min_samples_below_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    # min_samples=10, open_threshold=0.5 by default: 9 samples must not open it yet.
    _seed("eastmoney", "quote", [False] * 9)
    assert provider_health.health_score("eastmoney", "quote")["state"] == provider_health.STATE_CLOSED

    _seed("eastmoney", "quote", [False], start=9)
    assert provider_health.health_score("eastmoney", "quote")["state"] == provider_health.STATE_OPEN


def test_circuit_stays_closed_when_success_rate_meets_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    _seed("sina", "news", [True] * 6 + [False] * 4)  # 60% success, threshold is 0.5

    assert provider_health.health_score("sina", "news")["state"] == provider_health.STATE_CLOSED


def test_allow_request_blocks_while_open_before_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    _seed("eastmoney", "quote", [False] * 10)
    assert provider_health.health_score("eastmoney", "quote")["state"] == provider_health.STATE_OPEN

    gate = provider_health.allow_request("eastmoney", "quote", now=_dt(11))

    assert gate["allowed"] is False
    assert gate["state"] == provider_health.STATE_OPEN
    assert gate["reason"] == "circuit_open"


def test_allow_request_admits_single_probe_after_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    _seed("eastmoney", "quote", [False] * 10)
    # default cooldown_seconds=300
    after_cooldown = _dt(10 + 301)

    gate = provider_health.allow_request("eastmoney", "quote", now=after_cooldown)

    assert gate["allowed"] is True
    assert gate["state"] == provider_health.STATE_HALF_OPEN
    assert gate["reason"] == "probe_admitted"


def test_allow_request_rejects_second_concurrent_probe(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    _seed("eastmoney", "quote", [False] * 10)
    after_cooldown = _dt(10 + 301)

    first = provider_health.allow_request("eastmoney", "quote", now=after_cooldown)
    second = provider_health.allow_request("eastmoney", "quote", now=after_cooldown)

    assert first["allowed"] is True
    assert second["allowed"] is False
    assert second["reason"] == "probe_in_flight"


def test_concurrent_probe_claims_only_admit_one_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    _seed("eastmoney", "quote", [False] * 10)
    after_cooldown = _dt(10 + 301)

    results = []
    lock = threading.Lock()

    def worker():
        gate = provider_health.allow_request("eastmoney", "quote", now=after_cooldown)
        with lock:
            results.append(gate["allowed"])

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1
    assert results.count(False) == 19


def test_successful_probe_closes_circuit_and_resets_window(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    _seed("eastmoney", "quote", [False] * 10)
    after_cooldown = _dt(10 + 301)
    gate = provider_health.allow_request("eastmoney", "quote", now=after_cooldown)

    provider_health.record_result(
        "eastmoney", "quote", True, now=_dt(10 + 302), probe_token=gate["probe_token"],
    )

    score = provider_health.health_score("eastmoney", "quote")
    assert score["state"] == provider_health.STATE_CLOSED
    assert score["samples"] == 1
    assert score["successes"] == 1


def test_failed_probe_reopens_circuit(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    _seed("eastmoney", "quote", [False] * 10)
    after_cooldown = _dt(10 + 301)
    gate = provider_health.allow_request("eastmoney", "quote", now=after_cooldown)

    provider_health.record_result(
        "eastmoney", "quote", False, now=_dt(10 + 302), probe_token=gate["probe_token"],
    )

    score = provider_health.health_score("eastmoney", "quote")
    assert score["state"] == provider_health.STATE_OPEN


def test_probe_slot_reissued_after_probe_ttl(tmp_path, monkeypatch):
    """C1: a crashed prober must not deadlock the breaker forever."""
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    _seed("eastmoney", "quote", [False] * 10)
    claim_time = _dt(10 + 301)
    first = provider_health.allow_request("eastmoney", "quote", now=claim_time)
    assert first["allowed"] is True
    # Prober crashes: no record_result ever arrives.

    # Before the probe TTL (default 60s) the slot is still held.
    held = provider_health.allow_request("eastmoney", "quote", now=_dt(10 + 301 + 59))
    assert held["allowed"] is False
    assert held["reason"] == "probe_in_flight"

    # After the TTL the claim expires and a fresh probe slot is issued.
    reissued = provider_health.allow_request("eastmoney", "quote", now=_dt(10 + 301 + 60))
    assert reissued["allowed"] is True
    assert reissued["reason"] == "probe_reissued_after_ttl"
    assert reissued["probe_token"]
    assert reissued["probe_token"] != first["probe_token"]


def test_reissued_probe_token_resolves_circuit(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    _seed("eastmoney", "quote", [False] * 10)
    provider_health.allow_request("eastmoney", "quote", now=_dt(10 + 301))
    reissued = provider_health.allow_request("eastmoney", "quote", now=_dt(10 + 301 + 61))

    provider_health.record_result(
        "eastmoney", "quote", True, now=_dt(10 + 301 + 62), probe_token=reissued["probe_token"],
    )

    assert provider_health.health_score("eastmoney", "quote")["state"] == provider_health.STATE_CLOSED


def test_stale_result_without_token_does_not_resolve_half_open(tmp_path, monkeypatch):
    """H1: results not carrying the probe token must not adjudicate the trial."""
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    _seed("eastmoney", "quote", [False] * 10)
    provider_health.allow_request("eastmoney", "quote", now=_dt(10 + 301))

    # A stale in-flight success from before the circuit opened arrives late.
    provider_health.record_result("eastmoney", "quote", True, now=_dt(10 + 302))
    assert provider_health.health_score("eastmoney", "quote")["state"] == provider_health.STATE_HALF_OPEN

    # A stale failure must not reopen it either.
    provider_health.record_result("eastmoney", "quote", False, now=_dt(10 + 303))
    assert provider_health.health_score("eastmoney", "quote")["state"] == provider_health.STATE_HALF_OPEN


def test_stale_result_with_mismatched_token_does_not_resolve_half_open(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    _seed("eastmoney", "quote", [False] * 10)
    gate = provider_health.allow_request("eastmoney", "quote", now=_dt(10 + 301))

    provider_health.record_result(
        "eastmoney", "quote", True, now=_dt(10 + 302), probe_token="not-the-real-token",
    )
    assert provider_health.health_score("eastmoney", "quote")["state"] == provider_health.STATE_HALF_OPEN

    # The genuine probe result still resolves afterwards.
    provider_health.record_result(
        "eastmoney", "quote", True, now=_dt(10 + 303), probe_token=gate["probe_token"],
    )
    assert provider_health.health_score("eastmoney", "quote")["state"] == provider_health.STATE_CLOSED


def test_stale_results_in_half_open_still_land_in_window(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    _seed("eastmoney", "quote", [False] * 10)
    provider_health.allow_request("eastmoney", "quote", now=_dt(10 + 301))

    provider_health.record_result("eastmoney", "quote", True, now=_dt(10 + 302))

    score = provider_health.health_score("eastmoney", "quote")
    assert score["samples"] == 11  # bookkeeping only, no state change
    assert score["state"] == provider_health.STATE_HALF_OPEN


def test_window_size_is_bounded_by_config(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    _seed("tencent", "quote", [True] * 250)

    score = provider_health.health_score("tencent", "quote")
    assert score["samples"] == 200


def test_endpoint_classes_are_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    _seed("tencent", "quote", [False] * 10)
    _seed("tencent", "news", [True] * 10)

    assert provider_health.health_score("tencent", "quote")["state"] == provider_health.STATE_OPEN
    assert provider_health.health_score("tencent", "news")["state"] == provider_health.STATE_CLOSED


def test_summary_covers_all_recorded_providers(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    _seed("tencent", "quote", [True, True])
    _seed("eastmoney", "flow", [False, False])

    report = provider_health.summary()

    assert report["schema"] == "a_stock_provider_health_summary_v1"
    assert "tencent" in report["providers"]
    assert "eastmoney" in report["providers"]
    assert report["providers"]["tencent"]["quote"]["samples"] == 2
    assert report["providers"]["eastmoney"]["flow"]["samples"] == 2


def test_summary_with_no_data_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    report = provider_health.summary()

    assert report["providers"] == {}


def test_http_client_records_transport_health_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    client = HttpClient("tencent", opener=lambda request, timeout=None: io.BytesIO(b"payload"))

    client.request_bytes("https://example.invalid/quote")

    assert provider_health.health_score("tencent", "default")["samples"] == 1


def test_suppress_transport_recording_skips_http_client_recording(tmp_path, monkeypatch):
    """H2: inside the suppression context http_client must not record."""
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    client = HttpClient("tencent", opener=lambda request, timeout=None: io.BytesIO(b"payload"))

    with provider_health.suppress_transport_recording():
        client.request_bytes("https://example.invalid/quote")

    assert provider_health.health_score("tencent", "default")["samples"] == 0

    client.request_bytes("https://example.invalid/quote")  # context exited: records again
    assert provider_health.health_score("tencent", "default")["samples"] == 1
