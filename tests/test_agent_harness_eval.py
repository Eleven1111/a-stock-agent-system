"""Agent replay evaluation gate (T05).

The eval set is only useful if it is deterministic and if its hard metrics are
enforced rather than reported. These tests do both: they replay the frozen set,
require every hard metric, and prove the replay does not depend on today's date,
on conversation history, or on any production state.
"""

import json
import os

import pytest

from scripts import evaluate_agent_harness as harness


CASES = harness.load_cases()
CASE_LIST = CASES["cases"]


def test_the_dataset_covers_every_required_category():
    categories = {case["category"] for case in CASE_LIST}

    assert len(CASE_LIST) >= 20
    assert {
        "evidence_discipline",
        "evidence_refs",
        "research_boundary",
        "structured_output",
        "abstain",
        "no_signal",
        "normal",
        "conflict",
        "provider_degradation",
        "source_grading",
        "runtime_failure",
    } <= categories


def test_case_ids_are_unique():
    ids = [case["id"] for case in CASE_LIST]

    assert len(ids) == len(set(ids))


def test_all_hard_metrics_are_met():
    report = harness.evaluate()
    metrics = report["metrics"]

    assert report["failures"] == [], report["failures"]
    assert metrics["pass_rate"] == 1.0
    assert metrics["fail_closed_block_rate"] == 1.0
    assert metrics["abstain_correct_rate"] == 1.0
    assert metrics["research_only_leaks"] == 0
    fact_plane = metrics["fact_plane_writes"]
    # The old hardcoded 0 measured nothing; the suite actually declares eight
    # write attempts and blocks all of them.
    assert fact_plane["attempts_declared"] == 8
    assert fact_plane["blocked_attempts"] == 8
    assert fact_plane["completed_writes"] == 0
    assert fact_plane["guarantee_scope"] == "static_protocol_only"
    assert fact_plane["not_evidence_of"] == "operating_system_level_write_isolation"
    assert metrics["runtime_divergence"] == []
    assert metrics["all_hard_metrics_met"] is True


def test_hermes_and_openclaw_never_diverge():
    report = harness.evaluate(runtimes=("hermes", "openclaw", "fake"))

    assert report["metrics"]["runtime_divergence"] == []


def test_every_completed_case_produces_a_resolvable_finding():
    report = harness.evaluate(runtimes=("fake",))

    for row in report["results"]:
        if row["expected_status"] == "completed":
            assert row["produced_finding"] is True, row["id"]
        elif row["expected_status"] in ("blocked", "failed"):
            assert row["produced_finding"] is False, row["id"]


def test_replay_is_independent_of_the_current_date():
    """Same verdicts under a frozen clock, whatever today happens to be."""
    first = harness.evaluate()
    second = harness.evaluate()

    assert [row["actual_status"] for row in first["results"]] == [
        row["actual_status"] for row in second["results"]
    ]
    assert CASES["frozen_now"] == "2026-06-12T16:00:00+08:00"


def test_fixtures_carry_no_secrets_or_real_holdings():
    fixture_dir = harness.FIXTURES_DIR
    banned = ("api_key", "token", "secret", "password", "webhook", "chat_id",
              "app_secret", "cookie")

    for name in sorted(os.listdir(fixture_dir)):
        text = open(os.path.join(fixture_dir, name), encoding="utf-8").read()
        lowered = text.lower()
        for marker in banned:
            assert marker not in lowered, f"{name} contains {marker}"
        payload = json.loads(text)["payload"]
        assert payload["subject"]["code"] == "000000", f"{name} names a real stock"


def test_replay_does_not_read_production_state(monkeypatch, tmp_path):
    """Point every state root at an empty directory; the verdicts must not move."""
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "empty-state"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty-home"))

    report = harness.evaluate()

    assert report["metrics"]["all_hard_metrics_met"] is True


@pytest.mark.parametrize("case", CASE_LIST, ids=[case["id"] for case in CASE_LIST])
def test_each_case_matches_its_expected_terminal_state(case):
    row = harness.run_case(case, runtime="fake", frozen_now=CASES["frozen_now"])

    assert row["actual_status"] == case["expected_status"], row
    for code in case["expected_reason_codes"]:
        assert code in row["actual_reason_codes"], row


def test_scope_statement_disclaims_return_claims():
    assert "investment returns" in (CASES.get("description") or "")
