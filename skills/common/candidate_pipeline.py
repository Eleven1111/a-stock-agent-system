"""Pure candidate filtering and ranking for the A-share selection pipeline."""

from __future__ import annotations

from datetime import date, datetime
from statistics import pstdev
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple


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
        if listed_days is not None and listed_days < min_listed_days:
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


def compute_price_features(kline: Sequence[Mapping[str, Any]]) -> Dict[str, float]:
    if len(kline) < 20:
        return {
            "momentum_5d": 0.0,
            "momentum_20d": 0.0,
            "momentum_60d": 0.0,
            "volume_ratio_5d": 0.0,
            "above_ma20": 0.0,
            "above_ma60": 0.0,
            "breakout_20d": 0.0,
            "volatility_20d": 100.0,
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
    return {
        "momentum_5d": round(_returns(closes, 5), 4),
        "momentum_20d": round(_returns(closes, 20), 4),
        "momentum_60d": round(_returns(closes, min(60, len(closes) - 1)), 4),
        "volume_ratio_5d": round(volumes[-1] / avg_volume, 4) if avg_volume > 0 else 0.0,
        "above_ma20": 1.0 if closes[-1] > ma20 else 0.0,
        "above_ma60": 1.0 if closes[-1] > ma60 else 0.0,
        "breakout_20d": 1.0 if closes[-1] >= prior_high else 0.0,
        "volatility_20d": round(pstdev(daily_returns) * 100, 4) if daily_returns else 100.0,
    }


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


def rank_candidates(
    eligible: Sequence[Mapping[str, Any]],
    kline_by_code: Mapping[str, Sequence[Mapping[str, Any]]],
) -> List[Dict[str, Any]]:
    """Produce separate cross-sectional ranks for limit-up and trend strategies."""
    enriched: List[Dict[str, Any]] = []
    for raw in eligible:
        item = dict(raw)
        code = naked_code(item.get("code"))
        bars = list(kline_by_code.get(code, []))
        features = compute_price_features(bars)
        item.update(features)
        item.update({
            "code": code,
            "market_code": market_code(code),
            "kline_days": len(bars),
            "feature_ready": len(bars) >= 20,
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
        if not item["feature_ready"]:
            trend_score = 0.0
        item.update({
            "daban_eligible": daban_eligible,
            "daban_score": round(max(0.0, min(100.0, daban_score)), 2),
            "trend_score": round(max(0.0, min(100.0, trend_score)), 2),
        })

    daban_order = sorted(enriched, key=lambda row: (-row["daban_score"], row["code"]))
    trend_order = sorted(enriched, key=lambda row: (-row["trend_score"], row["code"]))
    daban_rank = {item["code"]: index + 1 for index, item in enumerate(daban_order)}
    trend_rank = {item["code"]: index + 1 for index, item in enumerate(trend_order)}
    for item in enriched:
        item["daban_rank"] = daban_rank[item["code"]]
        item["trend_rank"] = trend_rank[item["code"]]
    return enriched


def build_watch_pool(
    quotes: Sequence[Mapping[str, Any]],
    kline_by_code: Mapping[str, Sequence[Mapping[str, Any]]],
    watch_limit: int = 200,
    min_amount: float = 80_000_000,
    min_price: float = 2.0,
    min_listed_days: int = 60,
) -> Dict[str, Any]:
    eligible, rejected = filter_universe(
        quotes,
        min_amount=min_amount,
        min_price=min_price,
        min_listed_days=min_listed_days,
    )
    ranked = rank_candidates(eligible, kline_by_code)
    selectable = [item for item in ranked if item["feature_ready"]]
    daban_order = sorted(
        selectable,
        key=lambda row: (not row["daban_eligible"], -row["daban_score"], row["code"]),
    )
    trend_order = sorted(selectable, key=lambda row: (-row["trend_score"], row["code"]))
    daban_quota = watch_limit // 2
    trend_quota = watch_limit - daban_quota
    daban_codes = {item["code"] for item in daban_order[:daban_quota] if item["daban_eligible"]}
    trend_codes = {item["code"] for item in trend_order[:trend_quota]}
    selected_codes = daban_codes | trend_codes
    if len(selected_codes) < watch_limit:
        fill_order = sorted(
            selectable,
            key=lambda row: (-max(row["daban_score"], row["trend_score"]), row["code"]),
        )
        for item in fill_order:
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
        candidates.append(selected)
    candidates.sort(
        key=lambda row: (
            -max(row["daban_score"], row["trend_score"]),
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
        "candidates": candidates[:watch_limit],
        "evaluated_candidates": ranked,
    }


def _factor_map(factors: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {naked_code(item.get("code")): dict(item) for item in factors if item.get("code")}


def rank_auction_shortlist(
    pool: Mapping[str, Any],
    factors: Sequence[Mapping[str, Any]],
    limit: int = 20,
) -> Dict[str, Any]:
    """Combine prior strategy ranks with 09:25 auction microstructure."""
    factor_by_code = _factor_map(factors)
    rows: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for raw in pool.get("candidates", []):
        item = dict(raw)
        code = naked_code(item.get("code") or item.get("market_code"))
        factor = factor_by_code.get(code)
        reasons: List[str] = []
        if not factor:
            reasons.append("缺少09:25竞价因子")
        elif factor.get("error"):
            reasons.append(str(factor["error"]))
        elif factor.get("is_yiziban"):
            reasons.append("一字板不可成交")
        if reasons:
            rejected.append({
                **item,
                "code": market_code(code),
                "rejection_reasons": reasons,
            })
            continue
        rows.append({**item, **factor, "code": market_code(code)})

    amount_p = _percentiles(rows, "auction_amount")
    bid_p = _percentiles(rows, "auction_bid_ask_ratio")
    delta_p = _percentiles(rows, "auction_net_bid_delta")
    for item in rows:
        code = naked_code(item["code"])
        gap = _num(item.get("auction_gap_pct"))
        gap_quality = max(0.0, 1.0 - abs(gap - 2.0) / 7.0)
        prior_daban = _num(item.get("daban_score")) / 100
        prior_trend = _num(item.get("trend_score")) / 100
        shared_score = (
            0.20 * gap_quality
            + 0.15 * amount_p.get(code, 0.0)
            + 0.10 * bid_p.get(code, 0.0)
            + 0.05 * delta_p.get(code, 0.0)
        )
        item["auction_daban_score"] = round(100 * (0.50 * prior_daban + shared_score), 2)
        item["auction_trend_score"] = round(100 * (0.50 * prior_trend + shared_score), 2)
        item["auction_score"] = max(
            item["auction_daban_score"],
            item["auction_trend_score"],
        )

    def _lane_member(item: Mapping[str, Any], lane: str) -> bool:
        selected_by = item.get("selected_by")
        if isinstance(selected_by, Mapping):
            return bool(selected_by.get(lane))
        if lane == "daban":
            return bool(
                item.get("daban_eligible", is_main_board_10cm(item.get("code"), item.get("name", "")))
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
            chosen = dict(item)
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
            rejected.append({**item, "rejection_reasons": [f"竞价分策略排名未进入前{limit}"]})
    return {
        "schema": "auction_shortlist_v1",
        "source_asof": pool.get("asof"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_count": len(pool.get("candidates", [])),
        "shortlist_count": len(shortlist),
        "shortlist": shortlist,
        "rejected": rejected,
    }
