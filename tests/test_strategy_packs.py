"""Declarative NL strategy packs are an interpretation/research-hypothesis layer.

Hard contract (AGENTS.md 红线): a strategy pack MUST NOT influence live ranking,
scoring, or signals before passing the research gate. These tests pin the pack
schema, fail-closed validation, regime filtering, and the "hints never mutate
ranking" regression.
"""

from __future__ import annotations

import copy

import pytest

import strategy_packs
from strategy_packs import PackError


# --------------------------------------------------------------------------- #
# Built-in packs load and validate
# --------------------------------------------------------------------------- #
def test_builtin_packs_load_and_validate():
    packs = strategy_packs.load_packs()
    assert "dragon_head" in packs
    assert "emotion_cycle" in packs
    for name, pack in packs.items():
        assert pack["name"] == name
        assert pack["category"] in {"trend", "pattern", "reversal", "framework"}
        assert isinstance(pack["market_regimes"], list) and pack["market_regimes"]
        assert isinstance(pack["evidence_requirements"], list)
        assert isinstance(pack["interpretation"], str) and pack["interpretation"].strip()
        assert isinstance(pack["score_hints"], list)


def test_builtin_packs_do_not_hardcode_stocks_or_sectors():
    """红线：策略包只能是通用模板，不得内置具体股票/板块。"""
    packs = strategy_packs.load_packs()
    import re

    for pack in packs.values():
        blob = str(pack)
        # No 6-digit A-share codes anywhere in the declarative pack.
        assert not re.search(r"\b\d{6}\b", blob), f"{pack['name']} leaks a stock code"


# --------------------------------------------------------------------------- #
# Fail-closed schema validation
# --------------------------------------------------------------------------- #
def _valid_pack() -> dict:
    return {
        "name": "unit_pack",
        "display_name": "Unit Pack",
        "description": "test",
        "category": "trend",
        "market_regimes": ["发酵"],
        "evidence_requirements": ["turnover"],
        "interpretation": "some words",
        "score_hints": [
            {"when": "turnover_ge_5", "delta": 3, "reason": "high turnover"},
        ],
    }


def test_validate_accepts_valid_pack():
    pack = strategy_packs.validate_pack(_valid_pack())
    assert pack["name"] == "unit_pack"


@pytest.mark.parametrize("missing", [
    "name", "display_name", "description", "category",
    "market_regimes", "evidence_requirements", "interpretation", "score_hints",
])
def test_validate_rejects_missing_field(missing):
    pack = _valid_pack()
    del pack[missing]
    with pytest.raises(PackError, match=missing):
        strategy_packs.validate_pack(pack)


def test_validate_rejects_unknown_category():
    pack = _valid_pack()
    pack["category"] = "moon_phase"
    with pytest.raises(PackError, match="category"):
        strategy_packs.validate_pack(pack)


def test_validate_rejects_bad_score_hint_shape():
    pack = _valid_pack()
    pack["score_hints"] = [{"reason": "no when/delta"}]
    with pytest.raises(PackError):
        strategy_packs.validate_pack(pack)


def test_load_packs_rejects_invalid_file_loudly(tmp_path):
    """非法包必须拒载并明确报错，不静默跳过。"""
    good = tmp_path / "good.yaml"
    good.write_text(
        "name: good\ndisplay_name: G\ndescription: d\ncategory: trend\n"
        "market_regimes: [发酵]\nevidence_requirements: [turnover]\n"
        "interpretation: hi\nscore_hints: []\n",
        encoding="utf-8",
    )
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: bad\ncategory: nonsense\n", encoding="utf-8")
    with pytest.raises(PackError, match="bad"):
        strategy_packs.load_packs(directory=tmp_path)


def test_load_packs_rejects_duplicate_name(tmp_path):
    for fname in ("a.yaml", "b.yaml"):
        (tmp_path / fname).write_text(
            "name: dup\ndisplay_name: D\ndescription: d\ncategory: trend\n"
            "market_regimes: [发酵]\nevidence_requirements: [turnover]\n"
            "interpretation: hi\nscore_hints: []\n",
            encoding="utf-8",
        )
    with pytest.raises(PackError, match="dup"):
        strategy_packs.load_packs(directory=tmp_path)


# --------------------------------------------------------------------------- #
# Regime filtering
# --------------------------------------------------------------------------- #
def test_packs_for_regime_filters_by_market_regime():
    packs = strategy_packs.load_packs()
    hot = strategy_packs.packs_for_regime("加速", packs=packs)
    names = {p["name"] for p in hot}
    # dragon_head targets hot/accelerating regimes.
    assert "dragon_head" in names


def test_packs_for_regime_none_returns_all():
    packs = strategy_packs.load_packs()
    assert len(strategy_packs.packs_for_regime(None, packs=packs)) == len(packs)


def test_packs_for_regime_supports_wildcard():
    packs = {
        "framework_pack": strategy_packs.validate_pack({
            **_valid_pack(), "name": "framework_pack",
            "category": "framework", "market_regimes": ["*"],
        }),
    }
    got = strategy_packs.packs_for_regime("冰点", packs=packs)
    assert {p["name"] for p in got} == {"framework_pack"}


# --------------------------------------------------------------------------- #
# Hint generation (pure, interpretation only)
# --------------------------------------------------------------------------- #
def test_evaluate_hints_reports_hit_and_miss_detail():
    candidate = {
        "code": "000001",
        "turnover": 6.5,
        "volume_ratio_5d": 1.8,
        "auction_sector_delta": 2.5,
        "sector_source": "ladder",
    }
    hints = strategy_packs.evaluate_pack_hints(candidate)
    dragon = next(h for h in hints if h["pack"] == "dragon_head")
    assert "conditions" in dragon
    hit_ids = {c["id"] for c in dragon["conditions"] if c["hit"]}
    miss = {c["id"] for c in dragon["conditions"] if not c["hit"]}
    # A high-turnover, high-volume-ratio candidate hits some dragon conditions.
    assert hit_ids
    # Missing/undecidable conditions must carry an explicit reason.
    for cond in dragon["conditions"]:
        if not cond["hit"]:
            assert cond.get("reason")
    del miss  # documented for clarity


def test_evaluate_hints_marks_unknown_evidence_not_a_hit():
    """缺证据字段 → 不算命中，且给出未命中原因，绝不静默算通过。"""
    hints = strategy_packs.evaluate_pack_hints({"code": "000002"})
    for pack_hint in hints:
        for cond in pack_hint["conditions"]:
            assert cond["hit"] is False
            assert cond.get("reason")


def test_evaluate_hints_respects_regime_filter():
    hints_hot = strategy_packs.evaluate_pack_hints(
        {"code": "000003", "turnover": 6.0}, regime="加速",
    )
    assert any(h["pack"] == "dragon_head" for h in hints_hot)


# --------------------------------------------------------------------------- #
# The load-bearing red line: hints NEVER change ranking / scoring / signals.
# --------------------------------------------------------------------------- #
def test_hints_are_pure_and_do_not_mutate_candidate():
    candidate = {"code": "000004", "turnover": 6.0, "daban_score": 42.0, "trend_score": 30.0}
    snapshot = copy.deepcopy(candidate)
    strategy_packs.evaluate_pack_hints(candidate)
    assert candidate == snapshot, "evaluate_pack_hints must not mutate the candidate"


def test_hints_do_not_change_candidate_pipeline_ranking():
    """Regression: attaching pack hints must not shift daban/trend scores or ranks."""
    import candidate_pipeline as cp

    eligible = [
        {"code": "000001", "name": "A", "price": 10, "volume": 1e6, "amount": 3e8,
         "change_pct": 9.0, "turnover": 8.0},
        {"code": "600000", "name": "B", "price": 20, "volume": 2e6, "amount": 5e8,
         "change_pct": 4.0, "turnover": 3.0},
    ]
    kline = {c["code"]: [
        {"open": 10, "high": 11, "low": 9, "close": 10 + i * 0.1, "volume": 1e6}
        for i in range(30)
    ] for c in eligible}

    ranked_before = cp.rank_candidates(copy.deepcopy(eligible), kline)
    scores_before = {r["code"]: (r["daban_score"], r["trend_score"], r["daban_rank"])
                     for r in ranked_before}

    # Evaluating hints for each candidate must be side-effect free w.r.t. ranking.
    for cand in ranked_before:
        strategy_packs.evaluate_pack_hints(cand)

    ranked_after = cp.rank_candidates(copy.deepcopy(eligible), kline)
    scores_after = {r["code"]: (r["daban_score"], r["trend_score"], r["daban_rank"])
                    for r in ranked_after}
    assert scores_before == scores_after


# --------------------------------------------------------------------------- #
# Registry: packs are un-gated research hypotheses
# --------------------------------------------------------------------------- #
def test_registry_records_are_all_ungated():
    records = strategy_packs.registry_records()
    assert set(records) >= {"dragon_head", "emotion_cycle"}
    for rec in records.values():
        assert rec["allowed_in_live_agent"] is False
        assert rec["gate_decision"] == "not_gated"
        assert "research_gate" in rec["upgrade_path"]


def test_registry_records_match_strategy_registry_semantics():
    """A pack id is not admissible to live weighting (parallels un-registered id)."""
    import strategy_registry

    records = strategy_packs.registry_records()
    for name in records:
        # No pack is registered in the live gate registry by default.
        assert strategy_registry.is_allowed_in_live(name) is False


def test_strategy_registry_exposes_packs_as_ungated_hypotheses():
    import strategy_registry

    view = strategy_registry.strategy_pack_hypotheses()
    assert set(view) >= {"dragon_head", "emotion_cycle"}
    for rec in view.values():
        assert rec["allowed_in_live_agent"] is False
        assert rec["gate_decision"] == "not_gated"
