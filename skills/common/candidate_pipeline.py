"""Pure candidate filtering and ranking for the A-share selection pipeline."""

from __future__ import annotations

from datetime import date, datetime
from math import log
from statistics import pstdev
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from indicators import calc_atr
from hot_money_selection import apply_leader_identity
from sector_taxonomy import resolve_sector
from social_attention import candidate_attention_overlay
from weak_market_delivery import assess_delivery_quality


DEFAULT_THEME_WEIGHTING = {
    "enabled": True,
    # additive deltas applied to a candidate's lane score based on the
    # lifecycle stage of the theme its resolved sector belongs to. Conservative
    # by default; every value is config-overridable (§4c: nothing hardcoded).
    "stage_deltas": {
        "mainline": 3.0,
        "diverging": -3.0,
        "fading": -6.0,
        "emerging": 0.0,
    },
}

# Research-only until a separately registered, directionally validated trend
# strategy is promoted.  The registry check below is an additional fail-closed
# guard: changing this constant alone cannot give an unregistered score live
# ranking power.
TREND_STRATEGY_ID = "trend:trend_follow_v2"
DABAN_STRATEGY_ID = "daban:event_score_v1"
TREND_LIVE_WEIGHT = 0.0


def resolve_trend_live_weight(override: Any = None) -> float:
    """Return the live trend weight, requiring both config and registry gates."""
    configured = TREND_LIVE_WEIGHT if override is None else _num(override)
    configured = max(0.0, min(1.0, configured))
    if configured <= 0.0:
        return 0.0
    try:
        import strategy_registry

        registered = _num(strategy_registry.live_weight(TREND_STRATEGY_ID))
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        registered = 0.0
    return round(max(0.0, min(configured, registered)), 4)


def _theme_weighting_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_THEME_WEIGHTING)
    supplied = dict(config or {})
    merged.update({key: value for key, value in supplied.items() if key != "stage_deltas"})
    merged["stage_deltas"] = {
        **DEFAULT_THEME_WEIGHTING["stage_deltas"],
        **dict(supplied.get("stage_deltas") or {}),
    }
    return merged


def theme_stage_adjustment(
    sector: str | None,
    theme_stages: Mapping[str, Mapping[str, Any]] | None,
    config: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    """Resolve a candidate's sector to a live theme stage and return the
    additive score delta (config-gated). No-op ({"delta": 0.0}) whenever the
    hook is disabled, no theme map is supplied, or the sector matches no theme —
    so behaviour is identical to today unless explicitly turned on with data."""
    weighting = _theme_weighting_config(config)
    if not weighting.get("enabled") or not theme_stages or not sector:
        return {"delta": 0.0, "stage": None, "theme_id": None, "notes": []}
    entry = theme_stages.get(str(sector).strip().lower())
    if not isinstance(entry, Mapping):
        return {"delta": 0.0, "stage": None, "theme_id": None, "notes": []}
    stage = str(entry.get("stage") or "")
    delta = float((weighting.get("stage_deltas") or {}).get(stage, 0.0))
    notes = [f"主题[{sector}]处于{stage}阶段 {delta:+.1f}"] if delta else []
    return {"delta": round(delta, 2), "stage": stage or None, "theme_id": entry.get("id"), "notes": notes}


def naked_code(code: Any) -> str:
    text = str(code or "").strip().lower()
    return text[2:] if text.startswith(("sh", "sz")) else text.zfill(6)


def market_code(code: Any) -> str:
    bare = naked_code(code)
    return f"sh{bare}" if bare.startswith("6") else f"sz{bare}"


def is_main_board_10cm(code: Any, name: str = "") -> bool:
    bare = naked_code(code)
    upper_name = str(name or "").upper()
    if "ST" in upper_name or "退" in upper_name:
        return False
    return bare.startswith(("000", "001", "002", "003", "600", "601", "603", "605"))


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", "-"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _listed_days(value: Any, today: date | None = None) -> int | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        listed = (
            datetime.strptime(text[:8], "%Y%m%d").date()
            if len(text) >= 8 and text[:8].isdigit()
            else datetime.fromisoformat(text[:10]).date()
        )
    except ValueError:
        return None
    return ((today or date.today()) - listed).days


def filter_universe(
    records: Iterable[Mapping[str, Any]],
    min_amount: float = 80_000_000,
    min_price: float = 2.0,
    min_listed_days: int = 60,
    today: date | None = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, List[str]]]:
    """Apply deterministic liquidity and tradeability filters."""
    eligible: List[Dict[str, Any]] = []
    rejected: Dict[str, List[str]] = {}
    for raw in records:
        item = dict(raw)
        code = naked_code(item.get("code"))
        name = str(item.get("name") or code)
        reasons: List[str] = []
        price = _num(item.get("price"))
        volume = _num(item.get("volume"))
        amount = _num(item.get("amount"))
        listed_days = _listed_days(item.get("listed_date"), today=today)

        if not code.startswith(("0", "3", "6")):
            reasons.append("仅纳入沪深A股")
        if "ST" in name.upper() or "退" in name:
            reasons.append("ST/*ST/退市整理股票")
        if price <= 0 or volume <= 0:
            reasons.append("停牌或缺少有效成交")
        elif price < min_price:
            reasons.append(f"股价低于{min_price:g}元")
        if amount < min_amount:
            reasons.append(f"成交额低于{min_amount / 1e8:g}亿元")
        if listed_days is None:
            reasons.append("上市日期缺失或无效")
        elif listed_days < min_listed_days:
            reasons.append(f"上市不足{min_listed_days}天")

        item.update({
            "code": code,
            "market_code": market_code(code),
            "name": name,
            "listed_days": listed_days,
        })
        if reasons:
            rejected[code] = reasons
        else:
            eligible.append(item)
    return eligible, rejected


def select_enrichment_universe(
    eligible: Sequence[Mapping[str, Any]],
    limit: int = 350,
) -> List[Dict[str, Any]]:
    """Keep a broad union of liquid, active, and fast-moving stocks for K-line enrichment."""
    if len(eligible) <= limit:
        return [dict(item) for item in eligible]
    quota = max(1, limit // 3)
    selectors = (
        lambda item: _num(item.get("amount")),
        lambda item: _num(item.get("change_pct")),
        lambda item: _num(item.get("turnover")),
    )
    chosen: Dict[str, Dict[str, Any]] = {}
    for selector in selectors:
        for item in sorted(eligible, key=selector, reverse=True)[:quota]:
            chosen[naked_code(item.get("code"))] = dict(item)
    if len(chosen) < limit:
        for item in sorted(eligible, key=lambda row: _num(row.get("amount")), reverse=True):
            chosen.setdefault(naked_code(item.get("code")), dict(item))
            if len(chosen) >= limit:
                break
    return list(chosen.values())[:limit]


def _returns(closes: Sequence[float], periods: int) -> float:
    if len(closes) <= periods or closes[-periods - 1] <= 0:
        return 0.0
    return (closes[-1] / closes[-periods - 1] - 1.0) * 100


_OBSERVABLE_PROXY_NAMES = (
    "up_down_volume_ratio",
    "vwap_hold_ratio",
    "obv_slope",
    "mfi",
    "consecutive_large_order_net",
    "lhb_institution_net_buy",
    "industry_fund_flow",
)


def _proxy_observation(value: Any = None, *, source: str = "unavailable",
                       asof: Any = None, asof_lag_days: Any = None) -> Dict[str, Any]:
    if value is not None and (source == "unavailable" or asof is None):
        value = None
    result = {"value": value, "source": source, "asof": asof}
    if asof_lag_days is not None:
        result["asof_lag_days"] = asof_lag_days
    return result


def _candidate_proxy(item: Mapping[str, Any], name: str,
                     aliases: Sequence[str] = ()) -> Dict[str, Any]:
    raw = next((item[key] for key in (name, *aliases) if key in item), None)
    if isinstance(raw, Mapping):
        value = raw.get("value")
        source = raw.get("source") or item.get("source") or "candidate_input"
        asof = raw.get("asof") or item.get("asof") or item.get("date")
        lag = raw.get("asof_lag_days")
    else:
        value = raw
        source = item.get("source") or "candidate_input"
        asof = item.get("asof") or item.get("date")
        lag = item.get(f"{name}_asof_lag_days")
    if value is None:
        return _proxy_observation()
    return _proxy_observation(value, source=source, asof=asof,
                              asof_lag_days=lag)


def compute_observable_proxies(
    kline: Sequence[Mapping[str, Any]],
    item: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Expose price/volume evidence with provenance, without actor inference."""
    item = item or {}
    rows = []
    for bar in kline or []:
        try:
            close = float(bar.get("close"))
            volume = float(bar.get("volume"))
            high = float(bar.get("high", close))
            low = float(bar.get("low", close))
        except (TypeError, ValueError):
            continue
        if close > 0 and volume >= 0:
            rows.append((close, volume, high, low, bar))
    asof = ((rows[-1][4].get("asof") or rows[-1][4].get("date")
             or rows[-1][4].get("datetime") or rows[-1][4].get("time"))
            if rows else None)
    source = ((rows[-1][4].get("source") or rows[-1][4].get("provider"))
              if rows else None) or "candidate_kline"
    observations = {name: _proxy_observation() for name in _OBSERVABLE_PROXY_NAMES}
    if len(rows) >= 2:
        up = sum(cur[1] for prev, cur in zip(rows, rows[1:]) if cur[0] > prev[0])
        down = sum(cur[1] for prev, cur in zip(rows, rows[1:]) if cur[0] < prev[0])
        observations["up_down_volume_ratio"] = _proxy_observation(
            round(up / down, 6) if down > 0 else None, source=source, asof=asof)
        observations["vwap_hold_ratio"] = _proxy_observation(
            round(sum(cur[0] >= (cur[2] + cur[3] + cur[0]) / 3 for cur in rows) / len(rows), 6),
            source=source, asof=asof)
        obv = [0.0]
        for prev, cur in zip(rows, rows[1:]):
            obv.append(obv[-1] + (cur[1] if cur[0] > prev[0] else -cur[1] if cur[0] < prev[0] else 0))
        window = obv[-min(20, len(obv)):]
        x_mean = (len(window) - 1) / 2
        denominator = sum((i - x_mean) ** 2 for i in range(len(window)))
        if denominator:
            y_mean = sum(window) / len(window)
            slope = sum((i - x_mean) * (value - y_mean) for i, value in enumerate(window)) / denominator
            observations["obv_slope"] = _proxy_observation(
                round(slope, 6), source=source, asof=asof)
        period = min(14, len(rows) - 1)
        positive = negative = 0.0
        for prev, cur in zip(rows[-period - 1:-1], rows[-period:]):
            prev_typical = (prev[0] + prev[2] + prev[3]) / 3
            typical = (cur[0] + cur[2] + cur[3]) / 3
            if typical > prev_typical:
                positive += typical * cur[1]
            elif typical < prev_typical:
                negative += typical * cur[1]
        mfi = 100.0 if positive and not negative else (
            100.0 - 100.0 / (1.0 + positive / negative) if negative else None
        )
        observations["mfi"] = _proxy_observation(
            round(mfi, 6) if mfi is not None else None, source=source, asof=asof)

    observations["consecutive_large_order_net"] = _candidate_proxy(
        item, "consecutive_large_order_net", ("large_order_net", "large_order_net_yi")
    )
    lhb = _candidate_proxy(
        item, "lhb_institution_net_buy", ("institution_lhb_net_buy", "institution_lhb_net_wan")
    )
    if lhb["value"] is not None and "asof_lag_days" not in lhb:
        lhb["asof_lag_days"] = item.get("lhb_asof_lag_days", 1)
    observations["lhb_institution_net_buy"] = lhb
    observations["industry_fund_flow"] = _candidate_proxy(
        item, "industry_fund_flow", ("sector_fund_flow", "industry_net_flow")
    )
    return {
        **{name: obs.get("value") for name, obs in observations.items()},
        "observable_proxies": observations,
        "proxy_provenance": {
            name: {key: value for key, value in obs.items() if key != "value"}
            for name, obs in observations.items()
        },
    }


def compute_price_features(kline: Sequence[Mapping[str, Any]]) -> Dict[str, float]:
    proxy_fields = compute_observable_proxies(kline)
    if len(kline) < 20:
        return {
            "momentum_5d": 0.0,
            "momentum_20d": 0.0,
            "momentum_60d": 0.0,
            "momentum_5d_raw": 0.0,
            "momentum_20d_raw": 0.0,
            "momentum_60d_raw": 0.0,
            "volume_ratio_5d": 0.0,
            "above_ma20": 0.0,
            "above_ma60": 0.0,
            "breakout_20d": 0.0,
            "volatility_20d": 100.0,
            **proxy_fields,
        }
    closes = [_num(bar.get("close")) for bar in kline if _num(bar.get("close")) > 0]
    volumes = [_num(bar.get("volume")) for bar in kline]
    if len(closes) < 20:
        return compute_price_features([])
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / min(60, len(closes))
    prior_high = max(closes[-21:-1]) if len(closes) >= 21 else max(closes[:-1])
    daily_returns = [
        closes[i] / closes[i - 1] - 1.0
        for i in range(max(1, len(closes) - 20), len(closes))
        if closes[i - 1] > 0
    ]
    avg_volume = sum(volumes[-6:-1]) / max(1, len(volumes[-6:-1]))
    momentum_5d = round(_returns(closes, 5), 4)
    momentum_20d = round(_returns(closes, 20), 4)
    momentum_60d = round(_returns(closes, min(60, len(closes) - 1)), 4)
    return {
        "momentum_5d": momentum_5d,
        "momentum_20d": momentum_20d,
        "momentum_60d": momentum_60d,
        "momentum_5d_raw": momentum_5d,
        "momentum_20d_raw": momentum_20d,
        "momentum_60d_raw": momentum_60d,
        "volume_ratio_5d": round(volumes[-1] / avg_volume, 4) if avg_volume > 0 else 0.0,
        "above_ma20": 1.0 if closes[-1] > ma20 else 0.0,
        "above_ma60": 1.0 if closes[-1] > ma60 else 0.0,
        "breakout_20d": 1.0 if closes[-1] >= prior_high else 0.0,
        "volatility_20d": round(pstdev(daily_returns) * 100, 4) if daily_returns else 100.0,
        **proxy_fields,
    }


def _cross_sectional_rank(values: Mapping[str, float]) -> Dict[str, float]:
    """Percentile ranks with deterministic ties, in the closed interval [0, 1]."""
    ordered = sorted(values.items(), key=lambda pair: (pair[1], pair[0]))
    if not ordered:
        return {}
    denominator = max(1, len(ordered) - 1)
    return {code: index / denominator for index, (code, _value) in enumerate(ordered)}


def _slope_residual(values: Mapping[str, float], control: Mapping[str, float]) -> Dict[str, float]:
    """Residualize one cross-sectional factor against one control without numpy."""
    if len(values) < 2:
        return dict(values)
    mean_value = sum(values.values()) / len(values)
    mean_control = sum(control.get(code, 0.0) for code in values) / len(values)
    variance = sum((control.get(code, 0.0) - mean_control) ** 2 for code in values)
    covariance = sum(
        (control.get(code, 0.0) - mean_control) * (value - mean_value)
        for code, value in values.items()
    )
    beta = covariance / variance if variance > 1e-12 else 0.0
    return {
        code: value - beta * (control.get(code, 0.0) - mean_control)
        for code, value in values.items()
    }


def _apply_momentum_neutralization(items: Sequence[Dict[str, Any]]) -> None:
    """Add industry/size/volatility-neutral momentum evidence without reweighting."""
    if not items:
        return
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        group = str(item.get("sector") or item.get("industry") or "__market__")
        groups.setdefault(group, []).append(item)

    adjusted: Dict[str, float] = {}
    size: Dict[str, float] = {}
    volatility: Dict[str, float] = {}
    for item in items:
        code = str(item["code"])
        group = str(item.get("sector") or item.get("industry") or "__market__")
        members = groups[group]
        group_mean = sum(_num(row.get("momentum_20d_raw")) for row in members) / len(members)
        adjusted[code] = _num(item.get("momentum_20d_raw")) - group_mean
        scale = _num(
            item.get("float_mktcap")
            or item.get("market_cap")
            or item.get("amount")
        )
        size[code] = log(max(scale, 1.0))
        volatility[code] = _num(item.get("volatility_20d"), 100.0)

    size_residual = _slope_residual(adjusted, size)
    neutral_residual = _slope_residual(size_residual, volatility)
    size_rank = _cross_sectional_rank(size_residual)
    volatility_rank = _cross_sectional_rank(neutral_residual)
    adjusted_rank = _cross_sectional_rank(adjusted)
    for item in items:
        code = str(item["code"])
        item.update({
            "momentum_20d_ind_adj": round(adjusted[code], 4),
            "momentum_20d_resid": round(size_residual[code], 4),
            "momentum_20d_ind_adj_rank": round(adjusted_rank[code], 4),
            "momentum_20d_size_neutral_rank": round(size_rank[code], 4),
            "momentum_20d_volatility_neutral_rank": round(volatility_rank[code], 4),
            "momentum_20d_neutral_rank": round(volatility_rank[code], 4),
            "size_neutral_rank": round(size_rank[code], 4),
            "volatility_neutral_rank": round(volatility_rank[code], 4),
        })


def _percentiles(items: Sequence[Mapping[str, Any]], key: str) -> Dict[str, float]:
    ordered = sorted(
        ((naked_code(item.get("code")), _num(item.get(key))) for item in items),
        key=lambda pair: pair[1],
    )
    if not ordered:
        return {}
    denominator = max(1, len(ordered) - 1)
    result: Dict[str, float] = {}
    index = 0
    while index < len(ordered):
        end = index + 1
        value = ordered[index][1]
        while end < len(ordered) and ordered[end][1] == value:
            end += 1
        average_rank = (index + end - 1) / 2
        percentile = average_rank / denominator
        for code, _value in ordered[index:end]:
            result[code] = percentile
        index = end
    return result


def strategy_state(
    item: Mapping[str, Any],
    daban_score: Any,
    trend_score: Any,
    trend_live_weight: Any = None,
) -> Dict[str, Any]:
    """Choose one strategy identity; score lanes never lend points to each other."""
    daban_live = max(0.0, _num(daban_score))
    trend_live = max(0.0, _num(trend_score))
    weight = resolve_trend_live_weight(trend_live_weight)
    if weight <= 0.0:
        trend_live = 0.0
    selected_by = (
        item.get("open_selected_by")
        or item.get("auction_selected_by")
        or item.get("selected_by")
    )
    if isinstance(selected_by, Mapping):
        daban_selected = bool(selected_by.get("daban"))
        trend_selected = bool(selected_by.get("trend"))
    else:
        daban_selected = trend_selected = False
    if trend_selected and not daban_selected:
        identity = TREND_STRATEGY_ID
    elif daban_selected and not trend_selected:
        identity = DABAN_STRATEGY_ID
    else:
        identity = (
            DABAN_STRATEGY_ID
            if daban_live >= trend_live and daban_live > 0.0
            else TREND_STRATEGY_ID
        )
    exit_protocol = (
        "daban:t1_event_exit_v1"
        if identity == DABAN_STRATEGY_ID
        else "trend:state_atr_exit_v1"
    )
    previous = str(
        item.get("strategy_identity")
        or item.get("auction_strategy_identity")
        or item.get("open_strategy_identity")
        or ""
    ) or None
    migration_from = previous if previous and previous != identity else None
    confidence_score = daban_live if identity == DABAN_STRATEGY_ID else trend_live
    confidence = (
        "research_only"
        if identity == TREND_STRATEGY_ID and weight <= 0.0
        else "high" if confidence_score >= 80.0
        else "medium" if confidence_score >= 55.0
        else "low"
    )
    daban_net_expectancy = _num(
        item.get("daban_net_expectancy"), daban_live / 100.0
    )
    trend_net_expectancy = _num(
        item.get("trend_net_expectancy"), trend_live / 100.0
    )
    daban_confidence = (
        "high" if daban_live >= 80.0
        else "medium" if daban_live >= 55.0
        else "low"
    )
    return {
        "primary_strategy_id": identity,
        "strategy_identity": identity,
        "exit_protocol": exit_protocol,
        "primary_net_expectancy": round(
            _num(item.get("trend_net_expectancy"), trend_net_expectancy)
            if identity == TREND_STRATEGY_ID
            else _num(item.get("daban_net_expectancy"), daban_net_expectancy),
            6,
        ),
        "primary_confidence": confidence,
        "migration_from": migration_from,
        "strategy_state_event": {
            "event": "strategy_migration" if migration_from else "strategy_identity_selected",
            "from": migration_from,
            "to": identity,
            "trend_live_weight": weight,
        },
        "strategy_state": {
            "primary_strategy_id": identity,
            "primary_score": round(confidence_score, 2),
            "daban_net_expectancy": daban_net_expectancy,
            "trend_net_expectancy": trend_net_expectancy,
            "daban_confidence": daban_confidence,
            "trend_confidence": confidence,
            "trend_live_weight": weight,
        },
        "strategy_live_score": round(confidence_score, 2),
    }


def _positive_emotion_expectancy(signal_ctx: Mapping[str, Any] | None) -> bool:
    """Require an explicit, positive next-day net-premium observation.

    A missing temperature/premium is deliberately different from zero: the
    ladder bonus is then neutral rather than a hidden prior.
    """
    if not signal_ctx:
        return False
    context = dict(signal_ctx)
    temperature = context.get("temperature") or context.get("market_temperature") or {}
    if not isinstance(temperature, Mapping):
        temperature = {}
    tier = str(temperature.get("tier") or context.get("temperature_tier") or "")
    by_state = (
        temperature.get("premium_by_state")
        or context.get("premium_by_state")
        or context.get("next_day_net_premium_by_state")
        or {}
    )
    value = by_state.get(tier) if isinstance(by_state, Mapping) and tier else None
    if isinstance(value, Mapping):
        value = value.get("value", value.get("next_day_net_premium"))
    if value is None:
        value = temperature.get("next_day_net_premium")
    if value is None:
        value = temperature.get("next_day_executable_net_premium")
    try:
        return float(value) > 0.0
    except (TypeError, ValueError):
        return False


def hot_money_bonus(code: str, item: Mapping[str, Any],
                    signal_ctx: Mapping[str, Any] | None,
                    *, apply_lianban_gate: bool = False) -> Tuple[float, List[str]]:
    """游资龙头身份加分（0~20，叠加在量价 daban 基分上）。

    口径来自游资选股研究报告的龙头识别三条件：连板梯队在册（梯队龙头延续性）、
    率先封板（≤09:45 真龙头时间窗）、封单/流通市值比（≥1%最低、≥3%理想）、
    板块涨停集群（赚钱效应联动验证）。signal_ctx 缺失 → 0 分，排名退化为纯量价。
    """
    if not signal_ctx:
        return 0.0, []
    ladder = (signal_ctx.get("lianban_ladder") or {}).get(naked_code(code))
    bonus = 0.0
    notes: List[str] = []
    sector = None
    if isinstance(ladder, Mapping):
        sector = ladder.get("sector")
        lianban = int(ladder.get("lianban") or 0)
        if lianban >= 2:
            if not apply_lianban_gate or _positive_emotion_expectancy(signal_ctx):
                # 二板验证、三板确认、高位板稀缺性分级；不得把所有连板
                # 视为同一种证据。无净溢价证据时保持中性。
                tier_bonus = (
                    4.0 if lianban == 2 else 6.0 if lianban == 3 else 8.0
                ) if apply_lianban_gate else 8.0
                bonus += tier_bonus
                notes.append(f"{lianban}连板梯队在册(+{tier_bonus:.0f})")
            elif apply_lianban_gate:
                notes.append(f"{lianban}连板无情绪正净溢价证据，不加分")
        elif lianban == 1:
            bonus += 5.0
            notes.append("首板在册")
        first_seal = str(ladder.get("first_seal") or "")
        if first_seal and first_seal <= "09:45":
            bonus += 4.0
            notes.append(f"率先封板({first_seal})")
        seal_yi = ladder.get("seal_yi")
        float_cap = _num(item.get("float_mktcap") or item.get("market_cap"))
        if isinstance(seal_yi, (int, float)) and float_cap > 0:
            ratio = seal_yi * 1e8 / (float_cap * 1e8 if float_cap < 1e6 else float_cap)
            if ratio >= 0.03:
                bonus += 4.0
                notes.append(f"封单比{ratio:.1%}(理想)")
            elif ratio >= 0.01:
                bonus += 2.0
                notes.append(f"封单比{ratio:.1%}")
    limitups = signal_ctx.get("sector_limitups") or {}
    if sector and sector in limitups:
        n = int(limitups[sector] or 0)
        if n >= 5:
            bonus += 4.0
            notes.append(f"板块赚钱效应({sector}涨停{n}家)")
        elif n >= 3:
            bonus += 2.0
            notes.append(f"板块共振({sector}涨停{n}家)")
    return min(20.0, bonus), notes


def rank_candidates(
    eligible: Sequence[Mapping[str, Any]],
    kline_by_code: Mapping[str, Sequence[Mapping[str, Any]]],
    signal_ctx: Mapping[str, Any] | None = None,
    theme_stages: Mapping[str, Mapping[str, Any]] | None = None,
    theme_weighting: Mapping[str, Any] | None = None,
    trend_live_weight: Any = None,
) -> List[Dict[str, Any]]:
    """Produce separate cross-sectional ranks for limit-up and trend strategies."""
    live_trend_weight = resolve_trend_live_weight(trend_live_weight)
    enriched: List[Dict[str, Any]] = []
    for raw in eligible:
        item = dict(raw)
        code = naked_code(item.get("code"))
        bars = list(kline_by_code.get(code, []))
        features = compute_price_features(bars)
        # Preserve explicitly supplied upstream proxy evidence while keeping
        # the price features pure and score-compatible.
        features.update(compute_observable_proxies(bars, item))
        item.update(features)
        atr_values = calc_atr(
            [_num(bar.get("high")) for bar in bars],
            [_num(bar.get("low")) for bar in bars],
            [_num(bar.get("close")) for bar in bars],
            14,
        ) if bars else []
        atr14 = atr_values[-1] if atr_values and atr_values[-1] is not None else None
        item.update({
            "code": code,
            "market_code": market_code(code),
            "kline_days": len(bars),
            "feature_ready": len(bars) >= 20,
            "atr14": round(atr14, 3) if atr14 is not None else None,
        })
        enriched.append(item)

    amount_p = _percentiles(enriched, "amount")
    change_p = _percentiles(enriched, "change_pct")
    turnover_p = _percentiles(enriched, "turnover")
    momentum_5_p = _percentiles(enriched, "momentum_5d")
    momentum_20_p = _percentiles(enriched, "momentum_20d")
    momentum_60_p = _percentiles(enriched, "momentum_60d")
    volume_p = _percentiles(enriched, "volume_ratio_5d")
    volatility_p = _percentiles(enriched, "volatility_20d")

    for item in enriched:
        code = item["code"]
        change_pct = _num(item.get("change_pct"))
        limit_proximity = max(0.0, min(1.0, change_pct / 9.8))
        if change_pct > 11.0:
            limit_proximity = 0.0
        daban_eligible = is_main_board_10cm(code, item.get("name", ""))
        daban_score = 100 * (
            0.28 * limit_proximity
            + 0.15 * change_p.get(code, 0.0)
            + 0.15 * amount_p.get(code, 0.0)
            + 0.12 * turnover_p.get(code, 0.0)
            + 0.10 * momentum_5_p.get(code, 0.0)
            + 0.10 * volume_p.get(code, 0.0)
            + 0.10 * _num(item.get("breakout_20d"))
        )
        hm_bonus, hm_notes = hot_money_bonus(
            code, item, signal_ctx, apply_lianban_gate=True
        )
        social = candidate_attention_overlay(code, signal_ctx)
        social_delta = float(social["delta"])
        ladder = ((signal_ctx or {}).get("lianban_ladder") or {}).get(code) or {}
        social_record = social.get("record") or {}
        sector, sector_source = resolve_sector(
            item,
            ladder=ladder,
            social=social_record,
        )
        theme_adj = theme_stage_adjustment(sector, theme_stages, theme_weighting)
        theme_delta = float(theme_adj["delta"])
        daban_score = min(100.0, daban_score + hm_bonus + social_delta + theme_delta)
        if not daban_eligible or not item["feature_ready"]:
            daban_score = 0.0

        overextension = max(0.0, (_num(item.get("momentum_20d")) - 35.0) / 50.0)
        trend_score = 100 * (
            0.22 * momentum_20_p.get(code, 0.0)
            + 0.18 * momentum_60_p.get(code, 0.0)
            + 0.12 * amount_p.get(code, 0.0)
            + 0.10 * _num(item.get("above_ma20"))
            + 0.10 * _num(item.get("above_ma60"))
            + 0.10 * _num(item.get("breakout_20d"))
            + 0.08 * volume_p.get(code, 0.0)
            + 0.10 * (1.0 - volatility_p.get(code, 1.0))
        )
        trend_score -= min(15.0, overextension * 15.0)
        trend_score += social_delta + theme_delta
        if not item["feature_ready"]:
            trend_score = 0.0
        item.update({
            "daban_eligible": daban_eligible,
            "daban_score": round(max(0.0, min(100.0, daban_score)), 2),
            "trend_score": round(max(0.0, min(100.0, trend_score)), 2),
            "trend_score_raw": round(max(0.0, min(100.0, trend_score)), 2),
            "trend_live_score": round(
                max(0.0, min(100.0, trend_score)) * live_trend_weight,
                2,
            ),
            "trend_live_weight": live_trend_weight,
            "trend_lane_status": (
                "live_weighted" if live_trend_weight > 0.0 else "research_only"
            ),
            "hot_money_bonus": round(hm_bonus, 1),
            "hot_money_notes": hm_notes,
            "social_attention_bonus": round(social_delta, 2),
            "social_attention_notes": social["notes"],
            "social_attention": social["record"],
            "theme_stage_bonus": round(theme_delta, 2),
            "theme_stage": theme_adj["stage"],
            "theme_stage_notes": theme_adj["notes"],
            "sector": sector or None,
            "sector_source": sector_source,
            "industry": item.get("industry"),
            "industry_source": item.get("industry_source"),
        })
    _apply_momentum_neutralization(enriched)
    for item in enriched:
        item.update(strategy_state(
            item,
            item["daban_score"],
            item["trend_live_score"],
            live_trend_weight,
        ))

    daban_order = sorted(enriched, key=lambda row: (-row["daban_score"], row["code"]))
    # trend_rank is retained as an evidence/lifecycle rank.  Live selection
    # uses trend_live_rank, which is deliberately zero-weight by default.
    trend_order = sorted(enriched, key=lambda row: (-row["trend_score"], row["code"]))
    trend_live_order = sorted(
        enriched,
        key=lambda row: (-row["trend_live_score"], row["code"]),
    )
    daban_rank = {item["code"]: index + 1 for index, item in enumerate(daban_order)}
    trend_rank = {item["code"]: index + 1 for index, item in enumerate(trend_order)}
    trend_live_rank = {
        item["code"]: index + 1 for index, item in enumerate(trend_live_order)
    }
    for item in enriched:
        item["daban_rank"] = daban_rank[item["code"]]
        item["trend_rank"] = trend_rank[item["code"]]
        item["trend_live_rank"] = trend_live_rank[item["code"]]
    return enriched


def build_watch_pool(
    quotes: Sequence[Mapping[str, Any]],
    kline_by_code: Mapping[str, Sequence[Mapping[str, Any]]],
    watch_limit: int = 200,
    min_amount: float = 80_000_000,
    min_price: float = 2.0,
    min_listed_days: int = 60,
    signal_ctx: Mapping[str, Any] | None = None,
    selection_state: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    eligible, rejected = filter_universe(
        quotes,
        min_amount=min_amount,
        min_price=min_price,
        min_listed_days=min_listed_days,
    )
    ranked = rank_candidates(eligible, kline_by_code, signal_ctx=signal_ctx)
    # Recall-monitoring annotations are observational only.  They are carried
    # through the candidate artifacts so a later full-market snapshot can
    # compare pool coverage without changing either lane's ranking or gates.
    for item in ranked:
        item.setdefault("outside_pool_strong", False)
        item.setdefault("would_have_been_candidate", False)
    ranked = apply_leader_identity(ranked, selection_state, signal_ctx)
    selectable = [item for item in ranked if item["feature_ready"]]
    # --- non-mainboard quality gate ---
    # STAR (688) and ChiNext (300/301) stocks with zero daban score or low
    # trend score are low-quality noise.  Filter them before any lane
    # selection so they never enter the candidate pool.
    _filtered_non_mainboard = 0
    _gated: list = []
    for item in selectable:
        code = str(item.get("code") or "")
        naked = naked_code(code)
        is_star = naked.startswith("688")
        is_chinext = naked.startswith("300") or naked.startswith("301")
        if (is_star or is_chinext) and float(item.get("daban_score") or 0) == 0:
            # zero daban score = completely ineligible for the strategy
            _filtered_non_mainboard += 1
            continue
        if (is_star or is_chinext) and float(item.get("trend_score") or 0) < 70:
            _filtered_non_mainboard += 1
            continue
        _gated.append(item)
    selectable = _gated
    selection_gate_enabled = selection_state is not None

    delivery_by_code: Dict[str, Dict[str, Any]] = {}

    def _delivery(item: Mapping[str, Any], lane: str) -> Dict[str, Any]:
        quality = assess_delivery_quality(
            item,
            lane=lane,
            stage="D0_close",
            selection_state=selection_state,
        )
        if quality["status"] == "deliverable_watch":
            delivery_by_code[naked_code(item.get("code"))] = quality
        return quality

    daban_order = sorted(
        (
            item for item in selectable
            if not selection_gate_enabled or item.get("hot_money_qualified")
            if _delivery(item, "daban")["status"] == "deliverable_watch"
        ),
        key=lambda row: (not row["daban_eligible"], -row["daban_score"], row["code"]),
    )
    trend_order = sorted(
        (
            item for item in selectable
            if _delivery(item, "trend")["status"] == "deliverable_watch"
        ),
        key=lambda row: (-row.get("trend_live_score", 0.0), row["code"]),
    )
    daban_quota = watch_limit // 2
    trend_quota = watch_limit - daban_quota
    daban_codes = {
        item["code"]
        for item in daban_order[:daban_quota]
        if item["daban_eligible"]
        and (not selection_gate_enabled or item.get("hot_money_qualified"))
    }
    trend_codes = {item["code"] for item in trend_order[:trend_quota]}
    selected_codes = daban_codes | trend_codes
    if len(selected_codes) < watch_limit:
        fill_order = sorted(
            selectable,
            key=lambda row: (-row.get("strategy_live_score", 0.0), row["code"]),
        )
        for item in fill_order:
            lane = (
                "trend"
                if item.get("strategy_identity") == TREND_STRATEGY_ID
                else "daban"
            )
            if _delivery(item, lane)["status"] != "deliverable_watch":
                continue
            selected_codes.add(item["code"])
            if len(selected_codes) >= min(watch_limit, len(ranked)):
                break

    candidates = []
    for item in ranked:
        if item["code"] not in selected_codes:
            continue
        selected = dict(item)
        selected["selected_by"] = {
            "daban": item["code"] in daban_codes,
            "trend": item["code"] in trend_codes,
            "balanced_fill": item["code"] not in daban_codes and item["code"] not in trend_codes,
        }
        selected["delivery_quality"] = (
            delivery_by_code.get(item["code"])
            or assess_delivery_quality(
                selected,
                lane="trend" if selected["selected_by"]["trend"] else "daban",
                stage="D0_close",
                selection_state=selection_state,
            )
        )
        candidates.append(selected)
    candidates.sort(
        key=lambda row: (
            -row.get("strategy_live_score", 0.0),
            min(row["daban_rank"], row["trend_rank"]),
            row["code"],
        )
    )
    return {
        "schema": "candidate_watch_pool_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scanned_count": len(quotes),
        "eligible_count": len(eligible),
        "rejected_count": len(rejected),
        "rejected": rejected,
        "candidate_count": len(candidates),
        "hot_money_selection_status": (
            str(selection_state.get("status") or "insufficient_data")
            if selection_state is not None
            else "legacy_unscoped"
        ),
        "candidates": candidates[:watch_limit],
        "evaluated_candidates": ranked,
    }


def _factor_map(factors: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {naked_code(item.get("code")): dict(item) for item in factors if item.get("code")}


# 竞价打分权重（issue #139/#140：前日涨停分曾占 50%，掩盖了当日竞价的全面弱势）。
AUCTION_PRIOR_WEIGHT = 0.25
AUCTION_GAP_WEIGHT = 0.25
AUCTION_AMOUNT_WEIGHT = 0.25
AUCTION_BID_ASK_WEIGHT = 0.15
AUCTION_DELTA_WEIGHT = 0.10
AUCTION_IDEAL_GAP_PCT = 2.0        # 理想高开幅度
AUCTION_GAP_UPPER_SLACK_PCT = 6.0  # 高开超过理想值后的衰减跨度
AUCTION_DECAY_REJECT_PCT = 5.0     # 指示价自高点回落达此幅度 → 一票否决
AUCTION_DECAY_PENALTY_START_PCT = 2.0
AUCTION_DECAY_PENALTY_PER_PCT = 3.0
AUCTION_DECAY_PENALTY_CAP = 15.0
AUCTION_FLAT_OPEN_PENALTY = 12.0   # 涨停次日平开/低开是弱势信号
AUCTION_DEGRADED_BOOK_SCALE = 0.5  # 数据降级时委比/委买净增只按半权计
AUCTION_FILL_MIN_SCORE = 55.0      # balanced_fill 兜底通道的入选下限


def _auction_degraded(item: Mapping[str, Any]) -> bool:
    """竞价盘口是否不可当真信号（免费源零量能/镜像五档）。"""
    quality = item.get("auction_data_quality") or item.get("auction_quality")
    if isinstance(quality, Mapping):
        if str(quality.get("status") or "") in {"degraded", "unavailable"}:
            return True
    elif str(quality or "") in {"degraded", "unavailable"}:
        return True
    return _num(item.get("auction_amount")) <= 0


def _auction_quality_payload(item: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a JSON-safe quality state for every auction-ranked row."""
    quality = item.get("auction_data_quality") or item.get("auction_quality")
    if isinstance(quality, Mapping):
        return dict(quality)
    if quality:
        return {"status": str(quality)}
    if _num(item.get("auction_amount")) <= 0:
        return {
            "status": "unavailable",
            "reasons": ["竞价量能或质量状态缺失"],
        }
    return {"status": "unknown", "reasons": ["因子未提供竞价质量报告"]}


def _without_unavailable_microstructure_claims(
    item: Dict[str, Any],
    quality: Mapping[str, Any],
) -> None:
    """Remove strong book/seal prose when the auction microstructure is unavailable."""
    if quality.get("status") != "unavailable":
        return
    forbidden = ("强盘口", "盘口厚", "封单厚", "封单比", "委买", "委卖")
    for key in ("hot_money_notes", "auction_weakness_notes"):
        notes = item.get(key)
        if isinstance(notes, list):
            item[key] = [
                note for note in notes
                if not any(term in str(note) for term in forbidden)
            ]


def _auction_gap_quality(gap: float) -> float:
    """高开质量：平开/低开 ≈ 0，+2% 最优，高开越多越贵→线性衰减。"""
    if gap <= 0:
        return 0.0
    if gap <= AUCTION_IDEAL_GAP_PCT:
        return gap / AUCTION_IDEAL_GAP_PCT
    return max(0.0, 1.0 - (gap - AUCTION_IDEAL_GAP_PCT) / AUCTION_GAP_UPPER_SLACK_PCT)


def _auction_weakness(item: Mapping[str, Any]) -> Tuple[float, List[str]]:
    """竞价弱势扣分（分）+ 可审计说明。"""
    penalty = 0.0
    notes: List[str] = []
    if item.get("board_status") == "flat_or_low_open":
        penalty += AUCTION_FLAT_OPEN_PENALTY
        notes.append(f"竞价平开/低开 -{AUCTION_FLAT_OPEN_PENALTY:.0f}")
    decay = _num(item.get("auction_price_decay_pct"))
    if decay > AUCTION_DECAY_PENALTY_START_PCT:
        decay_penalty = min(
            AUCTION_DECAY_PENALTY_CAP,
            AUCTION_DECAY_PENALTY_PER_PCT * (decay - AUCTION_DECAY_PENALTY_START_PCT),
        )
        penalty += decay_penalty
        notes.append(f"竞价指示价自高点回落{decay:.2f}% -{decay_penalty:.1f}")
    if _num(item.get("auction_amount")) <= 0:
        notes.append("竞价量能为0，量能分位归零（免费源局限）")
    quality = _auction_quality_payload(item)
    if quality.get("status") == "unavailable":
        notes.append("竞价微结构数据不可用，盘口信号不参与解释")
    elif _auction_degraded(item):
        notes.append(f"竞价盘口数据降级，委比/委买净增按 {AUCTION_DEGRADED_BOOK_SCALE:g} 权重计")
    return round(penalty, 2), notes


def _auction_lane_score(prior: float, shared_score: float, penalty: float) -> float:
    """单条车道分 = 先验分 + 竞价共享分 − 弱势扣分，下限 0。"""
    base = 100 * (AUCTION_PRIOR_WEIGHT * prior + shared_score)
    return round(max(0.0, base - penalty), 2)


def _auction_rejection_reasons(factor: Mapping[str, Any] | None) -> List[str]:
    """竞价硬否决：缺因子 / 一字板 / 跌停 / 指示价崩塌。"""
    if not factor:
        return ["缺少09:25竞价因子"]
    if factor.get("error"):
        return [str(factor["error"])]
    if factor.get("is_yiziban"):
        return ["一字板不可成交"]
    if factor.get("is_limit_down") or factor.get("board_status") == "limit_down":
        return ["竞价跌停，买入无意义"]
    decay = _num(factor.get("auction_price_decay_pct"))
    if decay >= AUCTION_DECAY_REJECT_PCT:
        return [f"竞价指示价自高点回落{decay:.2f}%，疑似诱多出货"]
    return []


def rank_auction_shortlist(
    pool: Mapping[str, Any],
    factors: Sequence[Mapping[str, Any]],
    limit: int = 20,
    signal_ctx: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Combine prior strategy ranks with 09:25 auction microstructure."""
    configured_trend_weight = pool.get("trend_live_weight")
    live_trend_weight = resolve_trend_live_weight(configured_trend_weight)
    factor_by_code = _factor_map(factors)
    rows: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for raw in pool.get("candidates", []):
        item = dict(raw)
        code = naked_code(item.get("code") or item.get("market_code"))
        factor = factor_by_code.get(code)
        reasons = _auction_rejection_reasons(factor)
        if reasons:
            rejected.append({
                **item,
                "code": market_code(code),
                "rejection_reasons": reasons,
            })
            continue
        row = {**item, **factor, "code": market_code(code)}
        row["auction_quality"] = _auction_quality_payload(row)
        _without_unavailable_microstructure_claims(row, row["auction_quality"])
        rows.append(row)

    amount_p = _percentiles(rows, "auction_amount")
    bid_p = _percentiles(rows, "auction_bid_ask_ratio")
    delta_p = _percentiles(rows, "auction_net_bid_delta")
    for item in rows:
        # 零量能全场并列时 _percentiles 会给中位分位，等于白送分——直接归零。
        if _num(item.get("auction_amount")) <= 0:
            amount_p[naked_code(item["code"])] = 0.0
    for item in rows:
        code = naked_code(item["code"])
        gap_quality = _auction_gap_quality(_num(item.get("auction_gap_pct")))
        penalty, weakness_notes = _auction_weakness(item)
        item["auction_weakness_penalty"] = penalty
        item["auction_weakness_notes"] = weakness_notes
        prior_daban = _num(item.get("daban_score")) / 100
        prior_trend = _num(item.get("trend_score")) / 100
        # 盘口降级时不把丢掉的权重再分配出去——数据缺失不该换来分数。
        book_scale = AUCTION_DEGRADED_BOOK_SCALE if _auction_degraded(item) else 1.0
        shared_score = (
            AUCTION_GAP_WEIGHT * gap_quality
            + AUCTION_AMOUNT_WEIGHT * amount_p.get(code, 0.0)
            + book_scale * (
                AUCTION_BID_ASK_WEIGHT * bid_p.get(code, 0.0)
                + AUCTION_DELTA_WEIGHT * delta_p.get(code, 0.0)
            )
        )
        item["auction_daban_score"] = _auction_lane_score(prior_daban, shared_score, penalty)
        item["auction_trend_score"] = _auction_lane_score(prior_trend, shared_score, penalty)
        social = candidate_attention_overlay(code, signal_ctx)
        current_social_delta = 0.5 * float(social["delta"])
        item["auction_daban_score"] = round(max(
            0.0,
            min(100.0, item["auction_daban_score"] + current_social_delta),
        ), 2)
        item["auction_trend_score"] = round(max(
            0.0,
            min(100.0, item["auction_trend_score"] + current_social_delta),
        ) * live_trend_weight, 2)
        item["auction_trend_score_raw"] = round(
            max(0.0, min(100.0, item["auction_trend_score"] / live_trend_weight))
            if live_trend_weight > 0.0
            else max(0.0, min(100.0, _auction_lane_score(prior_trend, shared_score, penalty)
                              + current_social_delta)),
            2,
        )
        item["trend_live_weight"] = live_trend_weight
        item["trend_lane_status"] = (
            "live_weighted" if live_trend_weight > 0.0 else "research_only"
        )
        item["auction_social_attention_delta"] = round(current_social_delta, 2)
        item["social_attention"] = social["record"] or item.get("social_attention")
        item["social_attention_notes"] = social["notes"]
        state = strategy_state(
            item,
            item["auction_daban_score"],
            item["auction_trend_score"],
            live_trend_weight,
        )
        item.update({
            "auction_strategy_identity": state["strategy_identity"],
            "auction_primary_strategy_id": state["primary_strategy_id"],
            "auction_exit_protocol": state["exit_protocol"],
            "auction_primary_net_expectancy": state["primary_net_expectancy"],
            "auction_primary_confidence": state["primary_confidence"],
            "auction_migration_from": state["migration_from"],
            "auction_strategy_state_event": state["strategy_state_event"],
            "auction_strategy_state": state["strategy_state"],
            "auction_score": state["strategy_live_score"],
        })

    sector_groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in rows:
        sector = str(item.get("sector") or "").strip()
        if sector:
            sector_groups.setdefault(sector, []).append(item)
    for members in sector_groups.values():
        ordered = sorted(
            members,
            key=lambda item: (-_num(item.get("auction_daban_score")), item["code"]),
        )
        leader_score = _num(ordered[0].get("auction_daban_score")) if ordered else 0.0
        for rank, item in enumerate(ordered, 1):
            item["auction_sector_rank"] = rank
            item["auction_sector_delta"] = round(
                _num(item.get("auction_daban_score")) - leader_score,
                2,
            )

    def _lane_member(item: Mapping[str, Any], lane: str) -> bool:
        if lane == "daban" and "hot_money_qualified" in item:
            if not item.get("hot_money_qualified"):
                return False
        if (
            assess_delivery_quality(item, lane=lane, stage="09:25")["status"]
            != "deliverable_watch"
        ):
            return False
        selected_by = item.get("selected_by")
        if isinstance(selected_by, Mapping):
            return bool(selected_by.get(lane))
        if lane == "daban":
            return bool(
                item.get(
                    "daban_eligible",
                    is_main_board_10cm(item.get("code"), item.get("name", "")),
                )
            )
        return True

    selected: Dict[str, Dict[str, Any]] = {}

    def _add_lane(lane: str, quota: int) -> None:
        if quota <= 0:
            return
        score_key = f"auction_{lane}_score"
        ordered = sorted(
            (item for item in rows if _lane_member(item, lane)),
            key=lambda item: (-_num(item.get(score_key)), item["code"]),
        )
        added = 0
        for item in ordered:
            code = naked_code(item["code"])
            if code in selected:
                selected[code]["auction_selected_by"][lane] = True
                continue
            chosen = dict(item)
            chosen["delivery_quality"] = assess_delivery_quality(
                chosen,
                lane=lane,
                stage="09:25",
            )
            chosen["auction_selected_by"] = {
                "daban": lane == "daban",
                "trend": lane == "trend",
                "balanced_fill": False,
            }
            selected[code] = chosen
            added += 1
            if added >= quota:
                break

    _add_lane("daban", (limit + 1) // 2)
    _add_lane("trend", limit // 2)
    if len(selected) < limit:
        fill_order = sorted(
            rows,
            key=lambda item: (-_num(item.get("auction_score")), item["code"]),
        )
        for item in fill_order:
            code = naked_code(item["code"])
            if code in selected:
                continue
            selected_by = item.get("selected_by")
            if (
                "hot_money_qualified" in item
                and not item.get("hot_money_qualified")
                and isinstance(selected_by, Mapping)
                and not selected_by.get("trend")
            ):
                continue
            lane = (
                "trend"
                if isinstance(selected_by, Mapping) and selected_by.get("trend")
                else "daban"
            )
            delivery_quality = assess_delivery_quality(item, lane=lane, stage="09:25")
            if delivery_quality["status"] != "deliverable_watch":
                rejected.append({
                    **item,
                    "rejection_reasons": (
                        delivery_quality["reasons"]
                        or ["弱市交付门禁未通过"]
                    ),
                })
                continue
            # 兜底通道只补名额，不负责把弱竞价票捞回来（issue #140：天融信正是兜底进的池）。
            if _num(item.get("auction_score")) < AUCTION_FILL_MIN_SCORE:
                rejected.append({
                    **item,
                    "rejection_reasons": [
                        f"竞价分{_num(item.get('auction_score')):.2f}低于兜底门槛"
                        f"{AUCTION_FILL_MIN_SCORE:g}"
                    ],
                })
                continue
            chosen = dict(item)
            chosen["delivery_quality"] = delivery_quality
            chosen["auction_selected_by"] = {
                "daban": False,
                "trend": False,
                "balanced_fill": True,
            }
            selected[code] = chosen
            if len(selected) >= min(limit, len(rows)):
                break

    shortlist = sorted(
        selected.values(),
        key=lambda item: (-_num(item.get("auction_score")), item["code"]),
    )[:limit]
    for index, item in enumerate(shortlist, 1):
        item["auction_rank"] = index
    selected_codes = {naked_code(item["code"]) for item in shortlist}
    for item in rows:
        if naked_code(item["code"]) not in selected_codes:
            selected_by = item.get("selected_by")
            lanes = []
            if isinstance(selected_by, Mapping):
                lanes = [lane for lane in ("daban", "trend") if selected_by.get(lane)]
            if not lanes:
                lanes = ["trend"]
            qualities = [
                assess_delivery_quality(item, lane=lane, stage="09:25")
                for lane in lanes
            ]
            gate_reasons = [
                reason
                for quality in qualities
                if quality["status"] != "deliverable_watch"
                for reason in quality.get("reasons") or []
            ]
            rejected.append({
                **item,
                "rejection_reasons": (
                    list(dict.fromkeys(gate_reasons))
                    or [f"竞价分策略排名未进入前{limit}"]
                ),
            })
    return {
        "schema": "auction_shortlist_v1",
        "source_asof": pool.get("asof"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_count": len(pool.get("candidates", [])),
        "shortlist_count": len(shortlist),
        "shortlist": shortlist,
        "rejected": rejected,
        "auction_quality": {
            "status": (
                "unavailable"
                if any(
                    _auction_quality_payload(item).get("status") == "unavailable"
                    for item in rows
                )
                else "ok"
            ),
            "unavailable_count": sum(
                _auction_quality_payload(item).get("status") == "unavailable"
                for item in rows
            ),
            "factor_count": len(rows),
        },
    }
