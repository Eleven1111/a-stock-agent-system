"""Deterministic research-only tail-close strategy primitives.

The module owns strategy semantics, not runtime governance.  Callers must still
route every result through the shared snapshot, policy, registry, ledger,
validation, and portfolio layers.  No function in this module can place an
order or import a broker client.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from a_share_rules import add_trading_days
from config_registry import load_registered
from execution_model import estimate_round_trip_pnl, estimate_trade_cost


INPUT_SCHEMA = "tail_close_input_v1"
PREPARED_SCHEMA = "tail_close_prepared_v1"
DECISION_SCHEMA = "tail_close_signal_v1"
FILL_SCHEMA = "tail_close_simulated_fill_v1"
OUTCOME_SCHEMA = "tail_close_outcome_v1"
AFTER_HOURS_SCHEMA = "tail_close_after_hours_shadow_v1"
KILL_SWITCH_SCHEMA = "tail_close_kill_switch_v1"

PRIMARY_STRATEGY_ID = "tail_close:mainline_continuation_v1"
AFTER_HOURS_STRATEGY_ID = "tail_close:after_hours_fixed_v1"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class TailCloseContractError(ValueError):
    """Raised when a strategy input cannot support a point-in-time decision."""


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_tail_config(path: str | Path | None = None) -> dict[str, Any]:
    config = load_registered("tail_close_strategy", path=path)
    required = {
        "schema",
        "version",
        "strategies",
        "runtime",
        "universe",
        "market_gate",
        "sector_gate",
        "stock_gate",
        "ranking",
        "execution",
        "exit",
        "portfolio",
        "validation",
        "safety",
    }
    missing = sorted(required - set(config))
    if missing:
        raise TailCloseContractError(f"config_missing_roots:{','.join(missing)}")
    safety = config.get("safety") or {}
    if (
        safety.get("research_only") is not True
        or float(safety.get("live_weight", -1)) != 0
        or safety.get("broker_access") != "forbidden"
        or safety.get("automatic_ordering") != "forbidden"
    ):
        raise TailCloseContractError("unsafe_strategy_config")
    return config


def _as_float(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _as_datetime(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise TailCloseContractError(f"{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TailCloseContractError(f"{field}_timezone_missing")
    return parsed.astimezone(SHANGHAI_TZ)


def _clock(trading_date: str, value: str) -> datetime:
    return datetime.combine(
        date.fromisoformat(trading_date),
        time.fromisoformat(value),
        tzinfo=SHANGHAI_TZ,
    )


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip()
    return len(text) == 64 and all(
        character in "0123456789abcdefABCDEF" for character in text
    )


def _pit_market_records(bundle: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    market = bundle.get("market")
    if isinstance(market, Mapping):
        yield market
    for collection in ("sectors", "stocks"):
        for item in bundle.get(collection) or []:
            if not isinstance(item, Mapping):
                continue
            yield item
            for child_collection in ("minute_rows", "bars", "members", "records"):
                for child in item.get(child_collection) or []:
                    if isinstance(child, Mapping):
                        yield child


def _pit_current_records(bundle: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    market = bundle.get("market")
    if isinstance(market, Mapping):
        yield market
    for collection in ("sectors", "stocks"):
        for item in bundle.get(collection) or []:
            if isinstance(item, Mapping):
                yield item


def _runtime_identity(
    payload: Mapping[str, Any],
    *,
    prefix: str = "",
) -> dict[str, str]:
    identity: dict[str, str] = {}
    for field in ("run_id", "batch_id", "session_id"):
        value = str(payload.get(field) or "").strip()
        if not value:
            raise TailCloseContractError(f"{prefix}{field}_missing")
        identity[field] = value
    return identity


def validate_prepared_state(
    prepared_state: Mapping[str, Any] | None,
    bundle: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    """Bind a decision input to one successful, immutable prepare run."""
    decision_identity = _runtime_identity(bundle, prefix="decision_")
    if not isinstance(prepared_state, Mapping):
        raise TailCloseContractError("prepared_state_missing")
    if prepared_state.get("schema") != PREPARED_SCHEMA:
        raise TailCloseContractError("prepared_state_invalid")
    if prepared_state.get("status") not in {"ready", "no_signal"}:
        raise TailCloseContractError("prepared_state_not_ready")
    if prepared_state.get("config_hash") != canonical_hash(config):
        raise TailCloseContractError("prepared_config_mismatch")
    trading_date = str(bundle.get("trading_date") or "")
    if prepared_state.get("trading_date") != trading_date:
        raise TailCloseContractError("prepared_trading_date_mismatch")
    prepared_payload = {
        key: value
        for key, value in prepared_state.items()
        if key != "prepared_hash"
    }
    if prepared_state.get("prepared_hash") != canonical_hash(prepared_payload):
        raise TailCloseContractError("prepared_hash_mismatch")
    prepared_identity = _runtime_identity(prepared_state, prefix="prepared_")
    if prepared_identity["batch_id"] != decision_identity["batch_id"]:
        raise TailCloseContractError("prepared_batch_id_mismatch")
    if prepared_identity["session_id"] != decision_identity["session_id"]:
        raise TailCloseContractError("prepared_session_id_mismatch")
    if (
        str(bundle.get("prepare_run_id") or "").strip()
        != prepared_identity["run_id"]
    ):
        raise TailCloseContractError("prepared_run_id_mismatch")
    return {
        "decision": decision_identity,
        "prepared": prepared_identity,
    }


def validate_pit_bundle(
    bundle: Mapping[str, Any],
    *,
    cutoff_time: str,
    maximum_clock_offset_seconds: int,
    maximum_current_record_age_seconds: int,
) -> dict[str, Any]:
    """Validate event/availability time, watermarks, and clock state."""
    if bundle.get("schema") != INPUT_SCHEMA:
        raise TailCloseContractError("input_schema_invalid")
    trading_date = str(bundle.get("trading_date") or "")
    cutoff = _clock(trading_date, cutoff_time)
    for field in (
        "snapshot_id",
        "source_id",
        "source_version",
        "feature_version",
        "code_version",
    ):
        if not str(bundle.get(field) or "").strip():
            raise TailCloseContractError(f"{field}_missing")
    for field in ("snapshot_hash", "feature_hash", "config_hash"):
        if not _is_sha256(bundle.get(field)):
            raise TailCloseContractError(f"{field}_invalid")
    sealed_at = _as_datetime(bundle.get("snapshot_sealed_at"), "snapshot_sealed_at")
    if sealed_at > cutoff:
        raise TailCloseContractError("snapshot_sealed_after_cutoff")

    records = list(_pit_market_records(bundle))
    if not records:
        raise TailCloseContractError("pit_timestamps_missing")
    maximum_event = None
    maximum_available = None
    for record in records:
        if "event_time" not in record or "available_time" not in record:
            raise TailCloseContractError("pit_dual_time_incomplete")
        event = _as_datetime(record["event_time"], "event_time")
        available = _as_datetime(record["available_time"], "available_time")
        if available < event:
            raise TailCloseContractError("available_before_event")
        if event > cutoff:
            raise TailCloseContractError("event_after_cutoff")
        if available > cutoff:
            raise TailCloseContractError("available_after_cutoff")
        maximum_event = event if maximum_event is None else max(maximum_event, event)
        maximum_available = (
            available if maximum_available is None else max(maximum_available, available)
        )
    for collection in ("sectors", "stocks"):
        for item in bundle.get(collection) or []:
            if not isinstance(item, Mapping):
                continue
            for child_collection in ("minute_rows", "bars"):
                for child in item.get(child_collection) or []:
                    if not isinstance(child, Mapping):
                        raise TailCloseContractError("minute_row_invalid")
                    event = _as_datetime(child.get("event_time"), "event_time")
                    available = _as_datetime(
                        child.get("available_time"),
                        "available_time",
                    )
                    if (
                        event.date().isoformat() != trading_date
                        or available.date().isoformat() != trading_date
                    ):
                        raise TailCloseContractError(
                            "minute_row_trading_date_mismatch"
                        )
    freshness_floor = cutoff - timedelta(
        seconds=int(maximum_current_record_age_seconds)
    )
    for record in _pit_current_records(bundle):
        event = _as_datetime(record["event_time"], "event_time")
        available = _as_datetime(record["available_time"], "available_time")
        if event < freshness_floor or available < freshness_floor:
            raise TailCloseContractError("current_record_stale")

    watermarks = bundle.get("source_watermarks")
    if not isinstance(watermarks, Mapping) or not watermarks:
        raise TailCloseContractError("source_watermarks_missing")
    for source, watermark in watermarks.items():
        if not isinstance(watermark, Mapping):
            raise TailCloseContractError(f"watermark_invalid:{source}")
        if watermark.get("complete") is not True:
            raise TailCloseContractError(f"watermark_incomplete:{source}")
        coverage = _as_datetime(watermark.get("coverage_asof"), "coverage_asof")
        published = _as_datetime(
            watermark.get("provider_published_at"),
            "provider_published_at",
        )
        if coverage > cutoff or published > cutoff:
            raise TailCloseContractError(f"watermark_after_cutoff:{source}")
        if published < coverage:
            raise TailCloseContractError(f"watermark_publish_before_coverage:{source}")
        if maximum_event is not None and coverage < maximum_event:
            raise TailCloseContractError(f"watermark_coverage_incomplete:{source}")
        if coverage < freshness_floor:
            raise TailCloseContractError(f"watermark_stale:{source}")
    if maximum_available is not None and sealed_at < maximum_available:
        raise TailCloseContractError("snapshot_sealed_before_available_data")

    offset_value = _as_float(bundle.get("source_clock_offset_seconds"))
    if offset_value is None:
        raise TailCloseContractError("source_clock_drift_invalid")
    offset = abs(offset_value)
    if offset > int(maximum_clock_offset_seconds):
        raise TailCloseContractError("source_clock_drift")
    return {
        "schema": "tail_close_pit_validation_v1",
        "valid": True,
        "cutoff": cutoff.isoformat(),
        "max_event_time": maximum_event.isoformat() if maximum_event else None,
        "max_available_time": maximum_available.isoformat() if maximum_available else None,
        "source_watermarks": dict(watermarks),
        "source_clock_offset_seconds": offset,
        "snapshot_id": str(bundle["snapshot_id"]),
        "snapshot_hash": str(bundle["snapshot_hash"]).lower(),
        "feature_hash": str(bundle["feature_hash"]).lower(),
        "config_hash": str(bundle["config_hash"]).lower(),
        "code_version": str(bundle["code_version"]),
        "snapshot_sealed_at": sealed_at.isoformat(),
    }


def is_main_board_code(code: Any) -> bool:
    normalized = str(code or "").strip().lower()
    if normalized.startswith(("sh", "sz")):
        normalized = normalized[2:]
    return normalized.isdigit() and len(normalized) == 6 and normalized.startswith(
        ("000", "001", "002", "003", "600", "601", "603", "605")
    )


def _name_excluded(name: Any) -> bool:
    text = str(name or "").upper()
    return "ST" in text or "退" in text


def _universe_reasons(stock: Mapping[str, Any], config: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    if not is_main_board_code(stock.get("code")):
        reasons.append("not_main_board")
    if stock.get("is_st") is True or _name_excluded(stock.get("name")):
        reasons.append("risk_warning_security")
    listing_days = _as_float(stock.get("listing_days"))
    if listing_days is None:
        reasons.append("listing_days_missing")
    elif listing_days < float(
        config["universe"]["minimum_listing_trading_days"]
    ):
        reasons.append("listing_too_recent")
    median_amount = _as_float(stock.get("median_amount_20d"))
    if median_amount is None:
        reasons.append("median_amount_missing")
    elif median_amount < float(
        config["universe"]["minimum_median_turnover_20d_cny"]
    ):
        reasons.append("median_amount_low")
    pit_amount = _as_float(stock.get("amount"))
    if pit_amount is None:
        reasons.append("pit_amount_missing")
    elif pit_amount < float(
        config["universe"]["minimum_turnover_at_cutoff_cny"]
    ):
        reasons.append("pit_amount_low")
    if stock.get("tradeable") is not True:
        reasons.append("not_tradeable")
    if stock.get("event_gate_passed") is not True:
        reasons.append("event_gate_failed")
    return reasons


def build_prepared_state(
    bundle: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    if str(bundle.get("config_hash") or "").lower() != canonical_hash(config):
        raise TailCloseContractError("input_config_hash_mismatch")
    identity = _runtime_identity(bundle, prefix="prepare_")
    pit = validate_pit_bundle(
        bundle,
        cutoff_time=str(config["runtime"]["prepare_cutoff"]),
        maximum_clock_offset_seconds=int(
            config["runtime"]["maximum_clock_offset_seconds"]
        ),
        maximum_current_record_age_seconds=int(
            config["runtime"]["prepare_maximum_current_record_age_seconds"]
        ),
    )
    eligible: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for raw in bundle.get("stocks") or []:
        if not isinstance(raw, Mapping):
            continue
        stock = dict(raw)
        reasons = _universe_reasons(stock, config)
        item = {"code": str(stock.get("code") or "").zfill(6), "reasons": reasons}
        if reasons:
            rejected.append(item)
        else:
            eligible.append(
                {
                    "code": item["code"],
                    "name": stock.get("name"),
                    "sector_id": stock.get("sector_id"),
                    "listing_days": stock.get("listing_days"),
                    "median_amount_20d": stock.get("median_amount_20d"),
                }
            )
    content = {
        "schema": PREPARED_SCHEMA,
        "strategy_id": PRIMARY_STRATEGY_ID,
        "trading_date": bundle["trading_date"],
        "run_id": identity["run_id"],
        "batch_id": identity["batch_id"],
        "session_id": identity["session_id"],
        "pit_cutoff": pit["cutoff"],
        "input_hash": canonical_hash(bundle),
        "config_hash": canonical_hash(config),
        "eligible": eligible,
        "rejected": rejected,
        "status": "ready" if eligible else "no_signal",
        "research_only": True,
        "live_weight": 0.0,
    }
    content["prepared_hash"] = canonical_hash(content)
    return content


def _percentile_ranks(values: Mapping[str, float]) -> dict[str, float]:
    if not values:
        return {}
    unique = sorted(set(values.values()))
    if len(unique) == 1:
        return {key: 1.0 for key in values}
    percentile = {
        value: index / (len(unique) - 1)
        for index, value in enumerate(unique)
    }
    return {
        key: percentile[value]
        for key, value in values.items()
    }


def rank_sectors(
    sectors: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    metrics = {
        "session_relative_return": {},
        "tail_relative_return": {},
        "breadth": {},
        "persistence": {},
        "liquidity_support": {},
    }
    eligible: list[dict[str, Any]] = []
    for raw in sectors:
        sector = dict(raw)
        sector_id = str(sector.get("sector_id") or "")
        member_count = _as_float(sector.get("valid_member_count"))
        breadth = _as_float(sector.get("breadth"))
        required = {
            "session_relative_return": _as_float(
                sector.get("session_relative_return", sector.get("day_excess"))
            ),
            "tail_relative_return": _as_float(
                sector.get("tail_relative_return", sector.get("tail_excess"))
            ),
            "breadth": breadth,
            "persistence": _as_float(sector.get("persistence")),
            "liquidity_support": _as_float(sector.get("liquidity_support")),
        }
        if (
            not sector_id
            or member_count is None
            or member_count
            < int(config["sector_gate"]["minimum_valid_constituents"])
            or breadth is None
            or breadth < float(config["sector_gate"]["minimum_breadth"])
            or any(value is None for value in required.values())
        ):
            continue
        eligible.append(sector)
        for name, value in required.items():
            metrics[name][sector_id] = float(value)
    component_ranks = {name: _percentile_ranks(values) for name, values in metrics.items()}
    weights = config["sector_gate"]["weights"]
    ranked: list[dict[str, Any]] = []
    for sector in eligible:
        sector_id = str(sector["sector_id"])
        score = sum(
            float(weights[name]) * component_ranks[name][sector_id]
            for name in component_ranks
        )
        ranked.append(
            {
                **sector,
                "mainline_score": round(score, 8),
                "component_ranks": {
                    name: round(component_ranks[name][sector_id], 8)
                    for name in component_ranks
                },
            }
        )
    ranked.sort(
        key=lambda item: (
            -float(item["mainline_score"]),
            -int(item.get("valid_member_count") or 0),
            -float(item.get("pit_amount") or 0),
            str(item["sector_id"]),
        )
    )
    keep = max(
        1,
        math.ceil(
            len(ranked)
            * float(config["sector_gate"]["top_cross_section_fraction"])
        ),
    ) if ranked else 0
    for index, item in enumerate(ranked):
        item["mainline_qualified"] = index < keep
        item["mainline_rank"] = index + 1
    return ranked


def _minute_clock(row: Mapping[str, Any]) -> str:
    value = str(row.get("event_time") or row.get("time") or "")
    if "T" in value:
        try:
            return datetime.fromisoformat(value).strftime("%H%M")
        except ValueError:
            return ""
    return value.replace(":", "")[:4]


def _stock_gate_reasons(
    stock: Mapping[str, Any],
    config: Mapping[str, Any],
    qualified_sectors: set[str],
) -> list[str]:
    reasons = _universe_reasons(stock, config)
    sector_id = str(stock.get("sector_id") or "")
    if sector_id not in qualified_sectors:
        reasons.append("sector_not_mainline")
    fields = {
        name: _as_float(stock.get(name))
        for name in (
            "price",
            "open",
            "high",
            "low",
            "vwap",
            "ma5",
            "ma10",
            "ma20",
        )
    }
    if any(value is None for value in fields.values()):
        reasons.append("price_structure_missing")
        return list(dict.fromkeys(reasons))
    session_return = _as_float(
        stock.get("session_return", stock.get("day_change_pct"))
    )
    if session_return is None:
        reasons.append("session_return_missing")
        return list(dict.fromkeys(reasons))
    price = float(fields["price"])
    low = float(fields["low"])
    high = float(fields["high"])
    vwap = float(fields["vwap"])
    day_change = float(session_return)
    gate = config["stock_gate"]
    if not float(gate["minimum_session_return"]) <= day_change <= float(
        gate["maximum_session_return"]
    ):
        reasons.append("day_change_out_of_range")
    if price <= vwap:
        reasons.append("below_vwap")
    position = (price - low) / max(high - low, 1e-9)
    if position < float(gate["minimum_intraday_position"]):
        reasons.append("day_position_low")
    if not (
        float(fields["ma5"]) > float(fields["ma10"]) >= float(fields["ma20"])
        and price > float(fields["ma20"])
    ):
        reasons.append("trend_structure_failed")
    if price < float(fields["open"]):
        reasons.append("below_open")
    if stock.get("limit_up") is True or stock.get("sell_side_visible") is not True:
        reasons.append("not_buyable")
    if stock.get("near_limit_down") is True:
        reasons.append("near_limit_down")
    ask_price = _as_float(stock.get("ask_price", stock.get("best_ask")))
    if ask_price is None or ask_price <= 0:
        reasons.append("ask_price_missing")

    rows = [
        row
        for row in (stock.get("minute_rows") or [])
        if isinstance(row, Mapping) and "1430" <= _minute_clock(row) <= "1449"
    ]
    if len(rows) < int(gate["minimum_tail_minutes"]):
        reasons.append("tail_minutes_incomplete")
        return list(dict.fromkeys(reasons))
    prices = [_as_float(row.get("close", row.get("price"))) for row in rows]
    vwaps = [_as_float(row.get("vwap"), vwap) for row in rows]
    volumes = [_as_float(row.get("volume")) for row in rows]
    if any(value is None for value in prices):
        reasons.append("tail_price_missing")
        return list(dict.fromkeys(reasons))
    if any(value is None or value <= 0 for value in volumes):
        reasons.append("tail_volume_missing")
        return list(dict.fromkeys(reasons))
    above = sum(
        1
        for minute_price, minute_vwap in zip(prices, vwaps)
        if float(minute_price) > float(minute_vwap)
    ) / len(prices)
    if above < float(gate["minimum_minutes_above_vwap_ratio"]):
        reasons.append("tail_vwap_persistence_low")
    total_gain = max(0.0, float(prices[-1]) - float(prices[0]))
    positive_steps = [
        max(0.0, float(current) - float(previous))
        for previous, current in zip(prices, prices[1:])
    ]
    if total_gain > 0 and positive_steps:
        pulse_share = max(positive_steps) / total_gain
        if pulse_share > float(gate["maximum_single_minute_gain_share"]):
            reasons.append("single_minute_pulse")
    recent_start = float(prices[max(0, len(prices) - 10)])
    recent_change = float(prices[-1]) / recent_start - 1 if recent_start else -1
    if recent_change < float(gate["minimum_last10_return"]):
        reasons.append("late_selloff")
    median_volume = statistics.median(float(value) for value in volumes)
    recent_rows = list(zip(prices[-10:], volumes[-10:]))
    for (previous_price, _), (current_price, current_volume) in zip(
        recent_rows,
        recent_rows[1:],
    ):
        minute_return = float(current_price) / float(previous_price) - 1
        if (
            minute_return <= float(gate["maximum_single_minute_loss"])
            and float(current_volume)
            >= median_volume * float(gate["volume_selloff_multiple"])
        ):
            reasons.append("volume_selloff")
            break
    return list(dict.fromkeys(reasons))


def rank_stocks(
    stocks: Sequence[Mapping[str, Any]],
    ranked_sectors: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    qualified = {
        str(item["sector_id"])
        for item in ranked_sectors
        if item.get("mainline_qualified")
    }
    sector_score = {
        str(item["sector_id"]): float(item["mainline_score"])
        for item in ranked_sectors
    }
    eligible: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for raw in stocks:
        stock = dict(raw)
        code = str(stock.get("code") or "").zfill(6)
        reasons = _stock_gate_reasons(stock, config, qualified)
        if reasons:
            rejected.append({"code": code, "reasons": reasons})
            continue
        rows = [
            row
            for row in (stock.get("minute_rows") or [])
            if isinstance(row, Mapping) and "1430" <= _minute_clock(row) <= "1449"
        ]
        prices = [float(row.get("close", row.get("price"))) for row in rows]
        volumes = [float(row.get("volume") or 0) for row in rows]
        continuity = sum(
            1 for previous, current in zip(prices, prices[1:]) if current >= previous
        ) / max(1, len(prices) - 1)
        volume_quality = 1.0 - (
            max(volumes) / sum(volumes) if sum(volumes) > 0 else 1.0
        )
        execution_quality = min(
            1.0,
            float(stock.get("amount") or 0)
            / max(
                float(config["universe"]["minimum_turnover_at_cutoff_cny"]),
                1.0,
            ),
        )
        eligible.append(
            {
                **stock,
                "code": code,
                "_sector_strength": sector_score[str(stock["sector_id"])],
                "_leadership": float(stock.get("sector_relative_strength") or 0),
                "_continuity": continuity,
                "_volume_quality": volume_quality,
                "_execution_quality": execution_quality,
            }
        )
    component_fields = {
        "mainline_strength": "_sector_strength",
        "within_sector_leadership": "_leadership",
        "price_continuity": "_continuity",
        "volume_continuity_non_pulse": "_volume_quality",
        "execution_capacity": "_execution_quality",
    }
    components = {
        name: _percentile_ranks(
            {
                item["code"]: float(item[field])
                for item in eligible
            }
        )
        for name, field in component_fields.items()
    }
    weights = config["ranking"]["weights"]
    ranked: list[dict[str, Any]] = []
    for item in eligible:
        code = item["code"]
        score = sum(float(weights[name]) * components[name][code] for name in components)
        public = {key: value for key, value in item.items() if not key.startswith("_")}
        public["tail_rank_score"] = round(score, 8)
        public["rank_components"] = {
            name: round(components[name][code], 8) for name in components
        }
        ranked.append(public)
    ranked.sort(
        key=lambda item: (
            -float(item["tail_rank_score"]),
            -float(item.get("execution_quality") or item.get("amount") or 0),
            -float(item.get("amount") or 0),
            str(item["code"]),
        )
    )
    selected: list[dict[str, Any]] = []
    used_sectors: set[str] = set()
    for item in ranked:
        sector_id = str(item.get("sector_id") or "")
        if sector_id in used_sectors:
            continue
        selected.append(item)
        used_sectors.add(sector_id)
        if len(selected) >= int(config["ranking"]["maximum_research_signals"]):
            break
    return {
        "ranked_candidates": ranked[
            : int(config["ranking"]["maximum_research_candidates"])
        ],
        "selected": selected,
        "rejected": rejected,
    }


def build_research_decision(
    bundle: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    prepared_state: Mapping[str, Any] | None = None,
    emitted_at: str | None = None,
) -> dict[str, Any]:
    if str(bundle.get("config_hash") or "").lower() != canonical_hash(config):
        raise TailCloseContractError("input_config_hash_mismatch")
    identities = validate_prepared_state(prepared_state, bundle, config)
    decision_identity = identities["decision"]
    prepared_identity = identities["prepared"]
    trading_date = str(bundle["trading_date"])
    pit = validate_pit_bundle(
        bundle,
        cutoff_time=str(config["runtime"]["decision_cutoff"]),
        maximum_clock_offset_seconds=int(
            config["runtime"]["maximum_clock_offset_seconds"]
        ),
        maximum_current_record_age_seconds=int(
            config["runtime"]["decision_maximum_current_record_age_seconds"]
        ),
    )
    emitted = _as_datetime(
        emitted_at or datetime.now(SHANGHAI_TZ).isoformat(),
        "decision_emitted_at",
    )
    decision_at = _clock(trading_date, str(config["runtime"]["decision_at"]))
    deadline = _clock(trading_date, str(config["runtime"]["deadline"]))
    if emitted < decision_at:
        result = {
            "schema": DECISION_SCHEMA,
            "strategy_id": PRIMARY_STRATEGY_ID,
            "trading_date": trading_date,
            "status": "no_action_early",
            "signals": [],
            "reason": "decision_window_not_open",
            "decision_emitted_at": emitted.isoformat(),
            "pit": pit,
            "research_only": True,
            "live_weight": 0.0,
            "automatic_order_count": 0,
            "broker_call_count": 0,
        }
        result["decision_hash"] = canonical_hash(result)
        return result
    if emitted > deadline:
        result = {
            "schema": DECISION_SCHEMA,
            "strategy_id": PRIMARY_STRATEGY_ID,
            "trading_date": trading_date,
            "status": "no_action_late",
            "signals": [],
            "reason": "decision_sla_missed",
            "decision_emitted_at": emitted.isoformat(),
            "pit": pit,
            "research_only": True,
            "live_weight": 0.0,
            "automatic_order_count": 0,
            "broker_call_count": 0,
        }
        result["decision_hash"] = canonical_hash(result)
        return result
    market = bundle.get("market") or {}
    market_reasons: list[str] = []
    if market.get("context_status") not in {"ok", "ready"}:
        market_reasons.append("market_context_not_ready")
    if market.get("regime") in {"risk_off", "unknown", "stale"}:
        market_reasons.append(f"market_{market.get('regime')}")
    benchmark_tail = _as_float(market.get("benchmark_return_1400_to_cutoff"))
    breadth = _as_float(market.get("advancer_ratio"))
    if benchmark_tail is None or benchmark_tail < float(
        config["market_gate"]["minimum_benchmark_return_1400_to_cutoff"]
    ):
        market_reasons.append("benchmark_tail_weak")
    if breadth is None or breadth < float(
        config["market_gate"]["minimum_advancer_ratio"]
    ):
        market_reasons.append("market_breadth_weak")
    if market_reasons:
        result = {
            "schema": DECISION_SCHEMA,
            "strategy_id": PRIMARY_STRATEGY_ID,
            "trading_date": trading_date,
            "status": "no_action",
            "signals": [],
            "market_gate": {"allowed": False, "reasons": market_reasons},
            "pit": pit,
            "research_only": True,
            "live_weight": 0.0,
            "automatic_order_count": 0,
            "broker_call_count": 0,
        }
        result["decision_hash"] = canonical_hash(result)
        return result

    sectors = rank_sectors(bundle.get("sectors") or [], config)
    decision_stocks = list(bundle.get("stocks") or [])
    prepared_rejections: list[dict[str, Any]] = []
    allowed_codes = {
        str(item.get("code") or "").zfill(6)
        for item in prepared_state.get("eligible") or []
    }
    prepared_rejections = [
        {
            "code": str(item.get("code") or "").zfill(6),
            "reasons": ["prepare_gate_rejected", *(item.get("reasons") or [])],
        }
        for item in prepared_state.get("rejected") or []
    ]
    decision_stocks = [
        item
        for item in decision_stocks
        if str(item.get("code") or "").zfill(6) in allowed_codes
    ]
    stock_result = rank_stocks(decision_stocks, sectors, config)
    signals = []
    for index, item in enumerate(stock_result["selected"], start=1):
        signal_id = (
            f"tail-{trading_date.replace('-', '')}-{item['code']}-"
            f"{canonical_hash({'bundle': canonical_hash(bundle), 'config': canonical_hash(config)})[:10]}"
        )
        signals.append(
            {
                "signal_id": signal_id,
                "strategy_id": PRIMARY_STRATEGY_ID,
                "strategy_lane": "tail_close",
                "trading_date": trading_date,
                "rank": index,
                "code": item["code"],
                "name": item.get("name"),
                "sector_id": item.get("sector_id"),
                "score": item["tail_rank_score"],
                "priority": item["tail_rank_score"],
                "reference_price": item.get("price"),
                "sector": item.get("sector_id"),
                "requested_capacity": config["execution"][
                    "research_notional_per_signal_cny"
                ],
                "proposed_position_pct": (
                    float(
                        config["execution"]["research_notional_per_signal_cny"]
                    )
                    / float(
                        config["portfolio"][
                            "research_portfolio_notional_cny"
                        ]
                    )
                    * 100
                ),
                "requested_action": "buy",
                "decision_mode": str(bundle.get("decision_mode") or "replay"),
                "snapshot_id": bundle.get("snapshot_id"),
                "snapshot_hash": bundle.get("snapshot_hash") or canonical_hash(bundle),
                "config_hash": canonical_hash(config),
                "feature_hash": bundle.get("feature_hash"),
                "code_version": bundle.get("code_version"),
                "run_id": decision_identity["run_id"],
                "batch_id": decision_identity["batch_id"],
                "session_id": decision_identity["session_id"],
                "prepare_run_id": prepared_identity["run_id"],
                "provenance": {
                    "decision_mode": str(bundle.get("decision_mode") or "replay"),
                    "snapshot_id": bundle.get("snapshot_id"),
                    "snapshot_hash": bundle.get("snapshot_hash"),
                    "config_hash": canonical_hash(config),
                    "code_version": bundle.get("code_version"),
                    "run_id": decision_identity["run_id"],
                    "batch_id": decision_identity["batch_id"],
                    "session_id": decision_identity["session_id"],
                    "prepare_run_id": prepared_identity["run_id"],
                },
                "research_only": True,
                "live_weight": 0.0,
            }
        )
    result = {
        "schema": DECISION_SCHEMA,
        "strategy_id": PRIMARY_STRATEGY_ID,
        "trading_date": trading_date,
        "run_id": decision_identity["run_id"],
        "batch_id": decision_identity["batch_id"],
        "session_id": decision_identity["session_id"],
        "prepare_run_id": prepared_identity["run_id"],
        "status": "research_signal" if signals else "no_action",
        "decision_time": _clock(
            trading_date,
            str(config["runtime"]["decision_at"]),
        ).isoformat(),
        "decision_emitted_at": emitted.isoformat(),
        "pit": pit,
        "market_gate": {"allowed": True, "reasons": []},
        "sector_ranking": sectors,
        "ranked_candidates": stock_result["ranked_candidates"],
        "rejections": [*prepared_rejections, *stock_result["rejected"]],
        "signals": signals,
        "research_only": True,
        "live_weight": 0.0,
        "automatic_order_count": 0,
        "broker_call_count": 0,
    }
    result["decision_hash"] = canonical_hash(result)
    return result


def _bar_datetime(trading_date: str, row: Mapping[str, Any]) -> datetime:
    if "event_time" not in row:
        raise TailCloseContractError("fill_bar_event_time_missing")
    event = _as_datetime(row["event_time"], "bar_event_time")
    if event.date().isoformat() != trading_date:
        raise TailCloseContractError("fill_bar_trading_date_mismatch")
    return event


def simulate_continuous_fill(
    signal: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    decision_emitted_at: str,
    requested_notional: float | None = None,
) -> dict[str, Any]:
    if signal.get("strategy_id") != PRIMARY_STRATEGY_ID:
        raise TailCloseContractError("continuous_strategy_id_invalid")
    if signal.get("research_only") is not True or float(
        signal.get("live_weight") or 0
    ) != 0:
        raise TailCloseContractError("continuous_signal_not_research_only")
    if str(signal.get("config_hash") or "").lower() != canonical_hash(config):
        raise TailCloseContractError("continuous_signal_config_mismatch")
    trading_date = str(signal.get("trading_date") or "") or str(
        decision_emitted_at
    )[:10]
    emitted = _as_datetime(decision_emitted_at, "decision_emitted_at")
    latency = float(config["execution"]["observed_system_latency_seconds"]) + float(
        config["execution"]["manual_review_latency_seconds"]
    )
    arrival = emitted + timedelta(seconds=latency)
    cancel = _clock(trading_date, str(config["runtime"]["cancel"]))
    reference = float(signal.get("reference_price") or 0)
    if reference <= 0:
        raise TailCloseContractError("reference_price_invalid")
    limit_price = reference * (
        1 + float(config["execution"]["maximum_limit_premium"])
    )
    allocation = signal.get("portfolio_allocation")
    if not isinstance(allocation, Mapping):
        raise TailCloseContractError("portfolio_allocation_missing")
    allocated_capacity = _as_float(allocation.get("allocated_capacity"))
    if allocated_capacity is None or allocated_capacity <= 0:
        raise TailCloseContractError("portfolio_allocated_capacity_invalid")
    requested_value = _as_float(
        requested_notional
        if requested_notional is not None
        else signal.get(
            "requested_capacity",
            config["execution"]["research_notional_per_signal_cny"],
        )
    )
    if requested_value is None or requested_value <= 0:
        raise TailCloseContractError("requested_notional_invalid")
    notional = min(requested_value, allocated_capacity)
    requested_quantity = int(notional / reference / 100) * 100
    if requested_quantity <= 0:
        raise TailCloseContractError("requested_quantity_below_board_lot")
    participation = float(
        config["execution"]["maximum_visible_volume_participation"]
    )
    queue_discount = float(config["execution"]["queue_discount"])
    eligible = []
    for row in bars:
        if not isinstance(row, Mapping):
            raise TailCloseContractError("fill_bar_invalid")
        when = _bar_datetime(trading_date, row)
        if "available_time" not in row:
            raise TailCloseContractError("fill_bar_available_time_missing")
        available_at = _as_datetime(row["available_time"], "bar_available_time")
        if available_at.date().isoformat() != trading_date:
            raise TailCloseContractError("fill_bar_trading_date_mismatch")
        if available_at < when:
            raise TailCloseContractError("fill_bar_available_before_event")
        if not arrival <= when <= cancel:
            continue
        if available_at > cancel:
            continue
        price = _as_float(row.get("ask_price", row.get("price", row.get("close"))))
        if price is None or price <= 0 or price > limit_price:
            continue
        visible = _as_float(
            row.get("available_sell_volume", row.get("sell_volume", row.get("volume")))
        )
        if visible is None or visible <= 0:
            continue
        quantity = int(visible * participation * queue_discount / 100) * 100
        if quantity > 0:
            eligible.append((when, available_at, float(price), quantity))
    eligible.sort(key=lambda item: (item[0], item[1], item[2]))
    remaining = requested_quantity
    fills: list[dict[str, Any]] = []
    for when, available_at, price, quantity in eligible:
        filled = min(remaining, quantity)
        filled = int(filled / 100) * 100
        if filled <= 0:
            continue
        fills.append(
            {
                "event_time": when.isoformat(),
                "available_time": available_at.isoformat(),
                "price": price,
                "quantity": filled,
            }
        )
        remaining -= filled
        if remaining <= 0:
            break
    filled_quantity = requested_quantity - remaining
    fill_price = (
        sum(item["price"] * item["quantity"] for item in fills) / filled_quantity
        if filled_quantity
        else None
    )
    if filled_quantity == requested_quantity:
        status = "FULL_FILL"
    elif filled_quantity > 0:
        status = "PARTIAL_FILL"
    else:
        status = "UNFILLED"
    buy_cost = (
        estimate_trade_cost(
            "buy",
            float(fill_price) * filled_quantity,
            asof=trading_date,
        )
        if fill_price and filled_quantity
        else None
    )
    result = {
        "schema": FILL_SCHEMA,
        "strategy_id": PRIMARY_STRATEGY_ID,
        "signal_id": signal.get("signal_id"),
        "trading_date": trading_date,
        "status": status,
        "requested_notional": round(notional, 4),
        "portfolio_allocated_capacity": round(allocated_capacity, 4),
        "requested_quantity": requested_quantity,
        "filled_quantity": filled_quantity,
        "unfilled_quantity": remaining,
        "reference_price": reference,
        "limit_price": round(limit_price, 4),
        "fill_price": round(fill_price, 4) if fill_price else None,
        "fills": fills,
        "simulated_arrival_time": arrival.isoformat(),
        "simulated_cancel_time": cancel.isoformat(),
        "buy_cost": buy_cost,
        "simulation": True,
        "broker_called": False,
        "research_only": True,
        "live_weight": 0.0,
        "provenance": {
            "decision_mode": signal.get("decision_mode") or "replay",
            "snapshot_id": signal.get("snapshot_id"),
            "snapshot_hash": signal.get("snapshot_hash"),
            "config_hash": signal.get("config_hash") or canonical_hash(config),
            "code_version": signal.get("code_version"),
        },
    }
    result["fill_hash"] = canonical_hash(result)
    return result


def label_d1_outcome(
    fill: Mapping[str, Any],
    sessions: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    quantity = int(fill.get("filled_quantity") or 0)
    if quantity <= 0 or fill.get("status") == "UNFILLED":
        return {
            "schema": OUTCOME_SCHEMA,
            "signal_id": fill.get("signal_id"),
            "status": "not_opened",
            "filled_quantity": 0,
            "capital_days": 0,
            "right_censored": False,
        }
    entry_day = str(fill["trading_date"])
    d1 = add_trading_days(entry_day, 1).isoformat()
    maximum_days = int(config["exit"]["maximum_observation_trading_sessions"])
    window_start, window_end = str(config["exit"]["primary_window"]).split("-", 1)
    allowed_day_sequence = [
        add_trading_days(entry_day, offset).isoformat()
        for offset in range(1, maximum_days + 1)
    ]
    allowed_days = set(allowed_day_sequence)
    remaining = quantity
    exit_fills: list[dict[str, Any]] = []
    marks: list[float] = []
    blocked_days = 0
    for session in sorted(
        (
            item
            for item in sessions
            if isinstance(item, Mapping)
            and str(item.get("trading_date") or "") in allowed_days
        ),
        key=lambda item: str(item["trading_date"]),
    ):
        day = str(session["trading_date"])
        window_start_at = _clock(day, window_start)
        window_end_at = _clock(day, window_end)
        executable: list[tuple[datetime, datetime, float, int]] = []
        for row in session.get("bars") or []:
            if not isinstance(row, Mapping):
                raise TailCloseContractError("exit_bar_invalid")
            if "event_time" not in row or "available_time" not in row:
                raise TailCloseContractError("exit_bar_dual_time_incomplete")
            event_at = _as_datetime(row["event_time"], "exit_bar_event_time")
            available_at = _as_datetime(
                row["available_time"],
                "exit_bar_available_time",
            )
            if (
                event_at.date().isoformat() != day
                or available_at.date().isoformat() != day
            ):
                raise TailCloseContractError("exit_bar_trading_date_mismatch")
            if available_at < event_at:
                raise TailCloseContractError("exit_bar_available_before_event")
            # The configured TWAP window is half-open: 09:35 <= event < 09:40.
            # Rows not observable by the window end cannot support that exit.
            if not (
                window_start_at <= event_at < window_end_at
                and available_at <= window_end_at
            ):
                continue
            if row.get("blocked") is True:
                continue
            price = _as_float(row.get("bid_price", row.get("price", row.get("close"))))
            volume = _as_float(
                row.get("available_buy_volume", row.get("buy_volume", row.get("volume")))
            )
            if price and volume and volume > 0:
                executable.append(
                    (
                        event_at,
                        available_at,
                        float(price),
                        int(volume / 100) * 100,
                    )
                )
        executable.sort(key=lambda item: (item[0], item[1], item[2]))
        mark = _as_float(session.get("mark_price"))
        if mark and mark > 0:
            marks.append(float(mark))
        if not executable:
            blocked_days += 1
            continue
        session_start_quantity = remaining
        for index, (event_at, available_at, price, available) in enumerate(
            executable
        ):
            bars_left = len(executable) - index
            scheduled = math.ceil(remaining / bars_left / 100) * 100
            sold = min(remaining, available, scheduled)
            sold = int(sold / 100) * 100
            if sold <= 0:
                continue
            exit_fills.append(
                {
                    "trading_date": day,
                    "event_time": event_at.isoformat(),
                    "available_time": available_at.isoformat(),
                    "price": price,
                    "quantity": sold,
                }
            )
            remaining -= sold
            if remaining <= 0:
                break
        if remaining <= 0:
            break
        if remaining == session_start_quantity or remaining > 0:
            blocked_days += 1
    exited = quantity - remaining
    entry_price = float(fill.get("fill_price") or 0)
    exit_price = (
        sum(item["price"] * item["quantity"] for item in exit_fills) / exited
        if exited
        else None
    )
    observed_dates = {
        str(item.get("trading_date") or "")
        for item in sessions
        if isinstance(item, Mapping)
        and str(item.get("trading_date") or "") in allowed_days
    }
    observation_complete = set(allowed_day_sequence).issubset(observed_dates)
    right_censored = remaining > 0 and observation_complete
    conservative_mark = min(marks) if marks else entry_price
    valuation_price = exit_price if remaining == 0 and exit_price else conservative_mark
    corporate_action_status = (
        "clear"
        if all(
            str(item.get("corporate_action_status") or "clear") == "clear"
            for item in sessions
        )
        else "reconciliation_required"
    )
    pnl = None
    if entry_price > 0 and valuation_price > 0:
        if remaining == 0 and exit_price:
            pnl = estimate_round_trip_pnl(
                entry_price=entry_price,
                exit_price=float(exit_price),
                quantity=quantity,
                asof=entry_day,
                corporate_action_status=corporate_action_status,
            )
        else:
            entry_value = entry_price * quantity
            realised_exit_value = sum(
                item["price"] * item["quantity"] for item in exit_fills
            )
            censored_exit_value = conservative_mark * remaining
            exit_value = realised_exit_value + censored_exit_value
            buy_cost = estimate_trade_cost("buy", entry_value, asof=entry_day)
            sell_cost = estimate_trade_cost("sell", exit_value, asof=entry_day)
            estimated_cost = float(buy_cost["total"]) + float(sell_cost["total"])
            pnl = {
                "schema": "a_share_pnl_estimate_v1",
                "status": "estimate_only",
                "gross_pnl": round(exit_value - entry_value, 4),
                "estimated_cost": round(estimated_cost, 4),
                "estimated_net_pnl": round(
                    exit_value - entry_value - estimated_cost,
                    4,
                ),
                "realised_exit_value": round(realised_exit_value, 4),
                "censored_exit_value": round(censored_exit_value, 4),
                "corporate_action_status": corporate_action_status,
                "reconciliation_required": corporate_action_status != "clear",
                "authoritative_source": "broker_statement",
                "fee_schedule_version": buy_cost["rules"]["version"],
            }
    tail_loss_pct = (
        min((mark / entry_price - 1) * 100 for mark in marks)
        if marks and entry_price > 0
        else None
    )
    if exited + remaining != quantity:
        raise TailCloseContractError("exit_quantity_conservation_failed")
    return {
        "schema": OUTCOME_SCHEMA,
        "strategy_id": PRIMARY_STRATEGY_ID,
        "signal_id": fill.get("signal_id"),
        "status": (
            "exited"
            if remaining == 0
            else "right_censored"
            if right_censored
            else "blocked_pending"
        ),
        "entry_trading_date": entry_day,
        "d1_trading_date": d1,
        "planned_exit": {
            "window_start": window_start,
            "window_end": window_end,
        },
        "filled_quantity": quantity,
        "exited_quantity": exited,
        "remaining_quantity": remaining,
        "exit_fills": exit_fills,
        "exit_price": round(exit_price, 4) if exit_price else None,
        "days_blocked": blocked_days,
        "capital_days": blocked_days + 1,
        "tail_loss_pct": round(tail_loss_pct, 4) if tail_loss_pct is not None else None,
        "right_censored": right_censored,
        "observation_complete": observation_complete,
        "conservative_valuation_price": round(conservative_mark, 4),
        "corporate_action_status": corporate_action_status,
        "pnl_estimate": pnl,
        "simulation": True,
        "broker_called": False,
    }


def simulate_after_hours_fixed_fill(
    signal: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Forward-only fixed-price queue model for the isolated 15:05 sibling."""
    if signal.get("strategy_id") != AFTER_HOURS_STRATEGY_ID:
        raise TailCloseContractError("after_hours_strategy_id_invalid")
    if signal.get("queue_observable") is not True:
        return {
            "schema": AFTER_HOURS_SCHEMA,
            "strategy_id": AFTER_HOURS_STRATEGY_ID,
            "status": "not_ready",
            "reason": "queue_not_observable",
            "simulation": True,
            "broker_called": False,
            "live_weight": 0.0,
        }
    trading_date = str(signal["trading_date"])
    decision = _clock(trading_date, "15:05:00")
    end = _clock(trading_date, "15:30:00")
    close_price = float(signal.get("close_price") or 0)
    quantity = int(float(signal.get("requested_notional") or 0) / close_price / 100) * 100
    if close_price <= 0 or quantity <= 0:
        raise TailCloseContractError("after_hours_order_invalid")
    available = 0
    for row in observations:
        event = _as_datetime(row.get("event_time"), "event_time")
        observed = _as_datetime(row.get("available_time"), "available_time")
        if decision <= event <= end and observed >= event and observed <= end:
            if "incremental_matched_sell_volume" not in row:
                raise TailCloseContractError(
                    "after_hours_incremental_queue_volume_missing"
                )
            available += max(
                0,
                int(float(row["incremental_matched_sell_volume"])),
            )
    filled = min(
        quantity,
        int(
            available
            * float(config["execution"]["after_hours_queue_discount"])
            / 100
        )
        * 100,
    )
    status = "FULL_FILL" if filled == quantity else (
        "PARTIAL_FILL" if filled else "UNFILLED"
    )
    return {
        "schema": AFTER_HOURS_SCHEMA,
        "strategy_id": AFTER_HOURS_STRATEGY_ID,
        "status": status,
        "trading_date": trading_date,
        "decision_time": decision.isoformat(),
        "window_end": end.isoformat(),
        "fill_price": close_price if filled else None,
        "requested_quantity": quantity,
        "filled_quantity": filled,
        "unfilled_quantity": quantity - filled,
        "queue_model": "fixed_price_time_priority_forward_shadow",
        "simulation": True,
        "broker_called": False,
        "live_weight": 0.0,
    }


def evaluate_kill_switch(
    metrics: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    global_block = int(metrics.get("global_risk_incidents") or 0) > 0
    if global_block:
        reasons.append("global_risk")
    if int(metrics.get("pit_violations") or 0) > 0:
        reasons.append("pit_violation")
    if int(metrics.get("broker_call_count") or 0) > 0:
        reasons.append("broker_boundary_violation")
    if float(metrics.get("live_weight") or 0) != 0:
        reasons.append("live_weight_violation")
    if int(metrics.get("ledger_audit_mismatches") or 0) > 0:
        reasons.append("ledger_audit_mismatch")
    fill_error = _as_float(metrics.get("fill_rate_error"))
    if fill_error is not None and fill_error > float(
        config["validation"]["shadow"]["maximum_fill_rate_error"]
    ):
        reasons.append("fill_model_drift")
    incremental = _as_float(metrics.get("incremental_net_expectancy"))
    if incremental is not None and incremental <= 0:
        reasons.append("incremental_edge_lost")
    return {
        "schema": KILL_SWITCH_SCHEMA,
        "strategy_id": metrics.get("strategy_id"),
        "blocked": bool(reasons),
        "reasons": reasons,
        "scope": "all_strategies" if global_block else "strategy_lane",
        "affected_strategy_id": (
            "*" if global_block else metrics.get("strategy_id")
        ),
        "required_state": "research_only" if reasons else "unchanged",
        "live_weight": 0.0,
        "broker_call_count": 0,
    }
