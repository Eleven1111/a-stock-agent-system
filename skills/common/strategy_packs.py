"""Declarative NL strategy packs — an interpretation / research-hypothesis layer.

A strategy pack is a declarative document (``config/strategy_packs/*.yaml``) that
captures, in natural language plus a small set of evaluable conditions, *how a
human strategist would read a candidate*. It exists so the agent can explain a
recommendation ("this looks like a sector dragon-head because …") and so
research hypotheses are written down in one auditable place.

Hard boundary (AGENTS.md 红线)
------------------------------
A strategy pack MUST NOT influence live ranking, scoring, or signals. Nothing in
this module writes back to ``candidate_pipeline`` scores, the signal ledger, or
the portfolio policy. ``score_hints`` are *advisory* deltas surfaced only inside
the evidence pack's ``strategy_pack_hints`` section for explanation; they are
never summed into ``daban_score`` / ``trend_score`` / ranks.

Upgrade path
------------
To let a pack influence live ranking, it must earn admission the same way every
other strategy does: run ``skills/chanlun-backtest/scripts/research_gate.py`` on
locked rules, pass the out-of-sample wall, and register the verified result via
``strategy_registry.register_gate_result``. Until then ``registry_records()``
reports every pack as ``allowed_in_live_agent=False`` / ``gate_decision =
"not_gated"`` — identical semantics to an unregistered strategy id.

The pack YAML never hardcodes stocks, sectors, or themes; conditions reference
only the generic evidence fields produced upstream (candidate pipeline, auction
shortlist, temperature).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

_HERE = Path(__file__).resolve()
CONFIG_DIR = _HERE.parents[2] / "config" / "strategy_packs"

VALID_CATEGORIES = {"trend", "pattern", "reversal", "framework"}
REQUIRED_FIELDS = (
    "name",
    "display_name",
    "description",
    "category",
    "market_regimes",
    "evidence_requirements",
    "interpretation",
    "score_hints",
)

REGIME_WILDCARD = "*"


class PackError(RuntimeError):
    """Raised when a strategy pack is malformed. Never swallowed / skipped."""


# --------------------------------------------------------------------------- #
# Condition predicates — pure, evidence-only, side-effect free.
# --------------------------------------------------------------------------- #
# Each predicate maps a candidate evidence mapping to a tri-state:
#   (True,  None)   -> condition satisfied
#   (False, reason) -> condition not satisfied (or evidence missing) + why
# A missing evidence field is *never* treated as a pass.
_TURNOVER_LOW = 3.0
_TURNOVER_HIGH = 8.0
_VOLUME_RATIO_HOT = 1.5
_MA_COIL_THRESHOLD = 0.02


def _num(candidate: Mapping[str, Any], key: str) -> float | None:
    value = candidate.get(key)
    if isinstance(value, Mapping):
        value = value.get("value")
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _feature_mapping(candidate: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    value = candidate.get(key)
    return value if isinstance(value, Mapping) else None


def _c_turnover_ge_5(c: Mapping[str, Any]) -> tuple[bool, str | None]:
    v = _num(c, "turnover")
    if v is None:
        return False, "缺少换手率(turnover)证据"
    return (v >= 5.0, None if v >= 5.0 else f"换手率{v:g}%未达5%")


def _c_turnover_low_band(c: Mapping[str, Any]) -> tuple[bool, str | None]:
    v = _num(c, "turnover")
    if v is None:
        return False, "缺少换手率(turnover)证据"
    return (v <= _TURNOVER_LOW, None if v <= _TURNOVER_LOW else f"换手率{v:g}%不在低分档(≤{_TURNOVER_LOW:g}%)")


def _c_turnover_high_band(c: Mapping[str, Any]) -> tuple[bool, str | None]:
    v = _num(c, "turnover")
    if v is None:
        return False, "缺少换手率(turnover)证据"
    return (v >= _TURNOVER_HIGH, None if v >= _TURNOVER_HIGH else f"换手率{v:g}%不在高分档(≥{_TURNOVER_HIGH:g}%)")


def _c_volume_ratio_ge_1_5(c: Mapping[str, Any]) -> tuple[bool, str | None]:
    v = _num(c, "volume_ratio_5d")
    if v is None:
        return False, "缺少量比(volume_ratio_5d)证据"
    return (v >= _VOLUME_RATIO_HOT, None if v >= _VOLUME_RATIO_HOT else f"量比{v:g}未达{_VOLUME_RATIO_HOT:g}")


def _c_sector_lag_lte_2(c: Mapping[str, Any]) -> tuple[bool, str | None]:
    v = _num(c, "auction_sector_delta")
    if v is None:
        return False, "缺少相对板块分差(auction_sector_delta)证据"
    ok = v >= -2.0
    return (ok, None if ok else f"相对板块领头分差{v:+g}，落后超过2分")


def _c_sector_rank_top(c: Mapping[str, Any]) -> tuple[bool, str | None]:
    v = _num(c, "auction_sector_rank")
    if v is None:
        return False, "缺少竞价板块排名(auction_sector_rank)证据"
    return (v <= 2.0, None if v <= 2.0 else f"竞价板块排名{int(v)}未居前二")


def _c_sector_catalyst_present(c: Mapping[str, Any]) -> tuple[bool, str | None]:
    notes = c.get("hot_money_notes")
    if not isinstance(notes, (list, tuple)):
        return False, "缺少板块催化/赚钱效应线索(hot_money_notes)"
    has = any("板块" in str(n) or "赚钱效应" in str(n) or "共振" in str(n) for n in notes)
    return (has, None if has else "hot_money_notes 中无板块级催化线索")


def _c_turnover_trend_converging(c: Mapping[str, Any]) -> tuple[bool, str | None]:
    spike = _feature_mapping(c, "volume_spike_ratio")
    if spike is not None:
        if spike.get("available") is False:
            return False, "volume_spike_ratio 不可用，无法判断量能收缩"
        label = str(spike.get("label") or "").lower()
        ok = label in {"shrink", "shrinking", "contracting", "converging"}
        return (ok, None if ok else f"量能趋势标签为{label or '-'}，非收缩")

    trend = c.get("turnover_trend")
    if trend in (None, ""):
        return False, "缺少量能收缩(volume_spike_ratio)证据"
    ok = str(trend).lower() in {"converging", "收敛", "shrinking", "缩量"}
    return (ok, None if ok else f"换手率趋势为{trend}，非收敛")


def _c_ma_converged(c: Mapping[str, Any]) -> tuple[bool, str | None]:
    ma_coil = _feature_mapping(c, "ma_coil_ratio")
    if ma_coil is not None:
        if ma_coil.get("available") is False:
            return False, "ma_coil_ratio 不可用，无法判断均线粘合"
        if "coiled" in ma_coil:
            ok = bool(ma_coil.get("coiled"))
            v = _num(c, "ma_coil_ratio")
            detail = f"{v:g}" if v is not None else "未给出"
            return (ok, None if ok else f"均线粘合度{detail}未达粘合")
        v = _num(c, "ma_coil_ratio")
        if v is None:
            return False, "缺少均线粘合度(ma_coil_ratio)数值证据"
        return (
            v <= _MA_COIL_THRESHOLD,
            None if v <= _MA_COIL_THRESHOLD else f"均线粘合度{v:g}未达粘合(≤{_MA_COIL_THRESHOLD:g})",
        )

    v = _num(c, "ma_convergence")
    if v is None:
        return False, "缺少均线粘合度(ma_coil_ratio)证据"
    # ma_convergence is a small dispersion ratio; smaller = tighter cluster.
    return (v <= 0.03, None if v <= 0.03 else f"均线离散度{v:g}未达粘合(≤0.03)")


def _c_blowoff_volume_stall(c: Mapping[str, Any]) -> tuple[bool, str | None]:
    chg = _num(c, "change_pct")
    spike = _feature_mapping(c, "volume_spike_ratio")
    if spike is not None:
        if spike.get("available") is False or chg is None:
            return False, "缺少量能爆发或涨跌幅证据，无法判断爆量滞涨"
        label = str(spike.get("label") or "").lower()
        ok = label in {"distribution_suspect", "heavy_volume"} and chg <= 2.0
        return (ok, None if ok else f"量能标签{label or '-'}/涨幅{chg:+g}%不构成爆量滞涨")

    vr = _num(c, "volume_ratio_5d")
    if vr is None or chg is None:
        return False, "缺少量能或涨跌幅证据，无法判断爆量滞涨"
    ok = vr >= 2.0 and chg <= 2.0
    return (ok, None if ok else f"量比{vr:g}/涨幅{chg:+g}%不构成爆量滞涨")


_CONDITIONS: dict[str, Callable[[Mapping[str, Any]], tuple[bool, str | None]]] = {
    "turnover_ge_5": _c_turnover_ge_5,
    "turnover_low_band": _c_turnover_low_band,
    "turnover_high_band": _c_turnover_high_band,
    "volume_ratio_ge_1_5": _c_volume_ratio_ge_1_5,
    "sector_lag_lte_2": _c_sector_lag_lte_2,
    "sector_rank_top": _c_sector_rank_top,
    "sector_catalyst_present": _c_sector_catalyst_present,
    "turnover_trend_converging": _c_turnover_trend_converging,
    "ma_converged": _c_ma_converged,
    "blowoff_volume_stall": _c_blowoff_volume_stall,
}


# --------------------------------------------------------------------------- #
# Schema validation — fail closed, never skip a bad pack silently.
# --------------------------------------------------------------------------- #
def validate_pack(pack: Any) -> dict[str, Any]:
    if not isinstance(pack, Mapping):
        raise PackError("strategy pack must be a mapping/object")
    for field in REQUIRED_FIELDS:
        if field not in pack:
            raise PackError(f"strategy pack missing required field: {field}")

    name = pack["name"]
    if not isinstance(name, str) or not name.strip():
        raise PackError("strategy pack 'name' must be a non-empty string")
    if not isinstance(pack["display_name"], str) or not pack["display_name"].strip():
        raise PackError(f"strategy pack '{name}': display_name must be a non-empty string")
    if not isinstance(pack["description"], str) or not pack["description"].strip():
        raise PackError(f"strategy pack '{name}': description must be a non-empty string")
    if pack["category"] not in VALID_CATEGORIES:
        raise PackError(
            f"strategy pack '{name}': category '{pack['category']}' invalid; "
            f"allowed: {sorted(VALID_CATEGORIES)}"
        )
    regimes = pack["market_regimes"]
    if not isinstance(regimes, list) or not regimes or not all(
        isinstance(r, str) and r.strip() for r in regimes
    ):
        raise PackError(f"strategy pack '{name}': market_regimes must be a non-empty list of strings")
    reqs = pack["evidence_requirements"]
    if not isinstance(reqs, list) or not all(isinstance(r, str) for r in reqs):
        raise PackError(f"strategy pack '{name}': evidence_requirements must be a list of strings")
    if not isinstance(pack["interpretation"], str) or not pack["interpretation"].strip():
        raise PackError(f"strategy pack '{name}': interpretation must be a non-empty string")

    hints = pack["score_hints"]
    if not isinstance(hints, list):
        raise PackError(f"strategy pack '{name}': score_hints must be a list")
    for idx, hint in enumerate(hints):
        if not isinstance(hint, Mapping):
            raise PackError(f"strategy pack '{name}': score_hints[{idx}] must be a mapping")
        for key in ("when", "delta", "reason"):
            if key not in hint:
                raise PackError(f"strategy pack '{name}': score_hints[{idx}] missing '{key}'")
        try:
            float(hint["delta"])
        except (TypeError, ValueError) as exc:
            raise PackError(
                f"strategy pack '{name}': score_hints[{idx}].delta must be numeric"
            ) from exc
        if hint["when"] not in _CONDITIONS:
            raise PackError(
                f"strategy pack '{name}': score_hints[{idx}].when '{hint['when']}' "
                f"is not a known condition id"
            )
    return dict(pack)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_pack_file(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        with source.open(encoding="utf-8") as handle:
            payload = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise PackError(f"cannot read strategy pack {source}: {exc}") from exc
    try:
        return validate_pack(payload)
    except PackError as exc:
        raise PackError(f"{source.name}: {exc}") from exc


def load_packs(directory: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Load and validate every pack in ``directory`` (default: config/strategy_packs).

    An invalid pack raises ``PackError`` — packs are never silently skipped, and
    duplicate ``name`` ids are rejected.
    """
    base = Path(directory) if directory is not None else CONFIG_DIR
    if not base.exists():
        raise PackError(f"strategy pack directory not found: {base}")
    packs: dict[str, dict[str, Any]] = {}
    for path in sorted(base.glob("*.yaml")) + sorted(base.glob("*.yml")):
        pack = load_pack_file(path)
        name = pack["name"]
        if name in packs:
            raise PackError(f"duplicate strategy pack name: {name} ({path.name})")
        packs[name] = pack
    return packs


# --------------------------------------------------------------------------- #
# Regime filtering
# --------------------------------------------------------------------------- #
def packs_for_regime(
    regime: str | None,
    *,
    packs: Mapping[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return packs applicable to ``regime`` (temperature tier or S-state label).

    ``regime is None`` returns all packs. A pack listing ``"*"`` in
    ``market_regimes`` matches every regime.
    """
    loaded = dict(packs) if packs is not None else load_packs()
    ordered = [loaded[name] for name in sorted(loaded)]
    if regime is None:
        return ordered
    key = str(regime).strip()
    return [
        pack for pack in ordered
        if REGIME_WILDCARD in pack["market_regimes"] or key in pack["market_regimes"]
    ]


# --------------------------------------------------------------------------- #
# Hint evaluation — pure, interpretation only. NEVER mutates the candidate.
# --------------------------------------------------------------------------- #
def evaluate_pack_hints(
    candidate: Mapping[str, Any],
    *,
    regime: str | None = None,
    packs: Mapping[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Report which pack conditions a candidate hits, with hit/miss reasons.

    Returns one entry per applicable pack. Each condition carries ``hit`` and,
    when not hit, a ``reason``. ``advisory_delta`` sums the ``score_hints`` deltas
    of *hit* conditions — surfaced for explanation only; callers must not fold it
    into any live score (that is exactly the red line this module guards).
    """
    applicable = packs_for_regime(regime, packs=packs)
    results: list[dict[str, Any]] = []
    for pack in applicable:
        conditions: list[dict[str, Any]] = []
        advisory_delta = 0.0
        for hint in pack["score_hints"]:
            predicate = _CONDITIONS[hint["when"]]
            hit, reason = predicate(candidate)
            entry = {
                "id": hint["when"],
                "hit": bool(hit),
                "advisory_delta": float(hint["delta"]),
                "explanation": hint["reason"],
            }
            if not hit:
                entry["reason"] = reason or "条件未满足"
            else:
                advisory_delta += float(hint["delta"])
            conditions.append(entry)
        results.append({
            "pack": pack["name"],
            "display_name": pack["display_name"],
            "category": pack["category"],
            "interpretation": pack["interpretation"],
            "conditions": conditions,
            "advisory_delta": round(advisory_delta, 2),
            "hit_count": sum(1 for c in conditions if c["hit"]),
            "condition_count": len(conditions),
            "influences_live_ranking": False,
            "note": "解释性提示，不影响实盘排序/评分/信号；升级需过 research_gate",
        })
    return results


# --------------------------------------------------------------------------- #
# Registry view — packs are un-gated research hypotheses.
# --------------------------------------------------------------------------- #
def registry_records(
    packs: Mapping[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return each pack as an un-gated research-hypothesis record.

    Mirrors ``strategy_registry`` semantics: a pack id is *not* admissible to
    live weighting until it passes ``research_gate`` and is registered. This view
    makes that explicit and documents the upgrade path.
    """
    loaded = dict(packs) if packs is not None else load_packs()
    records: dict[str, dict[str, Any]] = {}
    for name, pack in loaded.items():
        records[name] = {
            "strategy_id": name,
            "kind": "strategy_pack",
            "category": pack["category"],
            "allowed_in_live_agent": False,
            "gate_decision": "not_gated",
            "market_regimes": list(pack["market_regimes"]),
            "upgrade_path": (
                "run skills/chanlun-backtest/scripts/research_gate.py on locked "
                "rules, pass the out-of-sample wall, then register via "
                "strategy_registry.register_gate_result to become live-eligible"
            ),
        }
    return records


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="策略包加载与查询（解释层，不影响实盘）")
    parser.add_argument("--regime", help="按市场状态/情绪温度档过滤适用策略包")
    parser.add_argument("--registry", action="store_true", help="输出未过门禁的研究假设登记视图")
    args = parser.parse_args()

    if args.registry:
        print(json.dumps(registry_records(), ensure_ascii=False, indent=2))
    else:
        applicable = packs_for_regime(args.regime)
        print(json.dumps(applicable, ensure_ascii=False, indent=2))
