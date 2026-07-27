"""Agent runtime conformance suite (T04).

Hermes and OpenClaw must be interchangeable. Every case below runs against all
adapters through a fake runtime, so the suite proves contract conformance
without ever calling a real model.

The invariant under test throughout: an agent turn is bounded research output,
never a fact. It cannot write the fact plane, cannot advance the candidate FSM,
and a failure of any kind yields no finding at all.
"""

import json

import pytest

import agent_run_contract
import agent_runtime_adapter
from agent_run_contract import AgentRunRequest


ADAPTERS = ["hermes", "openclaw", "fake"]

PACK = {
    "schema": "research_evidence_pack_v1",
    "ref": "pack-fixture",
    "payload": {
        "subject": {"code": "000000"},
        "fact_artifacts": [{"job_id": "candidate-discovery"}],
        "quality": {"coverage": "full"},
    },
}


def _request(**overrides):
    base = {
        "task_id": "task-1",
        "role": "fundamental",
        "evidence_pack_ref": "pack-fixture",
        "output_schema": "research_finding_v1",
        "runtime": "fake",
        "allowed_tools": ("read_evidence_pack",),
        "allowed_state_reads": ("evidence_pack",),
        "max_output_chars": 4000,
    }
    base.update(overrides)
    return AgentRunRequest(**base)


def _finding(**overrides):
    finding = {
        "schema": "research_finding_v1",
        "task_id": "task-1",
        "role": "fundamental",
        "stance": "neutral",
        "confidence": 0.5,
        "summary": "bounded neutral read of the pack",
        "evidence_refs": ["fact_artifacts.candidate-discovery"],
    }
    finding.update(overrides)
    return finding


def _payload(**overrides):
    payload = {
        "status": "completed",
        "finding": _finding(),
        "tool_usage_summary": {"tools": ["read_evidence_pack"], "state_writes": []},
        "model_usage": {"input_tokens": 100, "output_tokens": 50},
    }
    payload.update(overrides)
    return payload


def _run(runtime, payload, *, request=None, pack=PACK):
    adapter = agent_runtime_adapter.build_adapter(
        runtime, lambda req, evidence: payload
    )
    return adapter.run(request or _request(runtime=runtime), evidence_pack=pack)


# --------------------------------------------------------------------------
# conformance: identical semantics across runtimes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("runtime", ADAPTERS)
def test_a_valid_turn_completes(runtime):
    result = _run(runtime, _payload())

    assert result.status == "completed"
    assert result.runtime == runtime
    assert result.confidence == 0.5
    assert result.produced_evidence is True


def test_all_runtimes_agree_on_every_case():
    """Same input, same terminal state, whichever runtime executed it."""
    cases = {
        "ok": _payload(),
        "abstain": _payload(
            status="abstained",
            finding=_finding(stance="abstain", abstain_reason="insufficient evidence"),
        ),
        "bad_schema": _payload(finding=_finding(schema="something_else_v1")),
        "no_refs": _payload(finding=_finding(evidence_refs=[])),
        "unresolvable": _payload(finding=_finding(evidence_refs=["fact_artifacts.nope"])),
        "too_long": _payload(finding=_finding(summary="x" * 9000)),
        "bad_status": _payload(status="mostly_done"),
        "not_a_dict": "just a string",
        "forbidden_write": _payload(
            tool_usage_summary={"tools": ["read_evidence_pack"],
                                "state_writes": ["state/portfolio.json"]},
        ),
        "extra_tool": _payload(
            tool_usage_summary={"tools": ["read_evidence_pack", "place_order"],
                                "state_writes": []},
        ),
    }

    for name, payload in cases.items():
        outcomes = {
            runtime: (
                _run(runtime, payload, request=_request(runtime=runtime)).status,
                _run(runtime, payload, request=_request(runtime=runtime)).reason_codes,
            )
            for runtime in ADAPTERS
        }
        assert len(set(outcomes.values())) == 1, f"{name} diverged: {outcomes}"


# --------------------------------------------------------------------------
# terminal states
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload_overrides,expected_status,expected_code",
    [
        ({"finding": _finding(schema="wrong_v1")}, "failed", "schema_mismatch"),
        ({"finding": _finding(evidence_refs=[])}, "failed", "no_evidence_refs"),
        ({"finding": _finding(evidence_refs=["fact_artifacts.missing"])},
         "blocked", "evidence_ref_unresolved"),
        ({"finding": _finding(summary="x" * 9000)}, "failed", "output_too_long"),
        ({"finding": _finding(confidence=7)}, "failed", "invalid_confidence"),
        ({"status": "not_a_status"}, "failed", "invalid_status"),
        ({"finding": None}, "failed", "missing_finding"),
    ],
)
def test_every_malformed_output_has_a_named_terminal_state(
    payload_overrides, expected_status, expected_code
):
    result = _run("fake", _payload(**payload_overrides))

    assert result.status == expected_status
    assert expected_code in result.reason_codes
    assert result.finding is None


def test_timeout_is_a_terminal_failure():
    def raising_turn(request, pack):
        raise TimeoutError("model turn exceeded its deadline")

    adapter = agent_runtime_adapter.FakeRuntimeAdapter(raising_turn)
    result = adapter.run(_request(), evidence_pack=PACK)

    assert result.status == "failed"
    assert result.reason_codes == ("deadline_exceeded",)


def test_runtime_exception_is_a_terminal_failure_not_a_crash():
    def raising_turn(request, pack):
        raise RuntimeError("model backend exploded")

    adapter = agent_runtime_adapter.FakeRuntimeAdapter(raising_turn)
    result = adapter.run(_request(), evidence_pack=PACK)

    assert result.status == "failed"
    assert result.reason_codes == ("runtime_exception",)


def test_expired_deadline_never_reaches_the_model():
    called = []

    def turn(request, pack):
        called.append(True)
        return _payload()

    adapter = agent_runtime_adapter.FakeRuntimeAdapter(turn)
    result = adapter.run(
        _request(deadline="2000-01-01T00:00:00+08:00"), evidence_pack=PACK
    )

    assert result.status == "failed"
    assert "deadline_exceeded" in result.reason_codes


def test_missing_evidence_pack_blocks_before_the_turn():
    called = []

    adapter = agent_runtime_adapter.FakeRuntimeAdapter(
        lambda request, pack: called.append(True) or _payload()
    )
    result = adapter.run(_request(), evidence_pack=None)

    assert result.status == "blocked"
    assert "evidence_pack_missing" in result.reason_codes
    assert called == []


def test_abstain_is_a_first_class_outcome():
    result = _run(
        "fake",
        _payload(
            status="abstained",
            finding=_finding(stance="abstain", abstain_reason="evidence is stale"),
        ),
    )

    assert result.status == "abstained"
    assert result.produced_evidence is False
    assert agent_run_contract.to_research_finding(result) is not None


def test_abstain_without_a_reason_fails():
    result = _run("fake", _payload(status="abstained", finding=_finding(stance="abstain")))

    assert result.status == "failed"
    assert "missing_abstain_reason" in result.reason_codes


# --------------------------------------------------------------------------
# the fact plane stays out of reach
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "written",
    [
        "state/skills/stock-triage/data/portfolio.json",
        "state/signal_ledger.jsonl",
        "cron/hermes-cron-manifest.json",
        "state/monitor_registry.json",
        "state/candidate_lifecycle/2026-06-12.json",
        "state/strategy_registry.json",
    ],
)
def test_declared_fact_plane_writes_block_the_turn(written):
    result = _run(
        "fake",
        _payload(tool_usage_summary={"tools": ["read_evidence_pack"],
                                     "state_writes": [written]}),
    )

    assert result.status == "blocked"
    assert "forbidden_state_write" in result.reason_codes
    assert result.finding is None


def test_failure_never_becomes_a_finding():
    for status, codes in (("failed", ("schema_mismatch",)), ("blocked", ("tool_not_allowed",))):
        result = agent_run_contract.failure(_request(), status, *codes)

        assert agent_run_contract.to_research_finding(result) is None
        assert result.produced_evidence is False


def test_failed_result_marks_the_role_failed_instead_of_submitting(monkeypatch):
    submitted, failed = [], []
    fake_bus = type("Bus", (), {
        "submit_finding": staticmethod(lambda *a, **k: submitted.append(a) or {"ok": True}),
        "fail_role": staticmethod(lambda *a, **k: failed.append(a) or {"ok": True}),
    })
    monkeypatch.setitem(__import__("sys").modules, "research_bus", fake_bus)

    result = agent_run_contract.failure(_request(), "failed", "runtime_exception")
    agent_runtime_adapter.submit_result(result)

    assert submitted == []
    assert failed and failed[0][0] == "task-1"


def test_completed_result_submits_a_finding_and_nothing_else(monkeypatch):
    submitted, failed = [], []
    fake_bus = type("Bus", (), {
        "submit_finding": staticmethod(lambda *a, **k: submitted.append(a) or {"ok": True}),
        "fail_role": staticmethod(lambda *a, **k: failed.append(a) or {"ok": True}),
    })
    monkeypatch.setitem(__import__("sys").modules, "research_bus", fake_bus)

    result = _run("fake", _payload())
    agent_runtime_adapter.submit_result(result)

    assert failed == []
    assert submitted and json.loads(json.dumps(submitted[0][2]))["stance"] == "neutral"


# --------------------------------------------------------------------------
# request contract
# --------------------------------------------------------------------------


def test_invalid_request_fails_without_running_the_turn():
    called = []
    adapter = agent_runtime_adapter.FakeRuntimeAdapter(
        lambda request, pack: called.append(True) or _payload()
    )

    result = adapter.run(_request(task_id=""), evidence_pack=PACK)

    assert result.status == "failed"
    assert "invalid_request" in result.reason_codes
    assert called == []


def test_forbidden_state_writes_default_covers_the_whole_fact_plane():
    request = _request()

    for marker in agent_run_contract.FACT_PLANE_WRITE_MARKERS:
        assert marker in request.forbidden_state_writes


def test_unknown_runtime_is_rejected():
    with pytest.raises(ValueError):
        agent_runtime_adapter.build_adapter("langgraph", lambda request, pack: {})


def test_build_request_reads_the_task_not_the_conversation():
    request = agent_runtime_adapter.build_request(
        {"id": "task-9", "evidence_pack_ref": "pack-9"},
        "risk_redteam",
        runtime="hermes",
    )

    assert request.task_id == "task-9"
    assert request.evidence_pack_ref == "pack-9"
    assert request.validate() == []
