"""信任档位天花板 — 外部作者的策略永远拿不到实盘权重。

这些用例锁的是 CLAUDE 契约里那条红线的落地：外部策略不是「暂时没通过闸门」，
而是**结构上到不了**带权重的晋升档。两道闸各测一次，且分别单独证明有效。
"""

import json

import pytest

import strategy_registry as sr


def _external(gate):
    return {**gate, "origin": "external_user", "manifest_hash": "sha256:manifest"}


def test_external_origin_is_recorded_with_its_ceiling(tmp_path, verified_gate_factory):
    f = str(tmp_path / "reg.json")
    record = sr.register_gate_result(
        "ext:alice:momo:v1",
        _external(verified_gate_factory("ext:alice:momo:v1")),
        registry_file=f,
    )

    assert record["origin"] == "external_user"
    assert record["manifest_hash"] == "sha256:manifest"
    assert record["maximum_promotion_state"] == "shadow"


def test_external_strategy_never_receives_live_permission(tmp_path, verified_gate_factory):
    """闸一：即使研究证据完整可验，allowed_in_live_agent 也强制为假。"""
    f = str(tmp_path / "reg.json")
    gate = verified_gate_factory("ext:alice:momo:v1")
    record = sr.register_gate_result("ext:alice:momo:v1", _external(gate), registry_file=f)

    assert record["allowed_in_live_agent"] is False
    assert sr.is_allowed_in_live("ext:alice:momo:v1", registry_file=f) is False
    assert sr.live_weight("ext:alice:momo:v1", registry_file=f) == 0.0


def test_the_same_evidence_is_accepted_for_a_first_party_strategy(
    tmp_path, verified_gate_factory
):
    """对照组：证据本身是合格的——上一个用例拦下的确实是 origin，不是证据缺陷。"""
    f = str(tmp_path / "reg.json")
    record = sr.register_gate_result(
        "chanlun_third_buy",
        verified_gate_factory("chanlun_third_buy"),
        registry_file=f,
    )

    assert record["allowed_in_live_agent"] is True
    assert record["origin"] == "first_party"
    assert record["maximum_promotion_state"] == "live"


def _seed_shadow_record(path, strategy_id, origin):
    """直接落盘一条已在 shadow、且许可位为真的记录。

    绕过 start_shadow 的 OOS precommit 前置，是为了让本用例只考一件事：档位
    天花板本身。许可位刻意设成 True，等于假设闸一已被改坏——天花板仍须拦住。
    """
    record = {
        strategy_id: {
            "strategy_id": strategy_id,
            "origin": origin,
            "allowed_in_live_agent": True,
            "gating_status": "enabled",
            "promotion": {
                "state": "shadow",
                "reason": "seeded_for_test",
                "pilot_weight": 0.0,
                "history": [],
            },
        }
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(record, handle)


def test_external_strategy_cannot_be_promoted_past_shadow(tmp_path):
    """闸二：档位天花板独立生效——即便许可位为真也越不过 shadow。"""
    f = str(tmp_path / "reg.json")
    strategy_id = "ext:alice:momo:v1"
    _seed_shadow_record(f, strategy_id, "external_user")

    with pytest.raises(ValueError) as excinfo:
        sr.promote_strategy(strategy_id, "eligible_for_manual_pilot", registry_file=f)

    assert "origin_tier_exceeded" in str(excinfo.value)
    assert sr.promotion_state(strategy_id, registry_file=f)["state"] == "shadow"
    assert sr.live_weight(strategy_id, registry_file=f) == 0.0


def test_first_party_at_the_same_state_is_blocked_by_evidence_not_by_tier(tmp_path):
    """对照组：同样的记录换成第一方，拦它的是证据要求而非档位——证明天花板不是恒抛。"""
    f = str(tmp_path / "reg.json")
    strategy_id = "daban:first_board_reseal"
    _seed_shadow_record(f, strategy_id, "first_party")

    with pytest.raises(ValueError) as excinfo:
        sr.promote_strategy(strategy_id, "eligible_for_manual_pilot", registry_file=f)

    assert "origin_tier_exceeded" not in str(excinfo.value)
    assert "promotion_evidence_missing" in str(excinfo.value)


def test_legacy_records_without_origin_keep_first_party_ceiling(
    tmp_path, verified_gate_factory
):
    """manifest 之前写下的记录不带 origin，必须继续按第一方处理，不被误降级。"""
    f = str(tmp_path / "reg.json")
    record = sr.register_gate_result(
        "trend_pullback",
        verified_gate_factory("trend_pullback"),
        registry_file=f,
    )

    assert record["origin"] == "first_party"
    assert sr.promotion_state("trend_pullback", registry_file=f)["state"] == "research_only"
