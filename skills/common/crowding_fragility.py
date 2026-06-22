"""Research-only market crowding and fragility metrics.

Pure functions over an already-captured D0/D1 full-market snapshot, so the
module adds no network dependency and no snapshot-lineage bypass. Crowding and
fragility are the report's core "don't chase the climax" deduction signals
(游资方法论报告 4.2 / 5.1 / 7.5): high consensus + thin support = high tail
risk. They are descriptive research evidence, not a buy/sell rule on their own.

Scope and honesty boundary:
- Daily-bar feasible only. Intraday microstructure — order-book depth/spread
  (LiquidityQuality) and tick-level leader influence (5–30min response) — is
  deliberately NOT modeled. No such data source exists here, and faking it
  would violate the report's chapter 10 ("不能复刻也不应假装复刻").
- Scores are cross-sectional bounded ratios, not rolling robust percentiles.
  A single-day cross-section cannot tell constructive churn (流动性吸收的分歧)
  from real exhaustion. So these are early-warning signals consumed first as
  observation (A1), and only conservatively (de-risk direction) by the policy
  gate (A2) — never as a positive admission that bypasses the research gate.
"""

from __future__ import annotations

from statistics import mean
from typing import Any, Mapping, Sequence

from tradeability import limit_pct, round_limit


SCHEMA = "market_crowding_fragility_v1"

DEFAULT_CONFIG: dict[str, Any] = {
    "min_market_observed": 500,
    "min_sector_observed": 3,
    "top_concentration_n": 20,
    "high_open_eps_pct": 0.1,
    "weak_premium_full_pct": 5.0,
    "crowding_weights": {
        "high_open_ratio": 0.35,
        "limitup_high_open_ratio": 0.25,
        "top_concentration": 0.20,
        "amount_hhi": 0.20,
    },
    "fragility_weights": {
        "broke_board_ratio": 0.40,
        "tail_pullback": 0.25,
        "prev_strong_weakness": 0.20,
        "limitdown_pressure": 0.15,
    },
    "signal_thresholds": {
        "crowding": 0.60,
        "fragility": 0.55,
        "broke_board_ratio": 0.30,
        "prev_strong_weakness": 0.40,
    },
}


def _config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_CONFIG)
    supplied = dict(config or {})
    for nested in ("crowding_weights", "fragility_weights", "signal_thresholds"):
        merged[nested] = {**DEFAULT_CONFIG[nested], **dict(supplied.get(nested) or {})}
    merged.update({k: v for k, v in supplied.items() if k not in
                   ("crowding_weights", "fragility_weights", "signal_thresholds")})
    return merged


def _code(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith(("sh", "sz")):
        text = text[2:]
    return text.zfill(6)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", "-"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _price(quote: Mapping[str, Any]) -> float:
    price = _num(quote.get("price"))
    return price if price > 0 else _num(quote.get("close"))


def _limit_up_price(quote: Mapping[str, Any]) -> float | None:
    prev = _num(quote.get("prev_close"))
    if prev <= 0:
        return None
    pct = limit_pct(_code(quote.get("code")), str(quote.get("name") or ""))
    return round_limit(prev, pct, up=True)


def _is_limit_up(quote: Mapping[str, Any]) -> bool:
    threshold = limit_pct(_code(quote.get("code")), str(quote.get("name") or ""))
    return _num(quote.get("change_pct")) >= threshold - 0.2


def _is_limit_down(quote: Mapping[str, Any]) -> bool:
    threshold = limit_pct(_code(quote.get("code")), str(quote.get("name") or ""))
    return _num(quote.get("change_pct")) <= -threshold + 0.2


def _high_open_pct(quote: Mapping[str, Any]) -> float | None:
    gap = quote.get("auction_gap_pct")
    if isinstance(gap, (int, float)):
        return float(gap)
    open_price = _num(quote.get("open"))
    prev = _num(quote.get("prev_close"))
    if open_price > 0 and prev > 0:
        return (open_price / prev - 1.0) * 100.0
    return None


def _broke_board(quote: Mapping[str, Any]) -> bool | None:
    """Touched the limit-up price intraday but failed to close sealed."""
    up = _limit_up_price(quote)
    high = _num(quote.get("high"))
    price = _price(quote)
    if up is None or high <= 0 or price <= 0:
        return None
    if high < up - 0.01:
        return False
    return price < up - 0.01


def _tail_pullback(quote: Mapping[str, Any]) -> float | None:
    """Intraday retreat from the high, only when the bar actually rallied."""
    high = _num(quote.get("high"))
    price = _price(quote)
    prev = _num(quote.get("prev_close"))
    if high <= 0 or price <= 0 or prev <= 0 or high <= prev:
        return None
    return max(0.0, min(1.0, (high - price) / high))


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _weighted(parts: Mapping[str, float | None], weights: Mapping[str, Any]) -> float | None:
    """Average only the available parts, renormalizing weights over them."""
    usable = {k: v for k, v in parts.items() if v is not None and _num(weights.get(k)) > 0}
    total_weight = sum(_num(weights.get(k)) for k in usable)
    if not usable or total_weight <= 0:
        return None
    return round(sum(_num(weights.get(k)) * float(v) for k, v in usable.items()) / total_weight, 4)


def _crowding_components(observed: Sequence[Mapping[str, Any]], cfg: Mapping[str, Any]) -> dict[str, float | None]:
    eps = float(cfg["high_open_eps_pct"])
    high_opens = [pct for q in observed if (pct := _high_open_pct(q)) is not None]
    high_open_ratio = (
        sum(pct > eps for pct in high_opens) / len(high_opens) if high_opens else None
    )
    limitups = [q for q in observed if _is_limit_up(q)]
    limitup_gaps = [pct for q in limitups if (pct := _high_open_pct(q)) is not None]
    limitup_high_open_ratio = (
        sum(pct > eps for pct in limitup_gaps) / len(limitup_gaps) if limitup_gaps else None
    )
    amounts = sorted((_num(q.get("amount")) for q in observed if _num(q.get("amount")) > 0), reverse=True)
    total = sum(amounts)
    top_n = int(cfg["top_concentration_n"])
    top_concentration = (
        sum(amounts[:top_n]) / total if total > 0 and len(amounts) > top_n else None
    )
    n = len(amounts)
    if total > 0 and n > 1:
        hhi = sum((a / total) ** 2 for a in amounts)
        amount_hhi = _clip((hhi - 1.0 / n) / (1.0 - 1.0 / n))
    else:
        amount_hhi = None
    return {
        "high_open_ratio": high_open_ratio,
        "limitup_high_open_ratio": limitup_high_open_ratio,
        "top_concentration": top_concentration,
        "amount_hhi": amount_hhi,
    }


def _fragility_components(
    observed: Sequence[Mapping[str, Any]],
    cfg: Mapping[str, Any],
    previous_ladder_premium: float | None,
) -> dict[str, float | None]:
    broke = 0
    sealed = 0
    for quote in observed:
        if _broke_board(quote) is True:
            broke += 1
        elif _is_limit_up(quote):
            sealed += 1
    touched = broke + sealed
    broke_board_ratio = broke / touched if touched else None
    pullbacks = [pb for q in observed if (pb := _tail_pullback(q)) is not None]
    tail_pullback = round(mean(pullbacks), 4) if pullbacks else None
    if previous_ladder_premium is None:
        prev_strong_weakness = None
    else:
        full = float(cfg["weak_premium_full_pct"]) or 5.0
        prev_strong_weakness = _clip(-float(previous_ladder_premium) / full)
    limitups = sum(1 for q in observed if _is_limit_up(q))
    limitdowns = sum(1 for q in observed if _is_limit_down(q))
    limitdown_pressure = _clip(limitdowns / (limitups + 1)) if (limitups or limitdowns) else None
    return {
        "broke_board_ratio": broke_board_ratio,
        "tail_pullback": tail_pullback,
        "prev_strong_weakness": prev_strong_weakness,
        "limitdown_pressure": limitdown_pressure,
    }


def _signals(crowding: float | None, fragility: float | None,
             components: Mapping[str, Any], cfg: Mapping[str, Any]) -> list[str]:
    th = cfg["signal_thresholds"]
    out: list[str] = []
    if crowding is not None and crowding >= float(th["crowding"]):
        out.append(f"拥挤度偏高({crowding:.2f})：高开普遍/成交集中，买方可能已充分进入")
    if fragility is not None and fragility >= float(th["fragility"]):
        out.append(f"脆弱性偏高({fragility:.2f})：承接转弱，少数股贡献过高")
    broke = components.get("broke_board_ratio")
    if broke is not None and broke >= float(th["broke_board_ratio"]):
        out.append(f"炸板率偏高({broke:.2f})：封板质量差")
    weak = components.get("prev_strong_weakness")
    if weak is not None and weak >= float(th["prev_strong_weakness"]):
        out.append("昨日强势股今日负溢价：赚钱效应衰减")
    return out


def _assess(observed: Sequence[Mapping[str, Any]], cfg: Mapping[str, Any],
            previous_ladder_premium: float | None) -> dict[str, Any]:
    crowding_parts = _crowding_components(observed, cfg)
    fragility_parts = _fragility_components(observed, cfg, previous_ladder_premium)
    crowding_score = _weighted(crowding_parts, cfg["crowding_weights"])
    fragility_score = _weighted(fragility_parts, cfg["fragility_weights"])
    components = {**crowding_parts, **fragility_parts}
    return {
        "crowding_score": crowding_score,
        "fragility_score": fragility_score,
        "components": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in components.items()},
        "signals": _signals(crowding_score, fragility_score, components, cfg),
    }


def sector_crowding_fragility(
    members: Sequence[Mapping[str, Any]],
    *,
    previous_ladder_premium: float | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Crowding/fragility for one sector's members (low member threshold)."""
    cfg = _config(config)
    observed = [q for q in members if _price(q) > 0 and q.get("change_pct") not in (None, "", "-")]
    if len(observed) < int(cfg["min_sector_observed"]):
        return {"status": "insufficient_data", "observed": len(observed),
                "crowding_score": None, "fragility_score": None, "components": {}, "signals": []}
    return {"status": "ready", "observed": len(observed),
            **_assess(observed, cfg, previous_ladder_premium)}


def build_market_crowding_fragility(
    quotes: Sequence[Mapping[str, Any]],
    signal_context: Mapping[str, Any] | None = None,
    market_timing: Mapping[str, Any] | None = None,
    *,
    event_asof: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Market-wide crowding/fragility from a full-market D0 snapshot.

    Fails closed to ``status=insufficient_data`` (scores None) when coverage is
    thin, so a missing snapshot never fabricates a high-risk verdict that would
    wrongly suppress recommendations.
    """
    cfg = _config(config)
    observed = [q for q in quotes if _price(q) > 0 and q.get("change_pct") not in (None, "", "-")]
    previous_ladder_premium = None
    if market_timing is not None:
        previous_ladder_premium = market_timing.get("previous_ladder_premium")
    if len(observed) < int(cfg["min_market_observed"]):
        return {
            "schema": SCHEMA,
            "status": "insufficient_data",
            "event_asof": event_asof,
            "observed": len(observed),
            "crowding_score": None,
            "fragility_score": None,
            "components": {},
            "signals": [],
            "reason": f"全市场有效行情不足: {len(observed)} < {int(cfg['min_market_observed'])}",
        }
    assessed = _assess(observed, cfg, previous_ladder_premium)
    return {
        "schema": SCHEMA,
        "status": "ready",
        "event_asof": event_asof,
        "observed": len(observed),
        "previous_ladder_premium": previous_ladder_premium,
        **assessed,
    }
