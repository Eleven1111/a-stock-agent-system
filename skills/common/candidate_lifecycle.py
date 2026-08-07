"""Durable lifecycle records for dynamic stock-selection candidates."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, Iterable, Mapping, Sequence

from paths import data_file
from state_store import atomic_write_json, mutate_json, read_json


def _code(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text[2:] if text.startswith(("sh", "sz")) else text.zfill(6)


def lifecycle_file(asof: str) -> str:
    return data_file("stock-triage", os.path.join("candidate_lifecycle", f"{asof}.json"))


def load_day(asof: str) -> Dict[str, Any]:
    return read_json(
        lifecycle_file(asof),
        {"schema": "candidate_lifecycle_v1", "asof": asof, "metadata": {}, "records": []},
    )


def cohort_initialized(asof: str) -> bool:
    """该交易日的队列是否真的被 initialize_day 建过。

    load_day 在文件缺失时返回空骨架，而 transition/observe_day 走 mutate_json
    会把这个骨架落盘 —— 于是「从未初始化」的日期凭空长出 metadata={} /
    records=[] 的文件，和「跑了但没候选」（metadata 有值）几乎无法区分，下游
    还会把它当合法队列。没建过就不结算、不落盘。
    """
    return os.path.exists(lifecycle_file(asof))


def initialize_day(
    asof: str,
    candidates: Sequence[Mapping[str, Any]],
    rejected: Mapping[str, Sequence[str]] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    records = []
    seen = set()
    for raw in candidates:
        item = dict(raw)
        code = _code(item.get("code") or item.get("market_code"))
        selected = any(bool(value) for value in (item.get("selected_by") or {}).values())
        records.append({
            **item,
            "candidate_id": f"{asof}:{code}",
            "code": code,
            "current_stage": "watch_pool" if selected else "discovery_rejected",
            "rejection_reasons": list(item.get("rejection_reasons") or []),
            "stage_history": [{
                "stage": "discovery",
                "event_asof": asof,
                "selected": selected,
                "recorded_at": now,
            }],
            "outcome": {"resolved": False},
        })
        seen.add(code)

    for raw_code, reasons in (rejected or {}).items():
        code = _code(raw_code)
        if code in seen:
            continue
        records.append({
            "candidate_id": f"{asof}:{code}",
            "code": code,
            "name": code,
            "current_stage": "discovery_rejected",
            "rejection_reasons": list(reasons),
            "stage_history": [{
                "stage": "discovery",
                "event_asof": asof,
                "selected": False,
                "recorded_at": now,
            }],
            "outcome": {"resolved": False},
        })

    payload = {
        "schema": "candidate_lifecycle_v1",
        "asof": asof,
        "generated_at": now,
        "metadata": dict(metadata or {}),
        "records": records,
    }
    atomic_write_json(lifecycle_file(asof), payload)
    return payload


def transition(
    source_asof: str,
    stage: str,
    selected_codes: Iterable[str],
    rejection_reasons: Mapping[str, Sequence[str]] | None = None,
    event_asof: str | None = None,
    details_by_code: Mapping[str, Mapping[str, Any]] | None = None,
) -> Dict[str, Any]:
    if not cohort_initialized(source_asof):
        return {}
    selected = {_code(code) for code in selected_codes}
    reason_map = {_code(code): list(reasons) for code, reasons in (rejection_reasons or {}).items()}
    detail_map = {_code(code): dict(value) for code, value in (details_by_code or {}).items()}
    participants = selected | set(reason_map) | set(detail_map)
    now = datetime.now().isoformat(timespec="seconds")

    def _mutate(state: Any) -> Dict[str, Any]:
        if not isinstance(state, dict):
            state = {"schema": "candidate_lifecycle_v1", "asof": source_asof, "metadata": {}, "records": []}
        for record in state.setdefault("records", []):
            code = _code(record.get("code"))
            if code not in participants:
                continue
            is_selected = code in selected
            event = {
                "stage": stage,
                "event_asof": event_asof or source_asof,
                "selected": is_selected,
                "recorded_at": now,
            }
            if code in detail_map:
                event["details"] = detail_map[code]
            record.setdefault("stage_history", []).append(event)
            if is_selected:
                record["current_stage"] = stage
                record["rejection_reasons"] = []
            elif code in reason_map:
                record["current_stage"] = f"rejected:{stage}"
                record["rejection_reasons"] = reason_map[code]
        state["updated_at"] = now
        return state

    return mutate_json(lifecycle_file(source_asof), _mutate, load_day(source_asof))


def _round_ret(value: float) -> float:
    return round(value * 100, 2)


def observe_day(
    source_asof: str,
    event_asof: str,
    trading_horizon: int,
    quotes_by_code: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Incrementally settle one historical cohort from the current full-market snapshot."""
    if not cohort_initialized(source_asof):
        return {}
    normalized_quotes = {_code(code): dict(value) for code, value in quotes_by_code.items()}
    now = datetime.now().isoformat(timespec="seconds")

    def _mutate(state: Any) -> Dict[str, Any]:
        if not isinstance(state, dict):
            return {"schema": "candidate_lifecycle_v1", "asof": source_asof, "metadata": {}, "records": []}
        for record in state.get("records", []):
            code = _code(record.get("code"))
            quote = normalized_quotes.get(code)
            signal_close = float(record.get("price") or 0.0)
            if not quote or signal_close <= 0:
                continue
            close = float(quote.get("price") or 0.0)
            open_price = float(quote.get("open") or 0.0)
            high = float(quote.get("high") or close)
            low = float(quote.get("low") or close)
            if close <= 0:
                continue

            outcome = record.setdefault("outcome", {"resolved": False})
            observations = [
                item for item in outcome.setdefault("observations", [])
                if item.get("date") != event_asof
            ]
            observations.append({
                "date": event_asof,
                "trading_horizon": trading_horizon,
                "open": open_price or None,
                "close": close,
                "high": high,
                "low": low,
            })
            observations.sort(key=lambda item: item["date"])
            outcome["observations"] = observations
            outcome["bars_observed"] = len(observations)
            outcome["max_gain"] = _round_ret(max(item["high"] for item in observations) / signal_close - 1)
            outcome["max_drawdown"] = _round_ret(min(item["low"] for item in observations) / signal_close - 1)
            if trading_horizon == 1:
                outcome["t1_open_ret"] = (
                    _round_ret(open_price / signal_close - 1) if open_price > 0 else None
                )
                outcome["t1_close_ret"] = _round_ret(close / signal_close - 1)
            if trading_horizon == 3:
                outcome["t3_close_ret"] = _round_ret(close / signal_close - 1)
            outcome["resolved"] = outcome.get("t3_close_ret") is not None
            if outcome["resolved"]:
                outcome["resolved_at"] = now
        state["updated_at"] = now
        return state

    return mutate_json(lifecycle_file(source_asof), _mutate, load_day(source_asof))


def settle_day(
    asof: str,
    kline_by_code: Mapping[str, Sequence[Mapping[str, Any]]],
) -> Dict[str, Any]:
    """Resolve T+1 and T+3 returns for every candidate with enough future bars."""
    now = datetime.now().isoformat(timespec="seconds")

    def _mutate(state: Any) -> Dict[str, Any]:
        if not isinstance(state, dict):
            return {"schema": "candidate_lifecycle_v1", "asof": asof, "metadata": {}, "records": []}
        for record in state.get("records", []):
            if record.get("outcome", {}).get("resolved"):
                continue
            code = _code(record.get("code"))
            bars = list(kline_by_code.get(code, []))
            index = next((i for i, bar in enumerate(bars) if str(bar.get("date")) == asof), None)
            if index is None or index + 1 >= len(bars):
                continue
            signal_close = float(bars[index]["close"])
            future = bars[index + 1:index + 4]
            if signal_close <= 0 or not future:
                continue
            t1 = future[0]
            horizon = future[-1]
            record["outcome"] = {
                "resolved": len(future) >= 3,
                "bars_observed": len(future),
                "t1_open_ret": _round_ret(float(t1["open"]) / signal_close - 1),
                "t1_close_ret": _round_ret(float(t1["close"]) / signal_close - 1),
                "t3_close_ret": _round_ret(float(horizon["close"]) / signal_close - 1)
                if len(future) >= 3 else None,
                "max_gain": _round_ret(max(float(bar["high"]) for bar in future) / signal_close - 1),
                "max_drawdown": _round_ret(min(float(bar["low"]) for bar in future) / signal_close - 1),
                "resolved_at": now,
            }
        state["updated_at"] = now
        return state

    return mutate_json(lifecycle_file(asof), _mutate, load_day(asof))
