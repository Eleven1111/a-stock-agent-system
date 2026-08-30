"""Canonical forward evidence shared by the six research strategies.

This is the deep module at the provider/strategy seam.  It owns the bounded
cohort, point-in-time joins, provenance and availability report; strategy
implementations only consume its normalized records.  No reconstructed
5-minute event is accepted as canonical evidence here.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time
from math import sqrt
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import hot_money_selection
import minute_derived
import reverse_volume
import daban_config
import sentiment_score
import rank_surprise
import ice_point_reversal


SCHEMA = "strategy_evidence_daily_v1"
DEFAULT_MAX_CODES = 160
SHANGHAI = ZoneInfo("Asia/Shanghai")
CANONICAL_CLASS = "canonical_forward"
EXPLORATORY_CLASS = "exploratory_reconstruction"
UNAVAILABLE_CLASS = "unavailable"
RECONSTRUCTED_SOURCES = {"local_history_cache"}

REQUIRED_FIELDS = {
    "rank_surprise": (
        "sector", "auction_strength", "prior_return_pct", "prior_strength",
        "board_height", "volume_ratio",
    ),
    "divergence_reseal": (
        "sector", "sector_limit_up_count", "sector_fast_seal_count", "reseal_time",
        "pre_reseal_turnover_pct", "turnover_baseline_median_pct",
        "turnover_baseline_sample_days",
    ),
    "assist_arbitrage": (
        "sector", "board_height", "sector_breadth_count", "change_pct",
        "leader_score_shadow", "leader_confirmed", "breakout_time",
    ),
    "preleader_arbitrage": ("attribute",),
    "reverse_volume": (
        "was_prior_period_top_leader", "drawdown_pct", "volatility_contraction_ratio",
        "volume_percentile_20d", "max_down_minute_volume_prior", "max_up_minute_volume",
        "second_max_up_minute_volume", "pullback_max_down_minute_volume",
        "breakout_above_balance_zone",
    ),
    "ice_point_reversal": (),
}


class EvidenceBudgetExceeded(ValueError):
    """The bounded cohort exceeded its declared budget; never truncate it."""


def naked_code(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    for prefix in ("sh", "sz", "bj"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text.zfill(6) if text.isdigit() else ""


def _number(value: Any) -> float | None:
    if value in (None, "", "-") or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _time(value: Any) -> str | None:
    if value in (None, "", "-"):
        return None
    try:
        text = str(int(float(value)))
    except (TypeError, ValueError):
        text = str(value).replace(":", "").strip()
    if not text.isdigit() or len(text) > 6:
        return None
    return text.zfill(6)


def _auction_rows(payload: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for key in ("factors", "shortlist", "research_candidates", "rejected"):
        value = (payload or {}).get(key)
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, Mapping))
    return rows


def _auction_cohort_rows(payload: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    """Only actionable shortlists expand the external-request cohort.

    ``factors``/``research_candidates``/``rejected`` are useful join sidecars but
    can contain the full market; treating them as targets recreates the exact
    API explosion this module exists to prevent.
    """
    rows: list[Mapping[str, Any]] = []
    for key in ("shortlist", "execution_candidates"):
        value = (payload or {}).get(key)
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, Mapping))
    return rows


def select_cohort(
    candidates: Sequence[Mapping[str, Any]],
    auction: Mapping[str, Any] | None,
    limitup_rows: Sequence[Mapping[str, Any]],
    *,
    tracked_codes: Iterable[Any] = (),
    extra_codes: Iterable[Any] = (),
    max_codes: int = DEFAULT_MAX_CODES,
) -> list[str]:
    """Return the sorted event-driven union, or reject the whole oversized run."""
    candidate_codes = {naked_code(row.get("code") or row.get("market_code")) for row in candidates}
    codes = {
        naked_code(row.get("代码") or row.get("code")) for row in limitup_rows
    }
    codes.update(
        naked_code(row.get("code") or row.get("market_code"))
        for row in _auction_cohort_rows(auction)
    )
    codes.update(naked_code(code) for code in tracked_codes)
    codes.update(naked_code(code) for code in extra_codes)
    codes.discard("")
    # Provider-only event rows remain valid; candidate-only rows enter only via
    # auction/tracking, never because a 500-name discovery pool happened to contain them.
    result = sorted(codes | (candidate_codes & codes))
    if len(result) > int(max_codes):
        raise EvidenceBudgetExceeded(f"evidence cohort exceeds budget: {len(result)}>{max_codes}")
    return result


def rank_surprise_targets(
    asof: str,
    candidates: Sequence[Mapping[str, Any]],
    auction: Mapping[str, Any] | None,
    daily_bars: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Preselect S1 names that can pass its two cross-sectional rank gates.

    This pure, local step decides which additional names deserve the single
    post-close minute request. It does not evaluate volume or market state.
    """
    auction_by_code: dict[str, Mapping[str, Any]] = {}
    for item in _auction_rows(auction):
        code = naked_code(item.get("code") or item.get("market_code"))
        if code:
            auction_by_code[code] = item
    bars = _bar_groups(daily_bars)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in candidates:
        code = naked_code(raw.get("code") or raw.get("market_code"))
        sector = str(raw.get("sector") or raw.get("industry") or "")
        daily = _daily_fields(code, asof, bars)
        auction_row = auction_by_code.get(code) or {}
        actual = auction_row.get("auction_gap_pct")
        if actual is None:
            actual = auction_row.get("auction_strength")
        prior = _number(daily.get("prior_strength"))
        gap = _number(actual)
        if code and sector and prior is not None and gap is not None:
            grouped[sector].append({"code": code, "prior": prior, "auction": gap})
    settings = rank_surprise.config()
    minimum = int(settings.get("min_peer_count", 5))
    bottom = float(settings.get("prior_rank_bottom_pct", 0.30))
    top = float(settings.get("auction_rank_top_pct", 0.20))
    selected: list[str] = []
    for peers in grouped.values():
        if len(peers) < minimum + 1:  # expected_gap excludes the target itself
            continue
        prior_ranks = rank_surprise.percentile_ranks([row["prior"] for row in peers])
        auction_ranks = rank_surprise.percentile_ranks([row["auction"] for row in peers])
        selected.extend(
            row["code"] for row, prior_pct, auction_pct in zip(peers, prior_ranks, auction_ranks)
            if prior_pct is not None and auction_pct is not None
            and prior_pct <= bottom and auction_pct >= 1.0 - top
        )
    return sorted(set(selected))


def _standard_limitup(row: Mapping[str, Any]) -> dict[str, Any]:
    opens = _number(row.get("炸板次数", row.get("open_board_count")))
    last = _time(row.get("最后封板时间", row.get("last_seal_time")))
    return {
        "code": naked_code(row.get("代码") or row.get("code")),
        "sector": row.get("所属行业", row.get("sector")),
        "first_seal_time": _time(row.get("首次封板时间", row.get("first_seal_time"))),
        "last_seal_time": last,
        "reseal_time": last if opens is not None and opens > 0 else None,
        "open_board_count": opens,
        "float_market_cap": _number(row.get("流通市值", row.get("float_market_cap"))),
        "event_source": str(row.get("event_source") or "eastmoney_zt_pool"),
    }


def _bar_groups(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in rows:
        code = naked_code(raw.get("code"))
        day = str(raw.get("trading_date") or raw.get("date") or "")[:10]
        if code and day:
            groups[code].append({**raw, "trading_date": day})
    for values in groups.values():
        values.sort(key=lambda item: item["trading_date"])
    return groups


def _percentile(values: Sequence[float], value: float) -> float | None:
    if not values:
        return None
    return sum(1 for item in values if item <= value) / len(values)


def _std(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    avg = sum(values) / len(values)
    return sqrt(sum((item - avg) ** 2 for item in values) / (len(values) - 1))


def _daily_fields(code: str, asof: str, groups: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    prior = [row for row in groups.get(code, []) if str(row.get("trading_date")) < asof]
    if not prior:
        return {}
    last = prior[-1]
    returns = [_number(row.get("pct_chg")) for row in prior[-20:]]
    returns = [value for value in returns if value is not None]
    volumes = [_number(row.get("volume")) for row in prior[-20:]]
    volumes = [value for value in volumes if value is not None]
    closes = [_number(row.get("close")) for row in prior]
    highs = [_number(row.get("high")) for row in prior]
    current_close = closes[-1] if closes else None
    peak = max((value for value in highs if value is not None), default=None)
    short_std = _std(returns[-5:])
    long_std = _std(returns[-20:])
    return {
        "prior_return_pct": _number(last.get("pct_chg")),
        "prior_strength": _number(last.get("pct_chg")),
        "volume_baseline_per_minute": (
            sum(volumes[-5:]) * minute_derived.LOT_SHARES / (5 * minute_derived.SESSION_MINUTES)
            if len(volumes) >= 5 else None
        ),
        "drawdown_pct": ((peak - current_close) / peak if peak and current_close is not None else None),
        "volatility_contraction_ratio": (
            short_std / long_std if short_std is not None and long_std not in (None, 0) else None
        ),
        "volume_percentile_20d": (
            _percentile(volumes[:-1], volumes[-1]) if len(volumes) >= 20 else None
        ),
        "balance_zone_high": max(
            (value for value in (_number(row.get("high")) for row in prior[-20:])
             if value is not None),
            default=None,
        ),
    }


def _field(row: dict[str, Any], name: str, value: Any, source: str, asof: str) -> None:
    row[name] = value
    row.setdefault("evidence_provenance", {})[name] = {
        "source": source if value is not None else "unavailable",
        "source_identity": source if value is not None else None,
        "asof": asof if value is not None else None,
        "observed_at": (
            datetime.combine(date.fromisoformat(asof), time(15, 0), tzinfo=SHANGHAI).isoformat()
            if value is not None else None
        ),
    }


def _directional_peaks(
    raw_rows: Sequence[Mapping[str, Any]],
) -> tuple[float | None, float | None, float | None, float | None]:
    tagged = reverse_volume._attach_direction(raw_rows, minute_derived.SOURCE_TENCENT_INTRADAY)
    up = sorted(
        (float(row["volume_shares"]) for row in (tagged or []) if row.get("direction") == "up"),
        reverse=True,
    )
    down_rows = [row for row in (tagged or []) if row.get("direction") == "down"]
    down = [float(row["volume_shares"]) for row in down_rows]
    first_up_minute = next(
        (int(row["minute"]) for row in (tagged or []) if row.get("direction") == "up"), None
    )
    pullback = [
        float(row["volume_shares"]) for row in down_rows
        if first_up_minute is not None and int(row["minute"]) > first_up_minute
    ]
    return (
        up[0] if up else None,
        up[1] if len(up) > 1 else None,
        max(down) if down else None,
        max(pullback) if pullback else None,
    )


def _breakout_time(raw_rows: Sequence[Mapping[str, Any]], level: Any) -> str | None:
    threshold = _number(level)
    if threshold is None:
        return None
    for raw in raw_rows:
        price = _number(raw.get("price"))
        minute = minute_derived.parse_minute(raw.get("time"))
        if price is not None and minute is not None and price > threshold:
            return f"{minute // 60:02d}{minute % 60:02d}00"
    return None


def _present(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, Mapping) and value.get("status") not in (None, "ok", "ready"):
        return False
    return True


def _coverage(
    record_sets: Mapping[str, Sequence[Mapping[str, Any]]], market_state: Mapping[str, Any],
    *, pretable_ready: bool, source_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for strategy, fields in REQUIRED_FIELDS.items():
        records = list(record_sets.get(strategy) or [])
        missing = sorted({field for row in records for field in fields if not _present(row.get(field))})
        ready = sum(1 for row in records if all(_present(row.get(field)) for field in fields))
        market_missing: list[str] = []
        if strategy == "rank_surprise" and not _present(market_state.get("dominant_state")):
            market_missing.append("market_state.dominant_state")
        if strategy == "reverse_volume":
            for field in ("available", "deteriorating"):
                if market_state.get(field) is None:
                    market_missing.append(f"market_state.{field}")
        if strategy == "ice_point_reversal":
            if market_state.get("sentiment_score_status") != "ok":
                market_missing.append("market_state.sentiment_series_ready")
            for field in ("leader_confirm", "sector_breadth_top"):
                if market_state.get(field) is None:
                    market_missing.append(f"market_state.{field}")
            if not records:
                market_missing.append("market_state.tradeable_leader_binding")
            ready = len(records) if not market_missing else 0
        if strategy == "preleader_arbitrage" and not pretable_ready:
            market_missing.append("preleader_pretable")
        if market_missing:
            ready = 0
        output[strategy] = {
            "record_count": len(records),
            "ready_records": ready,
            "coverage_ratio": round(
                ready / len(records), 4
            ) if records else 0.0,
            "missing_fields": sorted(missing + market_missing),
            "source_missing_fields": sorted({
                field for row in source_records for field in fields
                if not _present(row.get(field))
            }),
        }
    return output


def _attach_static_evidence(
    row: dict[str, Any], *, asof: str, base: Mapping[str, Any], event: Mapping[str, Any],
    auction_row: Mapping[str, Any], peers: Sequence[Mapping[str, Any]],
    bars: Mapping[str, Sequence[Mapping[str, Any]]], fast_seal_minute: int,
) -> None:
    code = str(row["code"])
    for name, value in _daily_fields(code, asof, bars).items():
        _field(row, name, value, "local_market_history", asof)
    auction_strength = auction_row.get("auction_gap_pct")
    if auction_strength is None:
        auction_strength = auction_row.get("auction_strength")
    _field(row, "auction_strength", _number(auction_strength), "auction_snapshot", asof)
    seal = event.get("first_seal_time") or _time(base.get("first_seal"))
    _field(row, "first_seal", seal, event.get("event_source") or "candidate_pool", asof)
    _field(row, "reseal_time", event.get("reseal_time"), event.get("event_source") or "", asof)
    sector = str(row.get("sector") or "")
    _field(row, "sector_limit_up_count", len(peers) if sector else None,
           "eastmoney_zt_pool", asof)
    seal_minutes = [minute_derived.parse_minute(peer.get("first_seal_time")) for peer in peers]
    fast_count = (
        sum(1 for value in seal_minutes if value is not None and value <= fast_seal_minute)
        if peers and all(value is not None for value in seal_minutes) else None
    )
    _field(row, "sector_fast_seal_count", fast_count if sector else None,
           "eastmoney_zt_pool", asof)
    _field(row, "sector_breadth_count", len(peers) if sector else None,
           "eastmoney_zt_pool", asof)


def _attach_minute_evidence(
    row: dict[str, Any], *, asof: str, base: Mapping[str, Any], event: Mapping[str, Any],
    raw_minutes: Sequence[Mapping[str, Any]], bars: Mapping[str, Sequence[Mapping[str, Any]]],
    prior_record: Mapping[str, Any],
) -> None:
    normalized = minute_derived.normalize_tencent_minute(raw_minutes)
    breakout = _breakout_time(raw_minutes, row.get("balance_zone_high"))
    _field(row, "breakout_time", breakout, "tencent_minute_intraday+local_market_history", asof)
    _field(row, "breakout_above_balance_zone", True if breakout else None,
           "tencent_minute_intraday+local_market_history", asof)
    ratio = minute_derived.volume_ratio_at(
        normalized, checkpoint="09:45", baseline_per_minute=row.get("volume_baseline_per_minute")
    )
    _field(row, "volume_ratio", ratio.get("value"), "tencent_minute_intraday:09:45", asof)
    row["volume_ratio_source"] = (
        "tencent_minute_intraday:09:45" if ratio.get("value") is not None else None
    )
    float_shares = minute_derived.float_shares_from_mktcap(
        event.get("float_market_cap"), base.get("price")
    )
    turnover = minute_derived.cumulative_turnover_before(
        normalized, event.get("reseal_time"), float_shares
    )
    _field(row, "pre_reseal_turnover_pct", turnover.get("value"),
           "tencent_minute_intraday+eastmoney_zt_pool", asof)
    # Daily ``turn`` is full-session turnover. S2 needs cumulative turnover at
    # D0's same reseal clock minute across prior sessions. No such panel exists
    # yet, so fail closed instead of substituting a different denominator.
    _field(row, "turnover_baseline_median_pct", None, "unavailable", asof)
    _field(row, "turnover_baseline_sample_days", None, "unavailable", asof)
    row["turnover_baseline_semantics"] = "unavailable"
    up_peak, second_up, down_peak, pullback_down = _directional_peaks(raw_minutes)
    _field(row, "max_up_minute_volume", up_peak, "tencent_minute_intraday", asof)
    _field(row, "second_max_up_minute_volume", second_up, "tencent_minute_intraday", asof)
    prior_down = prior_record.get("tracked_max_down_minute_volume")
    _field(row, "max_down_minute_volume_prior", prior_down, "prior_strategy_evidence", asof)
    _field(row, "pullback_max_down_minute_volume", pullback_down,
           "tencent_minute_intraday", asof)
    row["tracked_max_down_minute_volume"] = max(
        (value for value in (prior_down, down_peak) if value is not None), default=None
    )


def _market_evidence(
    selection: Mapping[str, Any] | None, sentiment_series: Sequence[Mapping[str, Any]],
    sector_events: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    selection_state = dict(selection or {})
    market = dict(selection_state.get("market_state") or {})
    market.setdefault("available", bool(market.get("dominant_state")))
    market["sentiment_series"] = list(sentiment_series)
    market["sentiment_provenance"] = [
        {
            "trading_date": str(row.get("trading_date") or ""),
            "observed_at": row.get("observed_at"),
            "source_identity": row.get("source"),
            "evidence_class": (
                EXPLORATORY_CLASS
                if str(row.get("source") or "") in RECONSTRUCTED_SOURCES
                else CANONICAL_CLASS
                if row.get("observed_at") and row.get("source")
                else UNAVAILABLE_CLASS
            ),
        }
        for row in sentiment_series
    ]
    market["sector_breadth_top"] = max((len(value) for value in sector_events.values()), default=None)
    score = sentiment_score.compute_sentiment_score(list(sentiment_series))
    delta = _number(score.get("delta"))
    acceleration = _number(score.get("delta_squared"))
    if market.get("deteriorating") is None:
        market["deteriorating"] = (
            bool(delta < 0 and acceleration < 0)
            if delta is not None and acceleration is not None else None
        )
    market["sentiment_score_status"] = score.get("status")
    return selection_state, market


def _qualification(
    coverage: Mapping[str, Mapping[str, Any]], market: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Derive strategy eligibility from observed inputs, never caller booleans."""
    output: dict[str, dict[str, Any]] = {}
    for strategy in REQUIRED_FIELDS:
        reasons: list[str] = []
        klass = CANONICAL_CLASS
        if int((coverage.get(strategy) or {}).get("ready_records") or 0) <= 0:
            klass = UNAVAILABLE_CLASS
            reasons.append("required_strategy_evidence_unavailable")
        if strategy == "divergence_reseal":
            klass = UNAVAILABLE_CLASS
            reasons = ["historical_same_clock_turnover_baseline_unavailable"]
        if strategy == "ice_point_reversal":
            provenance = list(market.get("sentiment_provenance") or [])
            if any(row.get("evidence_class") == EXPLORATORY_CLASS for row in provenance):
                klass = EXPLORATORY_CLASS
                reasons = ["sentiment_series_contains_exploratory_reconstruction"]
            elif not provenance or any(
                row.get("evidence_class") != CANONICAL_CLASS for row in provenance
            ):
                klass = UNAVAILABLE_CLASS
                reasons = ["sentiment_series_provenance_incomplete"]
        output[strategy] = {
            "class": klass,
            "canonical_forward_eligible": klass == CANONICAL_CLASS,
            "reasons": reasons,
        }
    return output


def _rank_universe(
    *, asof: str, codes: Sequence[str], records: Sequence[Mapping[str, Any]],
    candidates: Mapping[str, Mapping[str, Any]], auction: Mapping[str, Mapping[str, Any]],
    bars: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    record_by_code = {str(row.get("code") or ""): row for row in records}
    output: list[dict[str, Any]] = []
    for code in sorted(set(auction) & set(candidates) | set(codes)):
        if code in record_by_code:
            output.append(dict(record_by_code[code]))
            continue
        base = dict(candidates.get(code) or {})
        sector = str(base.get("sector") or base.get("industry") or "")
        row = {**base, "code": code, "date": asof, "sector": sector or None,
               "attribute": sector or None, **_daily_fields(code, asof, bars)}
        auction_value = auction.get(code, {}).get("auction_gap_pct")
        if auction_value is None:
            auction_value = auction.get(code, {}).get("auction_strength")
        row["auction_strength"] = _number(auction_value)
        row["board_height"] = row.get("board_height") if row.get("board_height") is not None else 0
        row["prior_strength_tiebreak"] = -float(
            minute_derived.parse_minute(base.get("first_seal")) or 0
        )
        row["volume_ratio"], row["volume_ratio_source"] = None, None
        output.append(row)
    peer_fields = ("sector", "auction_strength", "prior_return_pct", "prior_strength", "board_height")
    return [row for row in output if all(_present(row.get(field)) for field in peer_fields)]


def _cohort_records(
    asof: str, *, candidates: Sequence[Mapping[str, Any]], auction: Mapping[str, Any] | None,
    limitup_rows: Sequence[Mapping[str, Any]], minute_rows: Mapping[str, Sequence[Mapping[str, Any]]],
    daily_bars: Sequence[Mapping[str, Any]], tracked_codes: Iterable[Any],
    extra_codes: Iterable[Any], previous_records: Mapping[str, Mapping[str, Any]] | None,
    max_codes: int,
) -> tuple[list[dict[str, Any]], list[str], dict[str, dict[str, Any]],
           dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]],
           dict[str, list[dict[str, Any]]], set[str]]:
    pool = [event for event in (_standard_limitup(row) for row in limitup_rows) if event["code"]]
    codes = select_cohort(
        candidates, auction, limitup_rows, tracked_codes=tracked_codes,
        extra_codes=extra_codes, max_codes=max_codes,
    )
    candidate_by_code = {
        naked_code(row.get("code") or row.get("market_code")): dict(row) for row in candidates
    }
    auction_by_code: dict[str, dict[str, Any]] = {}
    for item in _auction_rows(auction):
        code = naked_code(item.get("code") or item.get("market_code"))
        if code:
            auction_by_code.setdefault(code, {}).update(dict(item))
    event_by_code = {row["code"]: row for row in pool}
    sector_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in pool:
        sector_events[str(event.get("sector") or "")].append(event)
    bars = _bar_groups(daily_bars)
    tracked = {naked_code(code) for code in tracked_codes}
    previous = dict(previous_records or {})
    fast_minute = int(daban_config.section("divergence_reseal").get("fast_board_seal_minute", 571))
    records: list[dict[str, Any]] = []
    for code in codes:
        base, event = dict(candidate_by_code.get(code) or {}), event_by_code.get(code) or {}
        sector = str(base.get("sector") or event.get("sector") or base.get("industry") or "")
        row = {
            **dict(previous.get(code) or {}), **base, "code": code, "date": asof,
            "sector": sector or None, "attribute": sector or None,
            "leader_confirmed": bool(event.get("first_seal_time")) if event else None,
            "was_prior_period_top_leader": code in tracked if tracked else None,
            "event_source": event.get("event_source"),
        }
        _attach_static_evidence(
            row, asof=asof, base=base, event=event,
            auction_row=auction_by_code.get(code) or {}, peers=sector_events.get(sector, []),
            bars=bars, fast_seal_minute=fast_minute,
        )
        _attach_minute_evidence(
            row, asof=asof, base=base, event=event,
            raw_minutes=list(minute_rows.get(code) or []), bars=bars,
            prior_record=previous.get(code) or {},
        )
        records.append(row)
    return records, codes, candidate_by_code, auction_by_code, sector_events, bars, tracked


def build_evidence(
    asof: str,
    *,
    candidates: Sequence[Mapping[str, Any]],
    auction: Mapping[str, Any] | None,
    selection: Mapping[str, Any] | None,
    limitup_rows: Sequence[Mapping[str, Any]],
    minute_rows: Mapping[str, Sequence[Mapping[str, Any]]],
    daily_bars: Sequence[Mapping[str, Any]],
    sentiment_series: Sequence[Mapping[str, Any]],
    tracked_codes: Iterable[Any] = (),
    extra_codes: Iterable[Any] = (),
    previous_records: Mapping[str, Mapping[str, Any]] | None = None,
    preleader_pretable: Mapping[str, Any] | None = None,
    preleader_pretable_status: str = "no_prior_pretable_artifact",
    max_codes: int = DEFAULT_MAX_CODES,
) -> dict[str, Any]:
    """Build one immutable-ready artifact from already fetched inputs."""
    for label, payload in (("auction", auction), ("selection", selection)):
        payload_asof = str((payload or {}).get("asof") or "")[:10]
        if payload_asof and payload_asof != asof:
            raise ValueError(f"{label} asof mismatch: expected {asof}, got {payload_asof}")
    (records, codes, candidate_by_code, auction_by_code,
     sector_events, bars, tracked) = _cohort_records(
        asof, candidates=candidates, auction=auction, limitup_rows=limitup_rows,
        minute_rows=minute_rows, daily_bars=daily_bars, tracked_codes=tracked_codes,
        extra_codes=extra_codes, previous_records=previous_records, max_codes=max_codes,
    )
    selection_state, market = _market_evidence(selection, sentiment_series, sector_events)
    records = hot_money_selection.apply_leader_score_shadow(
        records,
        selection_state,
        deep_pool_codes=codes,
        market_median_change=None,
        back_row_history=sentiment_series,
    )
    leader_candidates: list[dict[str, Any]] = []
    for row in records:
        code = naked_code(row.get("code"))
        score = _number((row.get("leader_score_shadow") or {}).get("score"))
        if len(code) != 6 or not code.isdigit():
            continue
        if (row.get("leader_confirmed") is not True or score is None
                or score < ice_point_reversal.LEADER_SCORE_MIN):
            continue
        candidate = dict(row)
        candidate["ice_point_leader_candidate"] = True
        candidate["ice_point_leader_binding"] = {
            "code": code,
            "leader_score_shadow": score,
            "leader_score_threshold": ice_point_reversal.LEADER_SCORE_MIN,
            "leader_confirmed": True,
            "confirmation_source": row.get("event_source"),
            "confirmation_time": _time(row.get("first_seal_time") or row.get("first_seal")),
        }
        leader_candidates.append(candidate)
    leader_candidates.sort(
        key=lambda row: (
            -float((row.get("ice_point_leader_binding") or {}).get("leader_score_shadow") or 0),
            str(row.get("code") or ""),
        )
    )
    market["leader_confirm"] = bool(leader_candidates)
    market["tradeable_leader_bindings"] = [
        dict(row["ice_point_leader_binding"]) for row in leader_candidates
    ]
    rank_records = _rank_universe(
        asof=asof, codes=codes, records=records, candidates=candidate_by_code,
        auction=auction_by_code, bars=bars,
    )
    strategy_records = {
        "rank_surprise": rank_records,
        "divergence_reseal": [row for row in records if row.get("event_source") == "eastmoney_zt_pool"],
        "assist_arbitrage": [row for row in records if _number(row.get("board_height")) is not None],
        "preleader_arbitrage": [row for row in records if row.get("event_source") == "eastmoney_zt_pool"],
        "reverse_volume": [row for row in records if row.get("code") in tracked],
        "ice_point_reversal": leader_candidates,
    }
    coverage = _coverage(
        strategy_records, market,
        pretable_ready=preleader_pretable is not None and preleader_pretable_status == "ok",
        source_records=records,
    )
    qualification = _qualification(coverage, market)
    classes = {item["class"] for item in qualification.values()}
    evidence_class = next(iter(classes)) if len(classes) == 1 else "mixed"
    return {
        "schema": SCHEMA,
        "asof": asof,
        "canonical_forward": all(
            item["canonical_forward_eligible"] for item in qualification.values()
        ),
        "exploratory_reconstruction": any(
            item["class"] == EXPLORATORY_CLASS for item in qualification.values()
        ),
        "evidence_class": evidence_class,
        "evidence_qualification": qualification,
        "cohort_policy": "official_limitup+auction_shortlist+tracked_leaders",
        "cohort_count": len(codes),
        "cohort_budget": int(max_codes),
        "cohort_codes": codes,
        "records": records,
        "strategy_records": strategy_records,
        "market_state": market,
        "preleader_pretable": dict(preleader_pretable) if preleader_pretable is not None else None,
        "preleader_pretable_status": preleader_pretable_status,
        "coverage": coverage,
        "research_only": True,
        "execution_eligible": False,
        "live_order_sent": False,
    }


__all__ = [
    "SCHEMA", "DEFAULT_MAX_CODES", "EvidenceBudgetExceeded", "build_evidence",
    "naked_code", "rank_surprise_targets", "select_cohort",
]
