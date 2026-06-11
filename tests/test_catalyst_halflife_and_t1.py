"""催化半衰期分级 + T+1 竞价证伪场景决策树。"""

import importlib.util
from datetime import datetime
from pathlib import Path

import four_dim_scorer as fds

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "daban-stock-picker" / "scripts" / "daban_candidate_api.py"
SPEC = importlib.util.spec_from_file_location("daban_candidate_api", SCRIPT)
api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(api)

NOW = datetime(2026, 6, 11, 12, 0)


def _n(title, date=""):
    return {"title": title, "snippet": "", "source": {}, "date": date}


def test_t1_central_catalyst_slow_decay():
    """中央级催化 20 天后仍保有 0.6 权重；订单类同龄只剩 0.4。"""
    central = fds.news_catalyst_score([_n("国家战略落地", "20 days ago")], NOW)["delta"]
    order = fds.news_catalyst_score([_n("公司中标大额订单", "20 days ago")], NOW)["delta"]
    assert central == round(1.2 * 0.6, 2)   # T1 慢衰减
    assert order == round(0.8 * 0.4, 2)     # T2 快衰减
    assert central > order * 2


def test_t1_fresh_both_full():
    assert fds.freshness_factor(2, slow=True) == 1.0
    assert fds.freshness_factor(2, slow=False) == 1.0
    assert fds.freshness_factor(8, slow=True) == 1.0   # 中央级 10 天内全额
    assert fds.freshness_factor(8, slow=False) == 0.4  # 普通催化 8 天已大幅衰减


def test_t1_scenario_a_strong_seal():
    out = api.t1_scenario({"open_board_count": 0})
    assert out["scenario"] == "A"
    assert "竞价≥+3%" in out["auction_plan"]


def test_t1_scenario_b_resealed():
    out = api.t1_scenario({"open_board_count": 2, "sealed_at_close": True})
    assert out["scenario"] == "B"
    assert "1/3" in out["auction_plan"]


def test_t1_scenario_c_failed_seal():
    out = api.t1_scenario({"open_board_count": 3, "sealed_at_close": False})
    assert out["scenario"] == "C"
    assert "无条件" in out["auction_plan"]


def test_evaluate_candidate_carries_scenario():
    payload = api.example_payload()
    result = api.evaluate_payload(payload)
    plan = result["candidates"][0]["t1_exit_plan"]
    # example 候选 open_board_count=1 → 场景 B
    assert plan["scenario"] == "B"
    assert "auction_plan" in plan
