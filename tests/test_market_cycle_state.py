"""市场周期记忆层（P1，shadow only）测试。"""

import market_cycle_state as mcs

_CFG = {
    "enabled": True,
    "second_divergence_min": 2,
    "weaken_states": ["S6"],
    "divergence_states": ["S5"],
    "rise_reset_states": ["S3"],
}

_LABELS = {
    "S3": "扩散/主升", "S4": "高潮/拥挤", "S5": "分歧/轮动", "S6": "退潮/级联",
}


def _ms(state):
    return {"available": True, "dominant_state": state, "dominant_label": _LABELS.get(state)}


def _adv(prev, state, asof):
    return mcs.advance_cycle_memory(prev, _ms(state), asof=asof, config=_CFG)


def test_days_in_state_increments_only_across_new_days():
    d1 = _adv(None, "S3", "2026-06-01")
    assert d1["days_in_state"] == 1
    d2 = _adv(d1, "S3", "2026-06-02")
    assert d2["days_in_state"] == 2
    d3 = _adv(d2, "S4", "2026-06-03")
    assert d3["days_in_state"] == 1  # 状态切换，日龄归 1


def test_same_day_recompute_is_idempotent():
    d1 = _adv(None, "S5", "2026-06-01")
    again = _adv(d1, "S5", "2026-06-01")  # 同一交易日重复
    assert again == d1
    assert again["divergence_count"] == 1  # 不重复 +1


def test_first_divergence_then_second_divergence():
    m = _adv(None, "S3", "2026-06-01")       # 主升
    m = _adv(m, "S4", "2026-06-02")          # 高潮
    m = _adv(m, "S5", "2026-06-03")          # 首次分歧
    assert m["divergence_count"] == 1
    assert m["is_first_divergence"] is True
    m = _adv(m, "S4", "2026-06-04")          # 离开分歧（非主升，不清零）
    assert m["divergence_count"] == 1
    m = _adv(m, "S5", "2026-06-05")          # 二次分歧
    assert m["divergence_count"] == 2
    assert m["is_first_divergence"] is False


def test_rise_confirmation_resets_divergence_count():
    m = _adv(None, "S5", "2026-06-01")       # 分歧 count1
    assert m["divergence_count"] == 1
    m = _adv(m, "S3", "2026-06-02")          # 主升确认 → 清零
    assert m["divergence_count"] == 0
    m = _adv(m, "S5", "2026-06-03")          # 重新起算
    assert m["divergence_count"] == 1


def test_unavailable_market_state_fails_closed_and_carries_count():
    m = _adv(None, "S5", "2026-06-01")
    assert m["divergence_count"] == 1
    gap = mcs.advance_cycle_memory(m, {"available": False}, asof="2026-06-02", config=_CFG)
    assert gap["available"] is False
    assert gap["days_in_state"] is None
    assert gap["divergence_count"] == 1  # 数据缺口不清零


def test_divergence_not_incremented_across_data_gap():
    m = _adv(None, "S5", "2026-06-01")            # count1
    gap = mcs.advance_cycle_memory(m, {"available": False}, asof="2026-06-02", config=_CFG)
    resumed = _adv(gap, "S5", "2026-06-03")       # 缺口后仍 S5
    # prev 不可用，不臆断这是"新一次进入分歧"，保守结转不 +1
    assert resumed["divergence_count"] == 1


def test_shadow_constraints_block_on_weaken_and_second_divergence():
    weaken = {"available": True, "dominant_state": "S6", "state_label": "退潮/级联",
              "days_in_state": 1, "divergence_count": 0}
    c = mcs.shadow_constraints(weaken, config=_CFG)
    assert c["would_block_new_positions"] is True
    assert c["shadow_only"] is True

    second = {"available": True, "dominant_state": "S5", "state_label": "分歧/轮动",
              "days_in_state": 1, "divergence_count": 2, "is_first_divergence": False}
    c2 = mcs.shadow_constraints(second, config=_CFG)
    assert c2["would_block_new_positions"] is True
    assert c2["would_downgrade_second_board_w2s"] is True


def test_shadow_constraints_first_divergence_only_downgrades():
    first = {"available": True, "dominant_state": "S5", "state_label": "分歧/轮动",
             "days_in_state": 1, "divergence_count": 1, "is_first_divergence": True}
    c = mcs.shadow_constraints(first, config=_CFG)
    assert c["would_block_new_positions"] is False       # 首次分歧不拦，只降级
    assert c["would_downgrade_second_board_w2s"] is True


def test_shadow_constraints_unavailable_gives_no_direction():
    c = mcs.shadow_constraints({"available": False}, config=_CFG)
    assert c["would_block_new_positions"] is False
    assert c["would_downgrade_second_board_w2s"] is False
    assert any("不可用" in r for r in c["reasons"])


def test_shadow_layer_computes_regardless_of_enabled_flag():
    """enabled=False 时影子层照常计算"会拦什么"，只是 enabled 字段记 False——
    真正启用与否是 P2 的事，影子层永远 shadow_only。"""
    disabled_cfg = {**_CFG, "enabled": False}
    weaken = {"available": True, "dominant_state": "S6", "days_in_state": 1,
              "divergence_count": 0}
    c = mcs.shadow_constraints(weaken, config=disabled_cfg)
    assert c["enabled"] is False
    assert c["shadow_only"] is True
    assert c["would_block_new_positions"] is True
