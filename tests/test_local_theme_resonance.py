"""issue #260 §5: local_theme_gate_v1 resonance / execution-risk state tests."""

import local_theme_resonance as ltr


def _gate(**overrides):
    base = dict(
        sector="贵金属",
        confirmation_level="auction",
        strong_member_codes=["600001", "600002", "600003", "600004"],
        observed_member_count=6,
        core_code="600001",
        core_sector_rank=1,
        core_decayed=False,
        evidence_types=["breadth", "limitup_cluster", "sector_flow"],
        data_quality_ok=True,
        risk_reviewed=False,
        risk_hard_block=False,
    )
    base.update(overrides)
    return ltr.build_local_theme_gate(base.pop("sector"), **base)


def test_multi_member_resonance_confirms_with_structural_and_secondary_evidence():
    gate = _gate()
    assert gate["resonance_status"] == "confirmed"
    assert gate["participation_scope"] == "local_theme_only"
    assert gate["reason_codes"] == []


def test_electronics_communication_multi_member_also_confirms():
    gate = _gate(
        sector="电子",
        strong_member_codes=["1", "2", "3"],
        core_code="1",
        evidence_types=["breadth", "limitup_cluster", "theme_member_confirmed"],
    )
    assert gate["resonance_status"] == "confirmed"


def test_single_stock_pulse_is_fixed_to_none():
    gate = _gate(strong_member_codes=["600001"], core_sector_rank=1)
    assert gate["resonance_status"] == "none"
    assert gate["reason_codes"] == ["single_stock_pulse"]


def test_leader_isolated_after_ablation_blocks_confirmation():
    """移除核心后剩余强势成员不足 min_strong_members_after_core：孤立单核。"""
    gate = _gate(
        strong_member_codes=["600001", "600002"],
        core_code="600001",
    )
    assert gate["resonance_status"] == "observed"
    assert "leader_isolated" in gate["reason_codes"]


def test_social_media_single_source_is_not_second_evidence_type():
    gate = _gate(evidence_types=["breadth", "limitup_cluster", "social_theme"])
    assert gate["resonance_status"] == "observed"
    assert "social_source_only" in gate["reason_codes"]


def test_sector_flow_expired_counts_as_missing_secondary_evidence():
    """过期资金流不应作为独立证据传入；调用方需先过滤，这里验证只剩结构证据时不确认。"""
    gate = _gate(evidence_types=["breadth", "limitup_cluster"])
    assert gate["resonance_status"] == "observed"
    assert "insufficient_diffusion_evidence" in gate["reason_codes"]


def test_member_coverage_below_threshold_stays_observed_not_confirmed():
    """核心自身不计入 strong_member_codes 时，2 个跟风成员满足消融门槛
    但总强势数仍低于 min_strong_members，须独立于孤立单核判据被拒绝。"""
    gate = _gate(strong_member_codes=["600002", "600003"], core_code="600001", core_sector_rank=1)
    assert gate["resonance_status"] == "observed"
    assert gate["leader_isolated"] is False
    assert "insufficient_strong_members" in gate["reason_codes"]


def test_announcement_hard_risk_blocks_execution_risk_after_review():
    gate = _gate(risk_reviewed=True, risk_hard_block=True)
    assert gate["resonance_status"] == "confirmed"
    assert gate["execution_risk_status"] == "blocked"
    assert "risk_hard_block" in gate["reason_codes"]


def test_execution_risk_stays_pending_before_full_review():
    gate = _gate(risk_reviewed=False)
    assert gate["execution_risk_status"] == "pending"


def test_data_quality_failure_yields_blocked_not_none():
    gate = _gate(data_quality_ok=False, data_quality_reason="quote_coverage_insufficient")
    assert gate["resonance_status"] == "blocked"
    assert any(code.startswith("data_quality:") for code in gate["reason_codes"])


def test_preopen_confirmation_level_caps_resonance_at_observed():
    gate = _gate(confirmation_level="preopen")
    assert gate["resonance_status"] == "observed"
    assert "preopen_cannot_confirm" in gate["reason_codes"]


def test_open_and_intraday_confirmation_levels_allow_confirmed():
    for level in ("auction", "open", "intraday"):
        gate = _gate(confirmation_level=level)
        assert gate["resonance_status"] == "confirmed", level


def test_unknown_confirmation_level_rejected():
    import pytest

    with pytest.raises(ValueError):
        _gate(confirmation_level="lunchtime")


def test_can_upgrade_requires_later_confirmation_level():
    prior = _gate(confirmation_level="preopen")  # observed (capped)
    same_level = _gate(confirmation_level="preopen")
    assert ltr.can_upgrade(prior, same_level) is False


def test_can_upgrade_allows_observed_to_confirmed_on_fresher_stage():
    prior = _gate(confirmation_level="preopen")  # observed
    later = _gate(confirmation_level="auction")  # confirmed
    assert ltr.can_upgrade(prior, later) is True


def test_can_upgrade_rejects_downgrade_from_confirmed_to_observed():
    prior = _gate(confirmation_level="auction")  # confirmed
    later = _gate(confirmation_level="open", strong_member_codes=["600001", "600002"])  # observed
    assert ltr.can_upgrade(prior, later) is False


def test_can_upgrade_from_no_prior_gate_requires_at_least_observed():
    assert ltr.can_upgrade(None, _gate()) is True
    assert ltr.can_upgrade(None, _gate(strong_member_codes=["600001"])) is False
