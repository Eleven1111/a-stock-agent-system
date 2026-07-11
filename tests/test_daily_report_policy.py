"""Daily report templates must not fabricate portfolio sizing advice."""

from pathlib import Path


REPORT_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "a-stock-daily-report"
    / "scripts"
    / "a-stock-report.js"
)


def test_daily_report_defers_position_sizing_to_portfolio_policy():
    source = REPORT_SCRIPT.read_text(encoding="utf-8")

    assert "建议维持6-7成仓位" not in source
    assert "仓位由组合风险政策决定" in source


def test_daily_report_does_not_describe_price_rank_as_fund_flow():
    source = REPORT_SCRIPT.read_text(encoding="utf-8")

    assert "reason: '资金关注'" not in source
    assert "reason: '资金持续流入'" not in source
    assert "reason: '资金流出'" not in source
    assert "当日涨幅靠前（不代表资金流入）" in source
    assert "当日跌幅靠前（不代表资金流出）" in source
