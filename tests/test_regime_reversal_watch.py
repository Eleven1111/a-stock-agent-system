"""regime_reversal_watch 影子预警的因子、分级与 fail-closed 回归测试。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "scripts" / "regime_reversal_watch.py"
    spec = importlib.util.spec_from_file_location("rrw_test_module", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def module():
    return _load()


@pytest.fixture()
def cfg(module):
    return module.load_config()


def _factor(code, gap, limit_up=11.0, prev_close=10.0):
    return {"code": code, "name": f"股{code}", "auction_gap_pct": gap,
            "limit_up": limit_up, "prev_close": prev_close}


def test_f1_counts_gap_at_limit(module, cfg):
    factors = [_factor(f"6000{i:02d}", 10.0) for i in range(6)]
    factors.append(_factor("600100", 2.0))
    result = module._f1(factors, cfg)
    assert result["available"] is True
    assert result["triggered"] is True
    assert "6 家" in result["detail"]


def test_f1_below_threshold_not_triggered(module, cfg):
    factors = [_factor(f"6000{i:02d}", 10.0) for i in range(3)]
    result = module._f1(factors, cfg)
    assert result["triggered"] is False


def test_f2_coverage_fail_closed(module, cfg):
    ladder = {"600001": {"lianban": 2, "sector": "半导体"},
              "600002": {"lianban": 1, "sector": "半导体"}}
    factors = [_factor("600001", 3.0), _factor("600002", -1.0)]
    result = module._f2(factors, ladder, cfg)
    assert result["available"] is False
    assert result["triggered"] is False


def test_f2_red_ratio_triggers(module, cfg):
    ladder = {f"6000{i:02d}": {"lianban": 1, "sector": "半导体"} for i in range(4)}
    gaps = [3.0, 1.0, 2.0, -2.0]  # 3/4 红盘 = 75% ≥ 60%
    factors = [_factor(code, gap) for code, gap in zip(ladder, gaps)]
    result = module._f2(factors, ladder, cfg)
    assert result["available"] is True
    assert result["triggered"] is True
    assert "3/4" in result["detail"]


def test_f3_cluster_with_red_confirmation(module, cfg):
    sector_limitups = {"半导体": 3, "食品": 1}
    ladder = {"600001": {"lianban": 2, "sector": "半导体"},
              "600002": {"lianban": 1, "sector": "半导体"}}
    factors = [_factor("600001", 5.0), _factor("600002", -1.0)]
    result = module._f3(sector_limitups, ladder, {}, factors, cfg)
    assert result["available"] is True
    assert result["triggered"] is True
    assert "方向未知" in result["detail"]


def test_f3_below_cluster_not_triggered(module, cfg):
    sector_limitups = {"半导体": 2}
    result = module._f3(sector_limitups, {}, {}, [], cfg)
    assert result["triggered"] is False


def test_f4_breadth_ratio_and_min_coverage(module, cfg):
    red = [_factor(f"6001{i:02d}", 1.0) for i in range(84)]
    green = [_factor(f"6002{i:02d}", -1.0) for i in range(36)]  # 84/120 = 70%
    result = module._f4(red + green, cfg)
    assert result["triggered"] is True

    few = [_factor(f"6003{i:02d}", 1.0) for i in range(50)]
    result = module._f4(few, cfg)
    assert result["available"] is False


def test_grading_levels(module, cfg):
    ladder = {f"6000{i:02d}": {"lianban": 1, "sector": "半导体"} for i in range(4)}
    red_factors = [_factor(code, 3.0) for code in ladder]
    extra = [_factor(f"6009{i:02d}", 10.0) for i in range(6)]  # F1 触发
    factors = red_factors + extra
    sector_limitups = {"半导体": 4}

    strong = module.evaluate(factors, ladder, sector_limitups, {}, None,
                             asof="2026-09-10", stage="auction", cfg=cfg)
    assert strong["level"] == "strong"
    assert strong["triggered_count"] == 3  # F4 样本 10<100 不可用，F1/F2/F3 触发

    watch = module.evaluate(
        [_factor(f"6010{i:02d}", 10.0) for i in range(6)],  # 只有 F1
        {}, {}, {}, None, asof="2026-09-10", stage="auction", cfg=cfg)
    # F1 触发 + F4 样本 6 <100 不可用 + F2/F3 无梯队 → 只 1 项触发 → silent
    assert watch["level"] == "silent"

    two = module.evaluate(
        extra + [_factor(f"6011{i:02d}", 1.0) for i in range(120)],
        {}, {}, {}, None, asof="2026-09-10", stage="auction", cfg=cfg)
    assert two["level"] == "watch"  # F1 + F4


def test_shortlist_missing_maps_to_insufficient(module, cfg):
    result = module.evaluate(None, {}, {}, {}, None,
                             asof="2026-09-10", stage="auction", cfg=cfg)
    assert result["level"] == "silent"
    assert all(item["available"] is False for item in result["factors"])
    assert result["research_only"] is True


def test_render_silent_is_empty_and_strong_has_disclaimer(module, cfg):
    silent = {"level": "silent", "triggered_count": 1, "asof": "2026-09-10",
              "stage": "auction", "factors": [], "confirm_note": None}
    assert module.render(silent, cfg) == ""

    strong = {"level": "strong", "triggered_count": 3, "asof": "2026-09-10",
              "stage": "auction", "confirm_note": None,
              "factors": [{"id": "F1", "name": "一字板", "available": True,
                           "triggered": True, "detail": "6 家 ≥5"}]}
    text = module.render(strong, cfg)
    assert "🔴" in text
    assert "research_only" in text
    assert "不构成操作指令" in text
    assert "校准期至" in text


def test_shadow_log_appended(module, cfg, tmp_path, monkeypatch):
    log_path = tmp_path / "regime_reversal_watch.jsonl"
    monkeypatch.setattr(module, "data_file", lambda *a: str(log_path))
    factors = [_factor(f"6000{i:02d}", 10.0) for i in range(6)]

    result = module.evaluate(factors, {}, {}, {}, None,
                             asof="2026-09-10", stage="auction", cfg=cfg)
    module._append_shadow(result)

    lines = log_path.read_text().strip().splitlines()
    payload = json.loads(lines[-1])
    assert payload["shadow"] is True
    assert payload["asof"] == "2026-09-10"
    assert payload["level"] == result["level"]


def test_shortlist_asof_mismatch_rejected(module, cfg, tmp_path, monkeypatch):
    stale = tmp_path / "shortlist.json"
    stale.write_text(json.dumps({"asof": "2026-09-01", "status": "ready",
                                 "factors": [_factor("600001", 10.0)]}))
    monkeypatch.setattr(module, "data_file", lambda *a: str(stale))

    assert module._shortlist("2026-09-10") is None
