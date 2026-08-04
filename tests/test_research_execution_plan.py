import hashlib
import json

import pytest

from research_execution_plan import compile_execution_plan
from scripts.compile_research_execution_plan import _trusted_approval_path


NOW = "2026-07-02T09:36:00+08:00"


def _hash(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _pit(payload, name, *, asof="2026-07-02T09:35:00+08:00",
         captured_at="2026-07-02T09:35:30+08:00", ttl_seconds=300):
    return {
        **payload,
        "point_in_time": {
            "schema": "research_execution_context_pit_v1",
            "ref": f"{name}:sha256:{_hash(payload)}",
            "sha256": _hash(payload),
            "asof": asof,
            "captured_at": captured_at,
            "ttl_seconds": ttl_seconds,
        },
    }


def _synthesis(verdict="advance", final_stance=None):
    core = {
        "schema": "research_synthesis_v1",
        "task_id": "task-1",
        "subject": {"code": "600519"},
        "generated_at": "2026-07-02T09:34:00+08:00",
        "verdict": verdict,
    }
    if final_stance is not None:
        core["final_stance"] = final_stance
    return {**core, "synthesis_sha256": _hash(core)}


def _proposal(*, schema="research_proposal_v1", verdict="advance",
              synthesis=None):
    synthesis = synthesis or _synthesis(verdict)
    proposal = {
        "schema": schema,
        "task_id": "task-1",
        "policy_gate_required": True,
        "subject": {"code": "600519", "name": "示例"},
        "trading_date": "2026-07-02",
        "created_at": "2026-07-02T09:34:30+08:00",
        "verdict": verdict,
        "synthesis_ref": "board/task-1/synthesis.json",
        "synthesis_sha256": synthesis["synthesis_sha256"],
    }
    if synthesis.get("final_stance") is not None:
        proposal["final_stance"] = synthesis["final_stance"]
    return proposal, synthesis


def _contexts():
    quality = _pit(
        {"schema": "recommendation_quality_v1", "status": "passed"},
        "quality",
    )
    strategy = _pit(
        {
            "schema": "strategy_record_v1",
            "strategy_id": "trend",
            "allowed_in_live_agent": True,
            "gating_status": "enabled",
            "runtime_allowed": True,
        },
        "strategy",
    )
    market = _pit(
        {
            "schema": "research_execution_market_context_v1",
            "code": "600519",
            "name": "示例",
            "price": 10.0,
            "prev_close": 10.0,
            "action": "trend_watch",
            "stage": "open",
            "strategy_lane": "trend",
            "sector": "白酒",
            "tradeability": {"tradeable": True},
            "quality_report": quality,
            "strategy_record": strategy,
            "market_regime": {"regime": "risk_on", "context_status": "ready"},
        },
        "market",
    )
    portfolio = _pit(
        {
            "schema": "research_execution_portfolio_context_v1",
            "cash": 100000,
            "positions": [],
        },
        "portfolio",
    )
    return market, portfolio


def _compile(proposal=None, synthesis=None, market=None, portfolio=None,
             approval=None):
    if proposal is None:
        proposal, default_synthesis = _proposal()
        synthesis = synthesis or default_synthesis
    if market is None or portfolio is None:
        default_market, default_portfolio = _contexts()
        market = market or default_market
        portfolio = portfolio or default_portfolio
    return compile_execution_plan(
        proposal,
        market_context=market,
        portfolio=portfolio,
        synthesis_artifact=synthesis,
        approval_artifact=approval,
        now=NOW,
    )


def test_verified_advance_compiles_but_never_becomes_execution_eligible():
    result = _compile()
    assert result["schema"] == "research_execution_plan_v1"
    assert result["status"] == "ready_for_gate"
    assert result["execution_eligible"] is False
    assert result["live_effect"].startswith("none_until")
    assert result["policy"]["decision_policy"]["decision"] == "buy"
    assert result["policy"]["fresh_quote_verified"] is True
    assert result["policy"]["t1_verified"] is True


def test_forged_proposal_schema_fails_closed():
    proposal, synthesis = _proposal(schema="research_proposal_v999")
    result = _compile(proposal, synthesis)
    assert result["status"] == "blocked"
    assert "proposal_schema_invalid" in result["blocking_checks"]


@pytest.mark.parametrize(
    ("verdict", "final_stance"),
    [("rejected", None), ("watch", None), ("adjudicated", "oppose")],
)
def test_non_supporting_verdicts_fail_closed(verdict, final_stance):
    synthesis = _synthesis(verdict, final_stance)
    proposal, _ = _proposal(verdict=verdict, synthesis=synthesis)
    result = _compile(proposal, synthesis)
    assert result["status"] == "blocked"
    assert "proposal_verdict_not_supporting" in result["blocking_checks"]


def test_pending_unapproved_proposal_without_verifiable_synthesis_fails():
    proposal, _ = _proposal()
    result = _compile(proposal, synthesis={})
    assert result["status"] == "blocked"
    assert "proposal_not_approved_or_synthesis_verified" in result["blocking_checks"]


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("task_id", "proposal_task_id_missing"),
        ("trading_date", "proposal_trading_date_missing"),
        ("created_at", "proposal_created_at_missing"),
    ],
)
def test_proposal_requires_complete_identity(field, reason):
    proposal, synthesis = _proposal()
    proposal.pop(field)
    result = _compile(proposal, synthesis)
    assert result["status"] == "blocked"
    assert reason in result["blocking_checks"]


def test_proposal_requires_subject_code():
    proposal, synthesis = _proposal()
    proposal["subject"].pop("code")
    result = _compile(proposal, synthesis)
    assert result["status"] == "blocked"
    assert "proposal_code_missing" in result["blocking_checks"]


def test_2020_stale_proposal_fails_closed():
    proposal, synthesis = _proposal()
    proposal["trading_date"] = "2020-01-02"
    proposal["created_at"] = "2020-01-02T09:34:30+08:00"
    synthesis_core = {
        key: value for key, value in synthesis.items()
        if key != "synthesis_sha256"
    }
    synthesis_core["generated_at"] = "2020-01-02T09:34:00+08:00"
    synthesis = {**synthesis_core, "synthesis_sha256": _hash(synthesis_core)}
    proposal["synthesis_sha256"] = synthesis["synthesis_sha256"]
    result = _compile(proposal, synthesis)
    assert result["status"] == "blocked"
    assert "proposal_stale" in result["blocking_checks"]
    assert "proposal_trading_date_mismatch" in result["blocking_checks"]
    assert "synthesis_stale" in result["blocking_checks"]


@pytest.mark.parametrize(
    ("created_at", "reason"),
    [
        ("2026-07-02T09:34:30", "proposal_created_at_invalid"),
        ("2026-07-02T09:37:00+08:00", "proposal_future"),
    ],
)
def test_proposal_created_at_must_be_aware_and_not_future(created_at, reason):
    proposal, synthesis = _proposal()
    proposal["created_at"] = created_at
    result = _compile(proposal, synthesis)
    assert result["status"] == "blocked"
    assert reason in result["blocking_checks"]


def test_proposal_trading_date_must_match_created_at_and_current_shanghai_day():
    proposal, synthesis = _proposal()
    proposal["trading_date"] = "2026-07-01"
    result = _compile(proposal, synthesis)
    assert result["status"] == "blocked"
    assert "proposal_trading_date_mismatch" in result["blocking_checks"]


@pytest.mark.parametrize(
    ("generated_at", "reason"),
    [
        ("2026-07-02T09:34:00", "synthesis_generated_at_invalid"),
        ("2026-07-02T09:35:00+08:00", "synthesis_after_proposal"),
        ("2026-07-02T09:37:00+08:00", "synthesis_future"),
        ("2026-06-30T09:34:00+08:00", "synthesis_stale"),
    ],
)
def test_synthesis_chronology_and_freshness_are_verified(generated_at, reason):
    proposal, synthesis = _proposal()
    core = {
        key: value for key, value in synthesis.items()
        if key != "synthesis_sha256"
    }
    core["generated_at"] = generated_at
    synthesis = {**core, "synthesis_sha256": _hash(core)}
    proposal["synthesis_sha256"] = synthesis["synthesis_sha256"]
    result = _compile(proposal, synthesis)
    assert result["status"] == "blocked"
    assert reason in result["blocking_checks"]


def test_proposal_max_age_is_configurable():
    proposal, synthesis = _proposal()
    result = compile_execution_plan(
        proposal,
        market_context=_contexts()[0],
        portfolio=_contexts()[1],
        synthesis_artifact=synthesis,
        now=NOW,
        config={"proposal_max_age_seconds": 30},
    )
    assert result["status"] == "blocked"
    assert "proposal_stale" in result["blocking_checks"]


def test_proposal_self_declared_approval_is_never_trusted():
    proposal, _ = _proposal()
    proposal["approval_status"] = "approved"
    result = _compile(proposal, synthesis=None)
    assert result["status"] == "blocked"
    assert "proposal_not_approved_or_synthesis_verified" in result["blocking_checks"]


def test_approved_proposal_requires_bound_approval_artifact():
    proposal, synthesis = _proposal()
    approval_core = {
        "schema": "research_proposal_approval_v1",
        "task_id": "task-1",
        "status": "approved",
        "reviewer": "human-reviewer",
        "approved_at": "2026-07-02T09:35:00+08:00",
        "proposal_sha256": _hash(proposal),
        "synthesis_sha256": synthesis["synthesis_sha256"],
    }
    approval = {**approval_core, "artifact_sha256": _hash(approval_core)}
    assert _compile(proposal, None, approval=approval)["status"] == "ready_for_gate"

    forged = {**approval, "proposal_sha256": "0" * 64}
    result = _compile(proposal, synthesis={}, approval=forged)
    assert result["status"] == "blocked"
    assert "approval_artifact_invalid" in result["blocking_checks"]


def test_approval_cannot_predate_proposal_or_synthesis():
    proposal, synthesis = _proposal()
    approval_core = {
        "schema": "research_proposal_approval_v1",
        "task_id": "task-1",
        "status": "approved",
        "reviewer": "human-reviewer",
        "approved_at": "2026-07-02T09:33:00+08:00",
        "proposal_sha256": _hash(proposal),
        "synthesis_sha256": synthesis["synthesis_sha256"],
    }
    approval = {**approval_core, "artifact_sha256": _hash(approval_core)}
    result = _compile(proposal, synthesis, approval=approval)
    assert result["status"] == "blocked"
    assert "approval_artifact_invalid" in result["blocking_checks"]


def test_adjudicated_support_with_verified_synthesis_is_allowed_for_review():
    synthesis = _synthesis("adjudicated", "support")
    proposal, _ = _proposal(verdict="adjudicated", synthesis=synthesis)
    result = _compile(proposal, synthesis)
    assert result["status"] == "ready_for_gate"
    assert result["execution_eligible"] is False


def test_tampered_synthesis_is_rejected_even_when_proposal_hash_is_unchanged():
    proposal, synthesis = _proposal()
    synthesis["verdict"] = "rejected"
    result = _compile(proposal, synthesis)
    assert result["status"] == "blocked"
    assert "synthesis_artifact_invalid" in result["blocking_checks"]


def test_proposal_market_code_mismatch_fails_closed():
    proposal, synthesis = _proposal()
    market, portfolio = _contexts()
    market_body = {key: value for key, value in market.items() if key != "point_in_time"}
    market_body["code"] = "000001"
    market = _pit(market_body, "market")
    result = _compile(proposal, synthesis, market, portfolio)
    assert "proposal_market_code_mismatch" in result["blocking_checks"]


@pytest.mark.parametrize("missing", ["market", "portfolio", "quality", "strategy"])
def test_every_decision_context_requires_a_point_in_time_binding(missing):
    proposal, synthesis = _proposal()
    market, portfolio = _contexts()
    if missing == "market":
        market.pop("point_in_time")
    elif missing == "portfolio":
        portfolio.pop("point_in_time")
    elif missing == "quality":
        market["quality_report"].pop("point_in_time")
        market = _pit(
            {key: value for key, value in market.items() if key != "point_in_time"},
            "market",
        )
    else:
        market["strategy_record"].pop("point_in_time")
        market = _pit(
            {key: value for key, value in market.items() if key != "point_in_time"},
            "market",
        )
    result = _compile(proposal, synthesis, market, portfolio)
    assert f"{missing}_context_pit_missing" in result["blocking_checks"]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            {
                "asof": "2026-07-02T09:19:00+08:00",
                "captured_at": "2026-07-02T09:20:00+08:00",
            },
            "market_context_stale",
        ),
        ({"captured_at": "2026-07-02T09:37:00+08:00"}, "market_context_future"),
        ({"sha256": "f" * 64}, "market_context_hash_mismatch"),
    ],
)
def test_stale_future_or_hash_mismatched_context_fails(mutation, reason):
    proposal, synthesis = _proposal()
    market, portfolio = _contexts()
    market["point_in_time"].update(mutation)
    result = _compile(proposal, synthesis, market, portfolio)
    assert reason in result["blocking_checks"]


def test_quote_freshness_is_derived_not_a_boolean_declaration():
    proposal, synthesis = _proposal()
    market, portfolio = _contexts()
    market["fresh_quote_required"] = True
    market["point_in_time"]["captured_at"] = "2026-07-02T09:20:00+08:00"
    result = _compile(proposal, synthesis, market, portfolio)
    assert result["policy"]["fresh_quote_verified"] is False
    assert "fresh_quote_not_verified" in result["blocking_checks"]


def test_missing_quote_fields_fail_closed_even_with_valid_market_pit():
    proposal, synthesis = _proposal()
    market, portfolio = _contexts()
    market_body = {
        key: value for key, value in market.items()
        if key not in {"point_in_time", "price"}
    }
    market = _pit(market_body, "market")
    result = _compile(proposal, synthesis, market, portfolio)
    assert result["policy"]["fresh_quote_verified"] is False
    assert "fresh_quote_not_verified" in result["blocking_checks"]


def test_t1_is_derived_from_portfolio_lots_not_a_boolean_declaration():
    proposal, synthesis = _proposal()
    market, portfolio = _contexts()
    market_body = {key: value for key, value in market.items() if key != "point_in_time"}
    market_body["action"] = "sell"
    market_body["t1_required"] = True
    market = _pit(market_body, "market")
    portfolio_body = {
        key: value for key, value in portfolio.items() if key != "point_in_time"
    }
    portfolio_body["positions"] = [{
        "code": "600519",
        "shares": 100,
        "today_bought_shares": 100,
        "available_shares": 0,
    }]
    portfolio = _pit(portfolio_body, "portfolio")
    result = _compile(proposal, synthesis, market, portfolio)
    assert result["policy"]["t1_verified"] is False
    assert "t1_locked" in result["blocking_checks"]


def test_compiler_never_exposes_an_order_payload_or_live_side_effect():
    result = _compile()
    assert result["execution_eligible"] is False
    assert "order" not in result
    assert "broker" not in result
    assert result["live_effect"].startswith("none_until")


def test_cli_approval_path_is_confined_to_trusted_real_file(tmp_path, monkeypatch):
    state_home = tmp_path / "state"
    trusted = state_home / "approvals" / "research-committee"
    trusted.mkdir(parents=True)
    approval = trusted / "task-1.json"
    approval.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(state_home))

    assert _trusted_approval_path(str(approval)) == approval

    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="trusted approval root"):
        _trusted_approval_path(str(outside))

    symlink = trusted / "linked.json"
    symlink.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        _trusted_approval_path(str(symlink))
