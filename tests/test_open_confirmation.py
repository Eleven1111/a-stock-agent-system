"""09:35 open confirmation pure decision tests."""

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "daban-stock-picker" / "scripts" / "open_confirmation.py"
SPEC = importlib.util.spec_from_file_location("open_confirmation", SCRIPT)
oc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(oc)


def test_open_confirmation_marks_yiziban_not_buyable():
    factor = {
        "code": "sz002156",
        "name": "通富微电",
        "auction_gap_pct": 10.0,
        "board_status": "yizi_seal",
        "is_yiziban": True,
    }
    quote = {"price": 11.0, "prev_close": 10.0, "open": 11.0, "low": 11.0, "high": 11.0, "volume": 1000}

    result = oc.evaluate_open_confirmation(factor, quote)

    assert result["action"] == "not_buyable"
    assert result["tradeability"]["tradeable"] is False


def test_open_confirmation_marks_mid_gain_as_trend_watch():
    factor = {
        "code": "sz002156",
        "name": "通富微电",
        "auction_gap_pct": 4.0,
        "board_status": "high_open",
        "is_yiziban": False,
    }
    quote = {
        "price": 10.5,
        "prev_close": 10.0,
        "open": 10.4,
        "low": 10.3,
        "high": 10.6,
        "volume": 1000,
        "change_pct": 5.0,
    }

    result = oc.evaluate_open_confirmation(factor, quote)

    assert result["action"] == "trend_watch"
    assert "3%-10%" in result["reasons"][0]
