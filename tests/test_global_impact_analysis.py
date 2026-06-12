import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "global-market-monitor" / "scripts" / "monitor.py"
SPEC = importlib.util.spec_from_file_location("global_monitor_impact", SCRIPT)
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


def test_global_impact_includes_sector_and_stock_observation_map():
    result = monitor.assess_impact({
        "vix": {"price": 18},
        "us_indices": {
            "^GSPC": {"change_pct": 0.2},
            "^IXIC": {"change_pct": 2.2},
            "^DJI": {"change_pct": 0.1},
        },
        "treasuries": {},
        "fx": {},
        "commodities": {},
        "china_adrs": {},
        "key_stocks": {},
        "us_sectors": {},
        "global_indices": {},
        "disasters": [],
        "news": [],
    })

    analysis = result["a_share_analysis"]
    assert analysis["sector_views"]
    assert any(view["sector"] == "AI算力" for view in analysis["sector_views"])
    assert any(stock["code"] for stock in analysis["stock_watchlist"])
    assert analysis["stock_watchlist"][0]["advice"] == "watch_only_pending_stock_qc"
