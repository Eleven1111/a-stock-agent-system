"""反馈闭环 — 策略门控评估 / 写注册表 / 被停用策略仓位归零。"""

import performance_tracker as pt
import recommendation_audit as ra
import strategy_registry as sr


def test_evaluate_gating_actions():
    by = {
        "chanlun_third_buy": {"closed": 20, "expectancy": -0.8},        # 负期望→disable
        "daban:first_board_reseal": {"closed": 15, "expectancy": 1.2},  # 正期望→enable
        "small": {"closed": 5, "expectancy": -2.0},                     # 样本不足→skip
        "default": {"closed": 100, "expectancy": -1.0},                 # default 不门控
    }
    decisions = {x["strategy_id"]: x for x in pt.evaluate_strategy_gating(by, min_samples=12)}
    assert decisions["chanlun_third_buy"]["action"] == "disable"
    assert decisions["daban:first_board_reseal"]["action"] == "enable"
    assert decisions["small"]["action"] == "skip"
    assert "default" not in decisions


def test_apply_gating_writes_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pt.apply_strategy_gating([{"strategy_id": "chanlun_third_buy", "action": "disable",
                               "expectancy": -0.5, "closed": 20, "reason": "neg"}])
    rec = sr.get("chanlun_third_buy")
    assert rec["gating_status"] == "disabled"
    assert rec["live_expectancy"] == -0.5


def test_apply_gating_skips_skip(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    applied = pt.apply_strategy_gating([{"strategy_id": "x", "action": "skip",
                                         "expectancy": 0.0, "closed": 3, "reason": "few"}])
    assert applied == []
    assert sr.get("x") is None


def test_position_guidance_gated_off(tmp_path, monkeypatch, verified_gate_factory):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sr.register_gate_result(
        "chanlun_third_buy",
        verified_gate_factory("chanlun_third_buy"),
    )
    sr.set_gating("chanlun_third_buy", enabled=False, reason="实盘期望转负")
    g = ra.position_guidance("chanlun_third_buy", entry_price=10, target_price=12, stop_price=9)
    assert g["method"] == "gated_off"
    assert g["recommended_position_pct"] == 0.0
    assert g["execution_fraction"] == 0.0


def test_position_guidance_not_gated_when_enabled(
    tmp_path,
    monkeypatch,
    verified_gate_factory,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sr.register_gate_result(
        "daban:first_board_reseal",
        verified_gate_factory("daban:first_board_reseal"),
    )
    g = ra.position_guidance("daban:first_board_reseal", entry_price=10, target_price=12, stop_price=9)
    assert g["method"] != "gated_off"
