import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module():
    spec = importlib.util.spec_from_file_location("market_deep_review", ROOT / "scripts/market_deep_review.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_has_seven_sections_and_does_not_promote_stock_totals_to_index(tmp_path, monkeypatch):
    m = load_module()
    state = tmp_path
    cache = state / "skills/stock-triage/data"
    cache.mkdir(parents=True)
    (cache / "universe_quotes_cache.json").write_text(
        json.dumps(
            {"updated_at": "2026-09-02T15:10:00+08:00", "quotes": {"000001": {"change_pct": 1.0, "amount": 100000000}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(state))
    report = m._build("2026-09-02")
    assert list(report["sections"]) == [
        "index_performance",
        "capital_flow",
        "stock_breadth",
        "market_structure",
        "sentiment",
        "institutional_views",
        "conclusion",
    ]
    assert report["sections"]["index_performance"]["fields"]["close"]["value"] == "unavailable"
    assert report["sections"]["stock_breadth"]["fields"]["advance"]["value"] == 1


def test_missing_institutional_view_is_explicit_and_score_is_fail_closed(tmp_path, monkeypatch):
    m = load_module()
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    report = m._build("2026-09-02")
    assert "暂无可验证机构观点" in report["sections"]["institutional_views"]["note"]
    score = report["sections"]["sentiment"]["fields"]["composite_score"]
    assert score["value"] == "unavailable"
    assert score["status"] == "unavailable"


def test_markdown_contains_all_required_headings():
    m = load_module()
    text = m.render(m._build("2099-01-01"))
    for heading in (
        "一、指数表现",
        "二、资金流向",
        "三、个股涨跌",
        "四、盘面结构",
        "五、情绪判断",
        "六、机构观点摘要",
        "七、结论",
    ):
        assert heading in text
