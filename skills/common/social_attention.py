"""Normalized A-share social attention evidence and bounded scoring overlays."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from data_access_config import social_attention_settings
from paths import cache_dir
from sector_taxonomy import is_broad_sector_label, resolve_sector
from state_store import atomic_write_json, read_json


SCHEMA = "social_attention_snapshot_v1"
CACHE_SCHEMA = "social_attention_cache_v1"
SOURCE_LIMITS = {
    "eastmoney": 100,
    "eastmoney_rising": 100,
    "xueqiu_discussion": 200,
    "xueqiu_follow": 200,
    "baidu": 12,
}


def _code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text.startswith(("SH", "SZ")):
        text = text[2:]
    return text.zfill(6)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _rank_score(rank: Any, source: str) -> float:
    value = max(1, int(_number(rank, 1)))
    limit = SOURCE_LIMITS[source]
    return round(max(0.0, 100.0 * (limit - value + 1) / limit), 2)


def _provider_family(source: str) -> str:
    if source.startswith("xueqiu_"):
        return "xueqiu"
    if source.startswith("eastmoney"):
        return "eastmoney"
    return source


def _metadata_for(
    code: str,
    stock_metadata: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    return (
        stock_metadata.get(code)
        or stock_metadata.get(f"SH{code}")
        or stock_metadata.get(f"SZ{code}")
        or {}
    )


def _social_payload(context: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(context, Mapping):
        return {}
    if context.get("schema") == SCHEMA:
        return context
    payload = context.get("social_attention")
    return payload if isinstance(payload, Mapping) else {}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def build_social_attention_snapshot(
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    trading_date: str,
    captured_at: str | None = None,
    source_health: Mapping[str, Mapping[str, Any]] | None = None,
    stock_metadata: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Merge independent rankings into an auditable stock/theme snapshot."""
    settings = social_attention_settings()
    metadata = stock_metadata or {}
    by_code: dict[str, dict[str, Any]] = {}
    provider_presence: set[str] = set()

    for source, rows in rankings.items():
        if source not in SOURCE_LIMITS:
            continue
        if rows:
            provider_presence.add(_provider_family(source))
        for row in rows:
            code = _code(row.get("code"))
            if not code.strip("0"):
                continue
            record = by_code.setdefault(
                code,
                {
                    "code": code,
                    "name": None,
                    "source_scores": {},
                    "source_details": {},
                    "rank_change": None,
                    "price_change_pct": None,
                },
            )
            record["name"] = row.get("name") or record["name"]
            score = _rank_score(row.get("rank"), source)
            record["source_scores"][source] = score
            record["source_details"][source] = dict(row)
            if source.startswith("eastmoney"):
                change = _number(row.get("rank_change"))
                record["rank_change"] = (
                    change
                    if record.get("rank_change") is None
                    else max(_number(record["rank_change"]), change)
                )
            if row.get("price_change_pct") is not None:
                record["price_change_pct"] = _number(row.get("price_change_pct"))

    stocks: dict[str, dict[str, Any]] = {}
    min_sources = int(settings["min_sources_for_boost"])
    for code, raw in by_code.items():
        source_scores = dict(raw["source_scores"])
        provider_scores: dict[str, float] = {}
        eastmoney_scores = [
            source_scores[key]
            for key in ("eastmoney", "eastmoney_rising")
            if key in source_scores
        ]
        if eastmoney_scores:
            provider_scores["eastmoney"] = max(eastmoney_scores)
        xueqiu_scores = [
            source_scores[key]
            for key in ("xueqiu_discussion", "xueqiu_follow")
            if key in source_scores
        ]
        if xueqiu_scores:
            provider_scores["xueqiu"] = sum(xueqiu_scores) / len(xueqiu_scores)
        if "baidu" in source_scores:
            provider_scores["baidu"] = source_scores["baidu"]

        scores = list(provider_scores.values())
        attention_score = sum(scores) / len(scores) if scores else 0.0
        disagreement = max(scores) - min(scores) if len(scores) > 1 else 0.0
        source_count = len(provider_scores)
        velocity = max(-100.0, min(100.0, _number(raw["rank_change"]) * 5.0))
        crowding = (
            "high"
            if source_count >= min_sources and attention_score >= 85
            else "medium"
            if attention_score >= 70
            else "low"
        )
        meta = _metadata_for(code, metadata)
        sector, sector_source = resolve_sector(meta)
        industry = str(meta.get("industry") or "").strip() or None
        stocks[code] = {
            "code": code,
            "name": raw["name"] or meta.get("name") or code,
            "sector": sector or None,
            "sector_source": sector_source,
            "industry": industry,
            "industry_source": meta.get("industry_source"),
            "attention_score": round(attention_score, 2),
            "attention_velocity": round(velocity, 2),
            "cross_source_count": source_count,
            "eligible_for_boost": source_count >= min_sources,
            "crowding_risk": crowding,
            "source_disagreement": round(disagreement, 2),
            "source_disagreement_high": disagreement > 35,
            "price_change_pct": raw["price_change_pct"],
            "provider_scores": {
                key: round(value, 2) for key, value in provider_scores.items()
            },
            "source_details": raw["source_details"],
        }

    ordered = sorted(
        stocks.values(),
        key=lambda item: (
            not item["eligible_for_boost"],
            -item["attention_score"],
            item["code"],
        ),
    )
    top_limit = int(settings["top_limit"])
    stocks = {item["code"]: item for item in ordered[:top_limit]}

    themes: dict[str, dict[str, Any]] = {}
    for item in stocks.values():
        sector = item.get("sector")
        if not sector or is_broad_sector_label(sector):
            continue
        theme = themes.setdefault(
            str(sector),
            {"scores": [], "leaders": [], "confirmed": 0},
        )
        theme["scores"].append(item["attention_score"])
        theme["leaders"].append(
            {
                "code": item["code"],
                "name": item["name"],
                "attention_score": item["attention_score"],
            }
        )
        if item["eligible_for_boost"]:
            theme["confirmed"] += 1
    normalized_themes = {}
    min_theme_confirmed = int(settings["theme_min_confirmed_stocks"])
    min_theme_score = float(settings["theme_min_attention_score"])
    for name, theme in themes.items():
        leaders = sorted(
            theme["leaders"],
            key=lambda item: (-item["attention_score"], item["code"]),
        )[:5]
        attention_score = round(
            sum(theme["scores"]) / len(theme["scores"]),
            2,
        )
        confirmed_stock_count = theme["confirmed"]
        normalized_themes[name] = {
            "stock_count": len(theme["scores"]),
            "confirmed_stock_count": confirmed_stock_count,
            "attention_score": attention_score,
            "confirmed": bool(
                confirmed_stock_count >= min_theme_confirmed
                and attention_score >= min_theme_score
            ),
            "leaders": leaders,
        }

    status = (
        "ready"
        if len(provider_presence) >= min_sources
        else "partial"
        if provider_presence
        else "blocked"
    )
    return {
        "schema": SCHEMA,
        "status": status,
        "trading_date": trading_date,
        "captured_at": captured_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_health": dict(source_health or {}),
        "available_sources": sorted(provider_presence),
        "source_versions": {
            {
                "eastmoney": "eastmoney_attention",
                "xueqiu": "xueqiu_attention",
                "baidu": "baidu_attention",
            }[source]: {
                "eastmoney": "eastmoney-attention-v1",
                "xueqiu": "xueqiu-attention-v1",
                "baidu": "baidu-attention-v1",
            }[source]
            for source in sorted(provider_presence)
        },
        "stock_count": len(stocks),
        "theme_count": len(normalized_themes),
        "stocks": stocks,
        "themes": normalized_themes,
        "policy": {
            "min_sources_for_boost": min_sources,
            "candidate_bonus_max": settings["candidate_bonus_max"],
            "sentiment_delta_max": settings["sentiment_delta_max"],
            "single_source_behavior": "display_only",
        },
    }


def _attention_record(
    code: str,
    context: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    if not context:
        return None
    payload = context.get("social_attention")
    if not isinstance(payload, Mapping):
        return None
    stocks = payload.get("stocks")
    if not isinstance(stocks, Mapping):
        return None
    record = stocks.get(_code(code))
    return record if isinstance(record, Mapping) else None


def candidate_attention_overlay(
    code: str,
    context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return a bounded discovery overlay; attention never creates a buy signal."""
    record = _attention_record(code, context)
    if not record:
        return {
            "delta": 0.0,
            "notes": [],
            "display_only": False,
            "record": None,
        }
    if not record.get("eligible_for_boost"):
        return {
            "delta": 0.0,
            "notes": ["社会关注仅单源，展示不计分"],
            "display_only": True,
            "record": dict(record),
        }

    settings = social_attention_settings()
    score = _number(record.get("attention_score"))
    velocity = _number(record.get("attention_velocity"))
    price_change = record.get("price_change_pct")
    if (
        record.get("crowding_risk") == "high"
        and isinstance(price_change, (int, float))
        and price_change < 0
    ):
        return {
            "delta": -2.0,
            "notes": ["社会关注高位但价格走弱，出现拥挤背离"],
            "display_only": False,
            "record": dict(record),
        }

    delta = 0.0
    notes = []
    if score >= 70:
        delta += 1.5
        notes.append(f"社会关注双源确认({score:.0f})")
        if velocity >= 10:
            delta += 1.5
            notes.append(f"关注升温({velocity:+.0f})")
    elif score >= 60:
        delta += 1.0
        notes.append(f"社会关注活跃({score:.0f})")
    limit = float(settings["candidate_bonus_max"])
    return {
        "delta": round(max(-limit, min(limit, delta)), 2),
        "notes": notes,
        "display_only": False,
        "record": dict(record),
    }


def _empty_theme_evidence() -> dict[str, Any]:
    return {
        "available": False,
        "confirmed": False,
        "attention_score": 0.0,
        "confirmed_stock_count": 0,
        "stock_count": 0,
        "leaders": [],
        "alignment": None,
    }


def _exact_theme_evidence(name: str, record: Mapping[str, Any]) -> dict[str, Any]:
    settings = social_attention_settings()
    score = _number(record.get("attention_score"))
    confirmed_stock_count = _int(record.get("confirmed_stock_count"))
    stock_count = _int(record.get("stock_count"))
    confirmed = bool(record.get("confirmed"))
    if not confirmed:
        confirmed = (
            confirmed_stock_count >= int(settings["theme_min_confirmed_stocks"])
            and score >= float(settings["theme_min_attention_score"])
        )
    leaders = record.get("leaders") if isinstance(record.get("leaders"), list) else []
    return {
        "available": True,
        "confirmed": confirmed,
        "attention_score": round(score, 2),
        "confirmed_stock_count": confirmed_stock_count,
        "stock_count": stock_count,
        "leaders": leaders[:5],
        "alignment": {
            "method": "exact_sector_name",
            "target_sector": name,
            "source_sectors": [
                {
                    "sector": name,
                    "sector_sources": ["social_attention.themes"],
                    "stock_count": stock_count,
                    "matched_stock_codes": [],
                }
            ],
            "matched_stock_codes": [],
            "reason": "social theme name exactly matches the mainline sector",
        },
    }


def _theme_leaders(matched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (
            {
                "code": stock["code"],
                "name": stock.get("name") or stock["code"],
                "attention_score": round(_number(stock.get("attention_score")), 2),
                "original_sector": stock["source_sector"],
                "original_sector_source": stock.get("sector_source"),
                "industry": stock.get("industry"),
                "industry_source": stock.get("industry_source"),
            }
            for stock in matched
        ),
        key=lambda item: (-item["attention_score"], item["code"]),
    )[:5]


def _theme_source_sectors(matched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for stock in matched:
        source_sector = stock["source_sector"]
        group = grouped.setdefault(
            source_sector,
            {"sector_sources": set(), "matched_stock_codes": []},
        )
        source = str(stock.get("sector_source") or stock.get("industry_source") or "unknown")
        group["sector_sources"].add(source)
        group["matched_stock_codes"].append(stock["code"])
    return [
        {
            "sector": source_sector,
            "sector_sources": sorted(group["sector_sources"]),
            "stock_count": len(group["matched_stock_codes"]),
            "matched_stock_codes": sorted(group["matched_stock_codes"]),
        }
        for source_sector, group in sorted(grouped.items())
    ]


def _projected_theme_evidence(
    name: str,
    payload: Mapping[str, Any] | None,
    member_codes: Sequence[Any] | None,
) -> dict[str, Any]:
    member_set = {_code(code) for code in member_codes or [] if _code(code).strip("0")}
    stocks = payload.get("stocks") if isinstance(payload, Mapping) else None
    matched: list[dict[str, Any]] = []
    if isinstance(stocks, Mapping):
        for code in sorted(member_set):
            stock = stocks.get(code)
            if not isinstance(stock, Mapping):
                continue
            source_sector = str(stock.get("sector") or stock.get("industry") or "").strip()
            if not source_sector or is_broad_sector_label(source_sector):
                continue
            matched.append({"code": code, **dict(stock), "source_sector": source_sector})

    if not matched:
        return _empty_theme_evidence()

    score = round(sum(_number(stock.get("attention_score")) for stock in matched) / len(matched), 2)
    confirmed_stock_count = sum(bool(stock.get("eligible_for_boost")) for stock in matched)
    settings = social_attention_settings()
    confirmed = bool(
        confirmed_stock_count >= int(settings["theme_min_confirmed_stocks"])
        and score >= float(settings["theme_min_attention_score"])
    )
    return {
        "available": True,
        "confirmed": confirmed,
        "attention_score": score,
        "confirmed_stock_count": confirmed_stock_count,
        "stock_count": len(matched),
        "leaders": _theme_leaders(matched),
        "alignment": {
            "method": "stock_membership_projection",
            "target_sector": name,
            "source_sectors": _theme_source_sectors(matched),
            "matched_stock_codes": sorted(stock["code"] for stock in matched),
            "reason": (
                "social-attention stocks were re-aggregated by their same-run "
                "mainline sector membership; no global sector alias was inferred"
            ),
        },
    }


def theme_attention_evidence(
    sector: Any,
    context: Mapping[str, Any] | None,
    *,
    member_codes: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Return same-day social evidence aligned to a tradable narrow sector.

    Provider industry names are not a shared taxonomy: the mainline feed may
    call a stock ``医疗服务`` while the social metadata calls the same stock
    ``医疗器械``.  Exact theme-name matches still win.  When no exact theme
    exists, callers may supply the target sector's same-run member codes; the
    social stock records are then re-aggregated over that identity set.

    This is deliberately a local evidence projection, not a global alias.  It
    never asserts that every ``医疗器械`` stock belongs to ``医疗服务``, and it
    rejects broad exchange industries before projecting them into a narrow
    target.  The returned alignment block keeps the original labels, sources,
    matched codes, and reason auditable.
    """
    name = str(sector or "").strip()
    if not name or is_broad_sector_label(name):
        return _empty_theme_evidence()
    payload = _social_payload(context)
    themes = payload.get("themes") if isinstance(payload, Mapping) else None
    record = themes.get(name) if isinstance(themes, Mapping) else None
    if isinstance(record, Mapping):
        return _exact_theme_evidence(name, record)
    return _projected_theme_evidence(name, payload, member_codes)


def sentiment_attention_overlay(
    code: str,
    context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return a weak sentiment adjustment; hard policy gates always outrank it."""
    record = _attention_record(code, context)
    if not record:
        return {"delta": 0.0, "notes": [], "record": None}
    if not record.get("eligible_for_boost"):
        return {
            "delta": 0.0,
            "notes": ["社会关注单源数据仅展示"],
            "record": dict(record),
        }

    settings = social_attention_settings()
    limit = float(settings["sentiment_delta_max"])
    score = _number(record.get("attention_score"))
    velocity = _number(record.get("attention_velocity"))
    price_change = record.get("price_change_pct")
    if (
        record.get("crowding_risk") == "high"
        and isinstance(price_change, (int, float))
        and price_change < 0
    ):
        return {
            "delta": -limit,
            "notes": ["社会关注拥挤与价格走势背离"],
            "record": dict(record),
        }
    if score >= 70 and velocity > 0 and (
        price_change is None or _number(price_change) >= 0
    ):
        return {
            "delta": min(limit, 0.5),
            "notes": ["社会关注双源共振且持续升温"],
            "record": dict(record),
        }
    if score >= 60 and (price_change is None or _number(price_change) >= 0):
        return {
            "delta": min(limit, 0.3),
            "notes": ["社会关注活跃，作为弱确认"],
            "record": dict(record),
        }
    return {"delta": 0.0, "notes": [], "record": dict(record)}


def cache_file() -> str:
    return os.path.join(cache_dir("stock-triage"), "social_attention.json")


def write_social_attention_cache(
    payload: Mapping[str, Any],
    snapshot_ref: Mapping[str, Any],
) -> dict[str, Any]:
    record = {
        "schema": CACHE_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "payload": dict(payload),
        "snapshot_ref": dict(snapshot_ref),
    }
    atomic_write_json(cache_file(), record)
    return record


def read_social_attention_cache(
    *,
    max_age_hours: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    record = read_json(cache_file(), None)
    if not isinstance(record, dict) or record.get("schema") != CACHE_SCHEMA:
        return None
    try:
        generated_at = datetime.fromisoformat(str(record["generated_at"]))
    except (KeyError, TypeError, ValueError):
        return None
    reference = now or datetime.now(timezone.utc)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=reference.tzinfo)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=generated_at.tzinfo)
    max_age = (
        float(max_age_hours)
        if max_age_hours is not None
        else float(social_attention_settings()["cache_max_age_hours"])
    )
    age_seconds = (reference - generated_at).total_seconds()
    if age_seconds < 0 or age_seconds > max_age * 3600:
        return None
    return record
