"""板块三池 + 多标签 regime + 合成分。

这份合成分最容易被误读成「RotationStartScore 的实现」，所以第一组断言就是**它不是**：
报告九项里五项我们没有数据源，能算的四项只占 0.41，而且全是价量。产物必须把这件事
写出来（``missing_weight_share``），置信度恒低。

其余断言围绕同一条纪律：缺证据 ≠ 满足。
- 分项缺失不得补中性值（RS 缺失补 50 会让一个没有趋势的板块看起来中规中矩）；
- 四重共振里任何一项不可得都不算满足；
- 数据不足落 ``unavailable`` 而不是 ``watch`` —— watch 是一个可以被跟进的标签。
"""

from __future__ import annotations

import copy

import pytest
import yaml

import sector_rotation_pools as srp
from config_registry import config_path


def _pool(**overrides):
    kwargs = {
        "rs_slope": 0.001,
        "breadth": 0.70,
        "crowding_score": 30.0,
        "crowding_state": "NORMAL",
        "fake_risk": 20.0,
    }
    kwargs.update(overrides)
    return srp.sector_pool("半导体", **kwargs)


# ── 这不是 RotationStartScore ──────────────────────────────────

def test_available_weights_are_only_the_price_and_volume_ones():
    assert srp.AVAILABLE_WEIGHT_SHARE == pytest.approx(0.41, abs=1e-6)
    missing = set(srp.REPORT_WEIGHTS) - set(srp.AVAILABLE_COMPONENTS)
    assert missing == {"prosperity", "earnings", "flow", "valuation_odds", "regime_fit"}


def test_payload_states_how_much_weight_is_missing_and_stays_low_confidence():
    row = _pool()
    assert row["missing_weight_share"] == pytest.approx(0.59, abs=1e-6)
    assert row["confidence"] == "low"
    assert set(row["missing_components"]) == {
        "prosperity", "earnings", "flow", "valuation_odds", "regime_fit"
    }


def test_weights_are_report_values_renormalised_over_available_components():
    row = _pool()
    total = sum(srp.REPORT_WEIGHTS[name] for name in srp.AVAILABLE_COMPONENTS)
    for name in srp.AVAILABLE_COMPONENTS:
        assert row["applied_weights"][name] == pytest.approx(
            srp.REPORT_WEIGHTS[name] / total, abs=1e-4
        )


# ── 缺证据 ≠ 满足 ──────────────────────────────────────────────

def test_missing_rs_is_not_filled_with_a_neutral_fifty():
    row = _pool(rs_slope=None)
    assert row["components"]["relative_strength"] is None
    assert row["pool"] != "mainline"


def test_all_components_missing_is_unavailable_not_watch():
    row = _pool(rs_slope=None, breadth=None, crowding_score=None, fake_risk=None,
                crowding_state=None)
    assert row["status"] == srp.UNAVAILABLE
    assert row["pool"] == srp.UNAVAILABLE
    assert row["score"] is None


def test_mainline_needs_every_confirmation_present_not_merely_not_contradicted():
    assert _pool()["pool"] == "mainline"
    # 广度不可得：不是「没有反证所以算通过」
    weak = _pool(breadth=None)
    assert weak["pool"] == "watch"
    assert weak["confirmations"]["breadth"] is False


def test_unavailable_crowding_state_routes_to_avoid_not_watch():
    """拥挤状态不可得时不能放行 —— 与 sector_crowding 的状态机同一条纪律。"""
    assert _pool(crowding_state="unavailable")["pool"] == "avoid"


# ── 池判定 ──────────────────────────────────────────────────────

@pytest.mark.parametrize("state", ["NO_ADD", "EXIT_RISK", "COOLDOWN"])
def test_blocking_crowding_states_go_to_avoid_regardless_of_score(state):
    row = _pool(crowding_state=state, breadth=0.95, rs_slope=0.01)
    assert row["pool"] == "avoid"


def test_high_fake_risk_goes_to_avoid():
    assert _pool(fake_risk=60.0)["pool"] == "avoid"


def test_weak_breadth_drops_out_of_mainline_into_watch():
    assert _pool(breadth=0.40)["pool"] == "watch"


def test_negative_rs_slope_drops_out_of_mainline():
    assert _pool(rs_slope=-0.001)["pool"] == "watch"


def test_rs_component_is_symmetric_around_fifty():
    up = _pool(rs_slope=0.002)["components"]["relative_strength"]
    down = _pool(rs_slope=-0.002)["components"]["relative_strength"]
    flat = _pool(rs_slope=0.0)["components"]["relative_strength"]
    assert up == 100.0 and down == 0.0 and flat == 50.0


# ── B2 多标签 regime ────────────────────────────────────────────

def test_market_crowding_becomes_one_more_label_beside_the_existing_ones():
    result = srp.market_crowding_labels(0.8, extra_labels=["TrendUp", "SmallGrowth"])
    assert result["market_crowding"] == "EXTREME_CROWDING"
    assert result["labels"] == ["TrendUp", "SmallGrowth", "EXTREME_CROWDING"]


def test_market_crowding_ladder():
    assert srp.market_crowding_labels(0.3)["market_crowding"] == "NORMAL_CROWDING"
    assert srp.market_crowding_labels(0.65)["market_crowding"] == "ELEVATED_CROWDING"
    assert srp.market_crowding_labels(0.9)["market_crowding"] == "EXTREME_CROWDING"


def test_missing_market_crowding_is_unavailable_not_normal():
    result = srp.market_crowding_labels(None, extra_labels=["TrendUp"])
    assert result["market_crowding"] == srp.UNAVAILABLE
    assert "NORMAL_CROWDING" not in result["labels"]


# ── 组装 ────────────────────────────────────────────────────────

def test_build_partitions_sectors_and_marks_the_artifact():
    payload = srp.build_sector_rotation_pools(
        ["半导体", "银行", "传媒"],
        asof="2026-09-02",
        price_factors={
            "半导体": {"rs_slope_20d": 0.002, "breadth_ma20": 0.8},
            "银行": {"rs_slope_20d": -0.001, "breadth_ma20": 0.3},
            "传媒": {"rs_slope_20d": 0.002, "breadth_ma20": 0.9},
        },
        crowding={
            "半导体": {"score": 20.0, "state": "NORMAL"},
            "银行": {"score": 30.0, "state": "NORMAL"},
            "传媒": {"score": 95.0, "state": "EXIT_RISK"},
        },
        fake_breakout={
            "半导体": {"risk": 10.0},
            "银行": {"risk": 20.0},
            "传媒": {"risk": 80.0},
        },
        market_crowding_score=0.5,
    )
    assert payload["pools"]["mainline"] == ["半导体"]
    assert payload["pools"]["watch"] == ["银行"]
    assert payload["pools"]["avoid"] == ["传媒"]
    assert payload["live_effect"] == "none"
    assert payload["validated"] is False
    assert payload["confidence"] == "low"
    assert payload["weight_path"] == "expert_only_no_fitting"
    assert payload["missing_weight_share"] == pytest.approx(0.59, abs=1e-6)


def test_build_without_any_inputs_is_unavailable():
    payload = srp.build_sector_rotation_pools(["半导体"], asof="2026-09-02")
    assert payload["status"] == srp.UNAVAILABLE
    assert payload["pools"][srp.UNAVAILABLE] == ["半导体"]


# ── 配置 ────────────────────────────────────────────────────────

def test_yaml_section_and_module_defaults_do_not_drift():
    with open(config_path("scoring"), encoding="utf-8") as handle:
        section = (yaml.safe_load(handle) or {}).get(srp.CONFIG_SECTION)
    assert section == srp.DEFAULTS


def test_thresholds_are_actually_read_from_config():
    config = copy.deepcopy(srp.DEFAULTS)
    config["mainline_breadth_min"] = 0.30
    assert _pool(breadth=0.40)["pool"] == "watch"
    assert srp.sector_pool(
        "半导体", rs_slope=0.001, breadth=0.40, crowding_score=30.0,
        crowding_state="NORMAL", fake_risk=20.0, config=config,
    )["pool"] == "mainline"
