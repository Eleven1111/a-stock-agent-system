"""策略注册表 — 过闸登记 / 门控停用 / 默认不允许。"""

import json

import strategy_registry as sr


def _gate(strategy_id, allowed, decision="passed_for_reference"):
    return {"strategy_id": strategy_id, "decision": decision,
            "allowed_in_live_agent": allowed, "asof": "2026-06-03", "stats": {}}


def test_register_passed_stays_research_only_until_promotion(tmp_path, verified_gate_factory):
    f = str(tmp_path / "reg.json")
    sr.register_gate_result(
        "chanlun_third_buy",
        verified_gate_factory("chanlun_third_buy"),
        registry_file=f,
    )
    assert sr.is_allowed_in_live("chanlun_third_buy", registry_file=f) is False
    assert sr.live_weight("chanlun_third_buy", registry_file=f) == 0.0
    assert sr.promotion_state("chanlun_third_buy", registry_file=f)["state"] == "research_only"


def test_forged_pass_without_artifact_is_not_registered_for_live(tmp_path):
    f = str(tmp_path / "reg.json")
    record = sr.register_gate_result(
        "forged",
        _gate("forged", True),
        registry_file=f,
    )

    assert record["allowed_in_live_agent"] is False
    assert record["evidence_verified"] is False
    assert sr.is_allowed_in_live("forged", registry_file=f) is False


def test_registered_strategy_fails_closed_after_artifact_tampering(
    tmp_path,
    verified_gate_factory,
):
    f = str(tmp_path / "reg.json")
    gate = verified_gate_factory("chanlun_third_buy")
    sr.register_gate_result("chanlun_third_buy", gate, registry_file=f)
    artifact_path = gate["evidence"]["artifact"]
    with open(artifact_path, encoding="utf-8") as handle:
        artifact = json.load(handle)
    artifact["gate_metrics"]["oos_alpha"] = 9.9
    with open(artifact_path, "w", encoding="utf-8") as handle:
        json.dump(artifact, handle)

    assert sr.is_allowed_in_live("chanlun_third_buy", registry_file=f) is False


def test_unregistered_default_not_allowed(tmp_path):
    f = str(tmp_path / "reg.json")
    assert sr.is_allowed_in_live("never_seen", registry_file=f) is False
    assert sr.live_weight("never_seen", registry_file=f) == 0.0


def test_failed_gate_not_allowed(tmp_path):
    f = str(tmp_path / "reg.json")
    sr.register_gate_result("x", _gate("x", False, decision="failed"), registry_file=f)
    assert sr.is_allowed_in_live("x", registry_file=f) is False


def test_gating_disables_passed_strategy(tmp_path, verified_gate_factory):
    f = str(tmp_path / "reg.json")
    sr.register_gate_result(
        "chanlun_third_buy",
        verified_gate_factory("chanlun_third_buy"),
        registry_file=f,
    )
    sr.set_gating("chanlun_third_buy", enabled=False, reason="实盘期望<0",
                  expectancy=-0.5, samples=25, registry_file=f)
    # 过闸但实盘门控停用 → 不允许
    assert sr.is_allowed_in_live("chanlun_third_buy", registry_file=f) is False
    rec = sr.get("chanlun_third_buy", registry_file=f)
    assert rec["live_expectancy"] == -0.5
    assert rec["gating_status"] == "disabled"


def test_reenable_after_disable(tmp_path, verified_gate_factory):
    f = str(tmp_path / "reg.json")
    sr.register_gate_result("s", verified_gate_factory("s"), registry_file=f)
    sr.set_gating("s", enabled=False, registry_file=f)
    sr.set_gating("s", enabled=True, registry_file=f)
    assert sr.is_allowed_in_live("s", registry_file=f) is False
    assert sr.promotion_state("s", registry_file=f)["state"] == "research_only"


def test_legacy_record_without_promotion_fails_closed(tmp_path, verified_gate_factory):
    f = str(tmp_path / "reg.json")
    gate = verified_gate_factory("legacy")
    record = sr.register_gate_result("legacy", gate, registry_file=f)
    record.pop("promotion")
    with open(f, "w", encoding="utf-8") as handle:
        json.dump({"legacy": record}, handle)
    assert sr.is_allowed_in_live("legacy", registry_file=f) is False
    assert sr.live_weight("legacy", registry_file=f) == 0.0
