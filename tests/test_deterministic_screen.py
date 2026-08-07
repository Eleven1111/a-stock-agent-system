"""确定性红旗筛查（serenity P1 地板）—— 规则边界用合成用例，不冒充实测财务数据。

本文件所有 facts 都是为了压规则边界手工构造的**合成样本**，
不代表任何真实公司的财务状况，也不能用来论证"这套规则在 A 股上有效"。
真实数据验证属于 P0 取数管道落地后的事。
"""

import deterministic_screen as screen


def _facts(**periods):
    """构造 fundamental_facts_v1 形状的最小合成样本。"""
    ordered = sorted(periods.items(), reverse=True)
    return {
        "schema": "fundamental_facts_v1",
        "code": "600001",
        "name": "合成样本",
        "asof": "2026-06-30",
        "source": {"provider": "synthetic_fixture"},
        "units": {"scale": "yuan"},
        "periods": [{"period": key, **value} for key, value in ordered],
    }


BASE = {"total_assets": 1000.0, "total_liabilities": 400.0, "revenue": 800.0,
        "net_profit": 60.0, "equity": 600.0}


def test_missing_inputs_never_score_as_healthy():
    """缺数据必须不打分，绝不能把缺失当合格 —— 这是整个模块最重要的一条。"""
    incomplete = _facts(**{"2026Q2": {"total_assets": 1000.0}})

    result = screen.screen(incomplete)

    assert result["dimensions"]["financial_quality"] is None
    assert result["complete"] is False
    assert set(result["evidence"]["financial_quality"]["missing"]) == {
        "total_liabilities", "revenue", "net_profit",
    }


def test_clean_books_cap_at_three_never_four_or_five():
    """没筛出红旗 ≠ 质量高。确定性筛查封顶 3 分，无权给高分换仓位权重。"""
    result = screen.screen(_facts(**{"2026Q2": BASE, "2026Q1": BASE}))

    assert result["dimensions"] == {"financial_quality": 3, "risk_control": 3}
    assert result["hard_risk_codes"] == []
    # 不得产出进入 four_dim 排序权重的字段
    assert "deep_score" not in result and "rating" not in result


def test_severe_leverage_triggers_hard_risk_score_one():
    heavy = {**BASE, "total_liabilities": 900.0}
    result = screen.screen(_facts(**{"2026Q2": heavy, "2026Q1": heavy}))

    assert result["dimensions"]["financial_quality"] == 1
    assert "debt_ratio_severe" in result["hard_risk_codes"]


def test_consecutive_net_loss_is_severe():
    loss = {**BASE, "net_profit": -30.0}
    result = screen.screen(_facts(**{"2026Q2": loss, "2026Q1": loss}))

    assert result["dimensions"]["financial_quality"] == 1
    assert "consecutive_net_loss" in result["hard_risk_codes"]


def test_profitable_but_cash_burning_is_severe_not_merely_warn():
    """净利润为正、经营现金流连续为负 → 盈利质量存疑，比单纯亏损更该警惕。"""
    burning = {**BASE, "operating_cash_flow": -50.0}
    result = screen.screen(_facts(**{"2026Q2": burning, "2026Q1": burning}))

    assert "negative_operating_cash_flow" in result["hard_risk_codes"]
    assert result["dimensions"]["financial_quality"] == 1


def test_single_warn_flag_lands_on_two_not_one():
    warn = {**BASE, "total_liabilities": 750.0}
    result = screen.screen(_facts(**{"2026Q2": warn, "2026Q1": warn}))

    assert result["dimensions"]["financial_quality"] == 2
    assert result["hard_risk_codes"] == []
    assert result["evidence"]["financial_quality"]["flags"][0]["code"] == "debt_ratio_warn"


def test_st_and_pledge_drive_risk_control_hard_risk():
    result = screen.screen(
        _facts(**{"2026Q2": BASE, "2026Q1": BASE}),
        listing_flags={"st": True, "pledge_ratio": 0.62},
    )

    assert result["dimensions"]["risk_control"] == 1
    assert "st_flagged" in result["hard_risk_codes"]
    assert "pledge_severe" in result["hard_risk_codes"]


def test_absent_listing_flags_do_not_manufacture_a_clean_bill():
    """没有质押/问询数据时不能反过来当作「无风险」—— 这里只能靠财报维度判定，
    未覆盖的风险项不出现在 flags 里，也不得抬高分数。"""
    result = screen.screen(_facts(**{"2026Q2": BASE, "2026Q1": BASE}))

    assert result["dimensions"]["risk_control"] == 3
    assert result["evidence"]["risk_control"]["flags"] == []


def test_screen_carries_provenance_for_three_state_wiring():
    result = screen.screen(_facts(**{"2026Q2": BASE, "2026Q1": BASE}))

    assert result["source"] == "deterministic_screen"
    assert result["facts_provider"] == "synthetic_fixture"
    assert result["asof"] == "2026-06-30"
