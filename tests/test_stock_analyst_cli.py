import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "stock-analyst" / "analyst.py"
SPEC = importlib.util.spec_from_file_location("stock_analyst_cli", SCRIPT)
analyst = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analyst)


def test_parse_stock_pairs_accepts_explicit_code_name_tokens():
    assert analyst.parse_stock_pairs(
        ["600001:测试一", "000002:测试二"]
    ) == [
        ("600001", "测试一"),
        ("000002", "测试二"),
    ]


def test_screen_has_no_implicit_default_theme(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(analyst, "screen_stocks", lambda pairs: called.append(pairs))

    result = analyst.cmd_screen([])

    assert result is False
    assert called == []
    assert "必须显式提供" in capsys.readouterr().out
