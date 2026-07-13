import reflexivity


def test_confirmed_hot_money_diffusion_is_descriptive_not_live_admission():
    result = reflexivity.assess_candidate(
        {
            "hot_money_qualified": True,
            "leader_rank": 1,
            "ablation": {"structural_leader": True},
            "sector_state": "confirmed",
            "sector_evidence_count": 3,
        },
        {
            "market_state": {"dominant_state": "S3"},
            "crowding_fragility": {"crowding_score": 0.35, "fragility_score": 0.2},
        },
    )

    assert result["schema"] == "reflexivity_state_v1"
    assert result["phase"] == "diffusion"
    assert result["dominant_actor"] == "hot_money"
    assert result["live_effect"] == "none"
    assert result["positive_admission"] is False


def test_isolated_leader_in_weakening_sector_emits_defensive_guard():
    result = reflexivity.assess_candidate(
        {
            "hot_money_qualified": True,
            "leader_rank": 1,
            "ablation": {"structural_leader": False},
            "sector_state": "weakening",
        },
        {
            "market_state": {"dominant_state": "S5"},
            "crowding_fragility": {"crowding_score": 0.55, "fragility_score": 0.6},
        },
    )

    assert result["phase"] == "distribution"
    assert "leader_isolation_exit_v1" in result["defensive_guards"]
    assert result["risk_multiplier"] == 0.0


def test_open_burst_without_multi_source_support_is_only_algorithmic_pattern():
    result = reflexivity.assess_candidate(
        {
            "first_seal_time": "09:30:05",
            "sector_state": "emerging",
            "sector_evidence_count": 1,
        },
        {
            "market_state": {"dominant_state": "S2"},
            "crowding_fragility": {"crowding_score": 0.65, "fragility_score": 0.6},
        },
    )

    assert result["dominant_actor"] == "unknown"
    assert result["actor_probabilities"]["algorithmic_pattern"] > 0
    assert "algorithmic_false_consensus_guard_v1" in result["defensive_guards"]
    assert "quant" not in result["observed_facts"]


def test_missing_inputs_fail_closed_without_inventing_actor_or_phase():
    result = reflexivity.assess_candidate({}, {})

    assert result["status"] == "insufficient_data"
    assert result["phase"] == "unknown"
    assert result["dominant_actor"] == "unknown"
    assert result["risk_multiplier"] == 0.0


def test_reflexivity_output_carries_frozen_strategy_version_and_config_hash():
    config = {
        "schema": "reflexivity_strategy_config_v1",
        "version": "reflexivity-v-test",
        "thresholds": {
            "crowding_climax": 0.6,
            "fragility_climax": 0.55,
            "multi_source_min": 2,
            "open_burst_after": "09:25",
            "open_burst_until": "09:31",
            "algorithmic_pattern_probability": 0.65,
            "open_burst_pattern_probability": 0.25,
            "hot_money_probability": 0.8,
            "algorithmic_guard_multiplier": 0.5,
        },
    }

    result = reflexivity.assess_candidate(
        {"sector_state": "emerging"},
        {"market_state": {"dominant_state": "S2"}},
        config=config,
    )

    assert result["strategy_version"] == "reflexivity-v-test"
    assert len(result["config_sha256"]) == 64
    changed = reflexivity.assess_candidate(
        {"sector_state": "emerging"},
        {"market_state": {"dominant_state": "S2"}},
        config={**config, "version": "reflexivity-v-other"},
    )
    assert changed["config_sha256"] != result["config_sha256"]
