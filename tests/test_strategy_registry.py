"""策略注册表 — 过闸登记 / 门控停用 / 默认不允许。"""

import strategy_registry as sr


def _gate(strategy_id, allowed, decision="passed_for_reference"):
    return {"strategy_id": strategy_id, "decision": decision,
            "allowed_in_live_agent": allowed, "asof": "2026-06-03", "stats": {}}


def test_register_passed_allows_live(tmp_path):
    f = str(tmp_path / "reg.json")
    sr.register_gate_result("chanlun_third_buy", _gate("chanlun_third_buy", True), registry_file=f)
    assert sr.is_allowed_in_live("chanlun_third_buy", registry_file=f) is True
    assert sr.live_weight("chanlun_third_buy", registry_file=f) == 1.0


def test_unregistered_default_not_allowed(tmp_path):
    f = str(tmp_path / "reg.json")
    assert sr.is_allowed_in_live("never_seen", registry_file=f) is False
    assert sr.live_weight("never_seen", registry_file=f) == 0.0


def test_failed_gate_not_allowed(tmp_path):
    f = str(tmp_path / "reg.json")
    sr.register_gate_result("x", _gate("x", False, decision="failed"), registry_file=f)
    assert sr.is_allowed_in_live("x", registry_file=f) is False


def test_gating_disables_passed_strategy(tmp_path):
    f = str(tmp_path / "reg.json")
    sr.register_gate_result("chanlun_third_buy", _gate("chanlun_third_buy", True), registry_file=f)
    sr.set_gating("chanlun_third_buy", enabled=False, reason="实盘期望<0",
                  expectancy=-0.5, samples=25, registry_file=f)
    # 过闸但实盘门控停用 → 不允许
    assert sr.is_allowed_in_live("chanlun_third_buy", registry_file=f) is False
    rec = sr.get("chanlun_third_buy", registry_file=f)
    assert rec["live_expectancy"] == -0.5
    assert rec["gating_status"] == "disabled"


def test_reenable_after_disable(tmp_path):
    f = str(tmp_path / "reg.json")
    sr.register_gate_result("s", _gate("s", True), registry_file=f)
    sr.set_gating("s", enabled=False, registry_file=f)
    sr.set_gating("s", enabled=True, registry_file=f)
    assert sr.is_allowed_in_live("s", registry_file=f) is True
