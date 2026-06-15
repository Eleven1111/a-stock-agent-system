import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "stock-triage" / "scripts" / "stock_intelligence_refresh.py"
SPEC = importlib.util.spec_from_file_location("stock_intelligence_refresh", SCRIPT)
refresh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(refresh)


def test_targets_include_all_positions_and_only_top_five_candidates():
    targets = refresh.build_targets(
        portfolio={
            "positions": [
                {"code": "600011", "name": "华能国际"},
                {"code": "002156", "name": "通富微电"},
            ]
        },
        candidate_pool={
            "candidates": [
                {"code": f"60{i:04d}", "name": f"候选{i}"}
                for i in range(8)
            ]
        },
        candidate_limit=5,
    )

    assert [item["code"] for item in targets[:2]] == ["600011", "002156"]
    assert len([item for item in targets if item["source"] == "candidate_pool"]) == 5


def test_target_dedup_prefers_portfolio_priority():
    targets = refresh.build_targets(
        portfolio={"positions": [{"code": "600011", "name": "持仓名称"}]},
        candidate_pool={
            "candidates": [
                {"code": "600011", "name": "候选名称"},
                {"code": "002156", "name": "通富微电"},
            ]
        },
        candidate_limit=5,
    )

    assert targets[0] == {
        "code": "600011",
        "name": "持仓名称",
        "source": "portfolio",
    }


def test_manual_cancel_tombstone_excludes_all_target_sources():
    targets = refresh.build_targets(
        portfolio={"positions": [{"code": "600011", "name": "持仓名称"}]},
        candidate_pool={
            "candidates": [{"code": "600011", "name": "候选名称"}]
        },
        registry=[
            {
                "kind": "stock",
                "key": "600011",
                "status": "cancelled",
                "manual_cancelled": True,
            }
        ],
        candidate_limit=5,
    )

    assert targets == []
