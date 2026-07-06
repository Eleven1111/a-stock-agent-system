"""弱市交付门禁语义回归 — qualified 与 hot_money_qualified 拆分。

复现 07-06 P0：梯队缺档导致 leader.qualified=False 时，通用质量门禁不得误杀
trend lane / balanced fill；只有显式顶层 qualified=False 才 reject。
"""

import weak_market_delivery as wmd


def _normal_timing():
    """正常市场（非弱市）择时，weak_regime=False。"""
    return {
        "status": "ready",
        "breadth": {
            "advancers": 3200,
            "decliners": 1200,
            "flat": 100,
            "limitup_count": 90,
            "limitdown_count": 8,
        },
        "previous_ladder_premium": 3.5,
        "temperature": {"tier": "warm", "context_fresh": True},
    }


def _candidate_with_leader_qualified_false():
    """07-06 结构：无顶层 qualified、hot_money_qualified，仅 leader.qualified=False。"""
    return {
        "code": "sz300001",
        "name": "趋势票",
        "trend_score": 90,
        "daban_score": 10,
        "selection_context": {
            "market_timing": _normal_timing(),
            "sector": {"name": "半导体", "rank": 2},
            "leader": {"rank": 30, "qualified": False},
        },
    }


def test_leader_qualified_false_does_not_reject_trend_lane():
    item = _candidate_with_leader_qualified_false()

    result = wmd.assess_delivery_quality(item, lane="trend", stage="D0_close")

    assert result["status"] == "deliverable_watch"
    assert all(
        "候选质量门槛 qualified=False" not in reason
        for reason in result["reasons"]
    )


def test_top_level_qualified_false_still_rejects():
    item = _candidate_with_leader_qualified_false()
    item["qualified"] = False

    result = wmd.assess_delivery_quality(item, lane="trend", stage="D0_close")

    assert result["status"] == "reject"
    assert "候选质量门槛 qualified=False" in result["reasons"]


def test_extract_hot_money_qualified_from_top_level():
    item = {"code": "sh600001", "hot_money_qualified": True}

    fields = wmd._extract_candidate_fields(item)

    assert fields["hot_money_qualified"] is True


def test_extract_hot_money_qualified_from_leader_context_explicit_key():
    item = {
        "code": "sh600001",
        "selection_context": {"leader": {"hot_money_qualified": False}},
    }

    fields = wmd._extract_candidate_fields(item)

    assert fields["hot_money_qualified"] is False


def test_extract_hot_money_qualified_falls_back_to_legacy_leader_qualified():
    item = {
        "code": "sh600001",
        "selection_context": {"leader": {"qualified": True}},
    }

    fields = wmd._extract_candidate_fields(item)

    assert fields["hot_money_qualified"] is True


def test_extract_hot_money_qualified_unknown_is_none():
    item = {"code": "sh600001", "selection_context": {"leader": {"rank": 5}}}

    fields = wmd._extract_candidate_fields(item)

    assert fields["hot_money_qualified"] is None


def _weak_stale_timing():
    """弱市 + 数据不新鲜（context_fresh=False）。"""
    return {
        "status": "insufficient_data",
        "breadth": {
            "advancers": 700,
            "decliners": 4400,
            "flat": 60,
            "limitup_count": 40,
            "limitdown_count": 55,
        },
        "previous_ladder_premium": 0.3,
        "temperature": {"tier": "neutral", "context_fresh": False},
    }


def test_weak_market_daban_leader_ok_uses_tristate_hot_money_qualified():
    """弱市 daban lane：hot_money_qualified=True 的窄主题前二龙头才可交付。"""
    ready_leader = {
        "code": "sh600519",
        "name": "主线龙头",
        "hot_money_qualified": True,
        "daban_score": 95,
        "selection_context": {
            "market_timing": _weak_stale_timing(),
            "sector": {
                "name": "白酒",
                "rank": 1,
                "evidence_count": 3,
            },
            "leader": {"rank": 1},
        },
        "sector_evidence_count": 3,
        "capacity_core": True,
    }

    result = wmd.assess_delivery_quality(ready_leader, lane="daban", stage="09:25")

    assert result["status"] != "reject"
    # 弱市数据 stale 会降级为 research_only，这是既有行为；关键是不因 tri-state 改写。
    assert result["status"] in {"deliverable_watch", "research_only"}


def test_weak_market_daban_without_hot_money_qualified_is_research_only():
    """弱市 daban lane：hot_money_qualified 缺失/False → 不是前二龙头 → research_only。"""
    weak_leader = {
        "code": "sh600519",
        "name": "非游资龙头",
        "hot_money_qualified": False,
        "daban_score": 95,
        "selection_context": {
            "market_timing": _weak_stale_timing(),
            "sector": {"name": "白酒", "rank": 1, "evidence_count": 3},
            "leader": {"rank": 1},
        },
        "sector_evidence_count": 3,
        "capacity_core": True,
    }

    result = wmd.assess_delivery_quality(weak_leader, lane="daban", stage="09:25")

    assert result["status"] == "research_only"
    assert any("弱市打板仅交付窄主题前二龙头" in r for r in result["reasons"])
