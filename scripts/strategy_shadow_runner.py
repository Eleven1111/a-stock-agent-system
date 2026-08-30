#!/usr/bin/env python3
"""Run the six research strategies in an isolated, non-live shadow lane."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "skills" / "common"
if str(COMMON) not in sys.path:
    sys.path.insert(0, str(COMMON))

import assist_arbitrage  # noqa: E402
import divergence_reseal  # noqa: E402
import ice_point_reversal  # noqa: E402
import preleader_arbitrage  # noqa: E402
import preleader_pretable_store  # noqa: E402
import rank_surprise  # noqa: E402
import reverse_volume  # noqa: E402
from paths import data_file  # noqa: E402
from research_artifact import file_sha256, json_sha256  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402

STRATEGY_IDS = (
    "rank_surprise", "divergence_reseal", "assist_arbitrage",
    "preleader_arbitrage", "reverse_volume", "ice_point_reversal",
)


def _today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _input_path(asof: str) -> str:
    return data_file("stock-triage", os.path.join("strategy_evidence", f"{asof}.json"))


def _output_path(asof: str) -> str:
    return data_file("stock-triage", os.path.join("strategy_shadow", f"{asof}.json"))


def _load(path: str) -> Any:
    with open(os.path.abspath(os.path.expanduser(path)), encoding="utf-8") as handle:
        return json.load(handle)


def _records(payload: Any, asof: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, Mapping):
        rows = payload.get("records") or payload.get("events") or payload.get("candidates") or []
    else:
        rows = []
    if not isinstance(rows, list):
        raise ValueError("strategy shadow input requires an events/candidates list")
    output = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        row.setdefault("date", asof)
        row.setdefault("code", row.get("market_code") or row.get("symbol"))
        row.setdefault("sector", row.get("industry") or row.get("theme"))
        row.setdefault("attribute", row.get("sector") or row.get("industry"))
        output.append(row)
    return output


def _naked_code(value: Any) -> str:
    """候选池与竞价产物必须用同一把尺子归一化代码。

    只归一化一侧会让合并静默落空，而 sidecar 仍被记为「已使用」——artifact
    于是声称用了它其实没用上的证据，比干脆缺证据更难发现。
    """
    code = str(value or "").strip().lower()
    return code.removeprefix("sh").removeprefix("sz").zfill(6) if code else ""


def _merge_auction_evidence(payload: Any, asof: str) -> tuple[Any, list[str]]:
    """Merge same-day auction/market artifacts without inventing evidence."""
    if not isinstance(payload, Mapping):
        return payload, []
    merged = dict(payload)
    base_rows = _records(payload, asof)
    by_code = {_naked_code(row.get("code") or row.get("market_code")): row for row in base_rows}
    used = []
    auction_path = data_file("daban-stock-picker", "auction_shortlist_latest.json")
    auction = read_json(auction_path, None)
    if isinstance(auction, Mapping) and str(auction.get("asof") or "")[:10] == asof:
        used.append(auction_path)
        auction_rows = []
        for key in ("factors", "shortlist", "research_candidates", "rejected"):
            value = auction.get(key)
            if isinstance(value, list):
                auction_rows.extend(value)
        for raw in auction_rows:
            if not isinstance(raw, Mapping):
                continue
            code = _naked_code(raw.get("code") or raw.get("market_code"))
            if code and code in by_code:
                by_code[code].update(dict(raw))
    selection_path = data_file("stock-triage", "hot_money_selection_latest.json")
    selection = read_json(selection_path, None)
    if isinstance(selection, Mapping) and str(selection.get("asof") or "")[:10] == asof:
        used.append(selection_path)
        if not merged.get("market_state"):
            merged["market_state"] = selection.get("market_state")
    merged["candidates"] = list(by_code.values())
    return merged, used


def _preleader_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把候选池的封板时刻映射成 S4 需要的「龙头已确认」字段。

    S4 靠 ``pick_confirmed_leader`` 在同属性组里挑确认时刻最早者，需要每行带
    ``confirmed`` / ``confirmed_time``；反应窗口条件还要候选自己的
    ``evaluation_time``。候选池里对应的事实是 ``first_seal``（首次封板时刻）。

    **这是一处口径判断，不是既有字段的搬运**：这里把"当日封上板"等同于"该标的
    已确认"，把封板时刻同时当作龙头的确认时刻与候选自身的反应时刻。原案例
    （鹏起科技→航天通信）正是这个形态。没封板的行不给 ``confirmed``，让
    fail-closed 逻辑照常把龙头判成不可判定，而不是拿涨幅之类的代理值凑一个。
    """
    output = []
    for record in records:
        row = dict(record)
        seal = row.get("first_seal")
        if seal:
            row.setdefault("confirmed", True)
            row.setdefault("confirmed_time", seal)
            row.setdefault("evaluation_time", seal)
        output.append(row)
    return output


def _unavailable(strategy_id: str, reason: str) -> dict[str, Any]:
    return {"strategy_id": strategy_id, "status": "unavailable", "reasons": [reason],
            "results": [], "research_only": True, "execution_eligible": False,
            "forward_settlement_eligible": False}


def forward_settlement_eligible(strategy_result: Mapping[str, Any]) -> bool:
    """Compatibility gate: legacy shadow signals are ineligible by default.

    Older S2 artifacts have neither verified same-clock baseline semantics nor
    the explicit per-result marker. Future settlers must call this boundary and
    cannot infer eligibility merely from ``status=signal``.
    """
    signals = [
        row for row in strategy_result.get("results") or []
        if isinstance(row, Mapping) and row.get("status") == "signal"
    ]
    if not signals:
        return False
    if strategy_result.get("strategy_id") == "divergence_reseal":
        return all(row.get("forward_settlement_eligible") is True for row in signals)
    if strategy_result.get("strategy_id") == "ice_point_reversal":
        return all(
            row.get("forward_settlement_eligible") is True
            and isinstance(row.get("tradeable_leader_binding"), Mapping)
            and str(row["tradeable_leader_binding"].get("code") or "")
            == str(row.get("code") or "")
            and row["tradeable_leader_binding"].get("leader_confirmed") is True
            for row in signals
        )
    return True


def _strategy_rule_bindings() -> dict[str, dict[str, Any]]:
    """Bind every frozen prediction to executable strategy rules and config."""
    modules = {
        "rank_surprise": rank_surprise, "divergence_reseal": divergence_reseal,
        "assist_arbitrage": assist_arbitrage, "preleader_arbitrage": preleader_arbitrage,
        "reverse_volume": reverse_volume, "ice_point_reversal": ice_point_reversal,
    }
    bindings: dict[str, dict[str, Any]] = {}
    for strategy_id, module in modules.items():
        config_names = ["scoring.yaml"] if strategy_id == "ice_point_reversal" else [
            "daban_thresholds.yaml"
        ]
        paths = [Path(str(module.__file__)).resolve()]
        paths.extend(ROOT / "config" / name for name in config_names)
        paths.append(ROOT / "config" / "strategy_packs" / f"{strategy_id}.yaml")
        sources = {
            str(path.relative_to(ROOT)): file_sha256(str(path))
            for path in paths if path.is_file()
        }
        binding = {"version": "strategy-shadow-rules-v1", "sources": sources}
        bindings[strategy_id] = {**binding, "sha256": json_sha256(binding)}
    return bindings


def _run_one(
    strategy_id: str, records: list[dict[str, Any]], market_state: Any,
    *, pretable: Mapping[str, Any] | None = None, pretable_reason: str = "ok",
) -> dict[str, Any]:
    if not records:
        reason = (
            "tradeable_leader_binding_unavailable"
            if strategy_id == "ice_point_reversal" else "input_records_empty"
        )
        return _unavailable(strategy_id, reason)
    if strategy_id == "preleader_arbitrage" and pretable is None:
        # 缺盘前表就是缺证据，报 unavailable 并带上具体原因；传一张空表进去会让
        # 它输出 no_signal，把"没数据"伪装成"明确不满足"。
        return _unavailable(strategy_id, pretable_reason)
    runners: dict[str, Callable[[], list[dict[str, Any]]]] = {
        "rank_surprise": lambda: rank_surprise.evaluate_universe(records, market_state=market_state),
        "divergence_reseal": lambda: divergence_reseal.evaluate_universe(records),
        "assist_arbitrage": lambda: assist_arbitrage.evaluate_universe(records),
        "preleader_arbitrage": lambda: preleader_arbitrage.evaluate_universe(
            _preleader_records(records), pretable=pretable),
        "reverse_volume": lambda: reverse_volume.evaluate_universe(records, market_state=market_state),
        "ice_point_reversal": lambda: ice_point_reversal.evaluate_universe(
            records, market_state=market_state),
    }
    modules = {
        "rank_surprise": rank_surprise, "divergence_reseal": divergence_reseal,
        "assist_arbitrage": assist_arbitrage, "preleader_arbitrage": preleader_arbitrage,
        "reverse_volume": reverse_volume, "ice_point_reversal": ice_point_reversal,
    }
    try:
        results = runners[strategy_id]()
    except Exception as exc:  # noqa: BLE001 - preserve the failure in the artifact
        return _unavailable(strategy_id, f"runner_error:{type(exc).__name__}:{exc}")
    module = modules[strategy_id]
    summary = module.summarize(results)
    signal_codes = [{"code": code, "date": date}
                    for code, date in sorted(module.signal_codes(results))]
    counts = summary.get("status_counts") or {}
    status = (
        "unavailable" if counts.get("unavailable", 0) == len(results) and results
        else "signal" if counts.get("signal", 0) else "no_signal"
    )
    return {"strategy_id": strategy_id, "status": status, "summary": summary,
            "signal_codes": signal_codes, "results": results,
            "research_only": True, "execution_eligible": False,
            "forward_settlement_eligible": forward_settlement_eligible({
                "strategy_id": strategy_id, "results": results,
            })}


def run(input_path: str, *, asof: str | None = None) -> dict[str, Any]:
    requested_asof = asof or _today()
    payload = _load(input_path)
    source_asof = payload.get("asof") or payload.get("date") if isinstance(payload, Mapping) else None
    if not source_asof:
        raise ValueError("input asof is required")
    if str(source_asof)[:10] != requested_asof:
        raise ValueError(f"input asof mismatch: expected {requested_asof}, got {source_asof}")
    evidence_schema = payload.get("schema") if isinstance(payload, Mapping) else None
    canonical_schema = evidence_schema == "strategy_evidence_daily_v1"
    qualifications = payload.get("evidence_qualification") if canonical_schema else None
    if canonical_schema:
        if not isinstance(qualifications, Mapping):
            raise ValueError(
                "strategy shadow canonical forward evidence requires derived evidence qualification"
            )
        sidecars = []
    else:
        # Compatibility path for explicit historical fixtures. The scheduled
        # job always consumes the Strategy Evidence Dataset.
        payload, sidecars = _merge_auction_evidence(payload, requested_asof)
    records = _records(payload, requested_asof)
    raw_strategy_records = payload.get("strategy_records") if canonical_schema else None
    strategy_records = {}
    for sid in STRATEGY_IDS:
        if isinstance(raw_strategy_records, Mapping):
            selected = (raw_strategy_records or {}).get(sid) or []
            strategy_records[sid] = _records({"records": selected}, requested_asof)
        elif canonical_schema and sid == "ice_point_reversal":
            # Legacy canonical artifacts only carried a market boolean and no
            # security-level Tradeable Leader Binding. Never fan that state out
            # over the generic candidate pool.
            strategy_records[sid] = []
        else:
            strategy_records[sid] = records
    market_state = payload.get("market_state") if isinstance(payload, Mapping) else None
    if canonical_schema:
        raw_pretable = payload.get("preleader_pretable")
        pretable = dict(raw_pretable) if isinstance(raw_pretable, Mapping) else None
        pretable_reason = str(payload.get("preleader_pretable_status") or "pretable_status_missing")
    else:
        pretable, pretable_reason = preleader_pretable_store.load_previous_pretable(requested_asof)
    source_hash = json_sha256(payload)
    path = _output_path(requested_asof)
    existing = read_json(path, None)
    if isinstance(existing, Mapping) and existing.get("input_sha256") == source_hash:
        return dict(existing)
    if isinstance(existing, Mapping):
        raise ValueError("strategy shadow output already exists with a different input")
    result = {
        "schema": "strategy_shadow_daily_v1", "asof": requested_asof,
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "input_path": os.path.abspath(os.path.expanduser(input_path)),
        "input_sha256": source_hash, "record_count": len(records),
        "evidence_sidecars": sidecars,
        "evidence_schema": evidence_schema,
        "canonical_forward": bool(canonical_schema and all(
            isinstance((qualifications or {}).get(sid), Mapping)
            and (qualifications or {}).get(sid, {}).get("canonical_forward_eligible") is True
            for sid in STRATEGY_IDS
        )),
        "evidence_class": payload.get("evidence_class") if canonical_schema else None,
        "evidence_qualification": dict(qualifications or {}) if canonical_schema else None,
        "evidence_coverage": payload.get("coverage") if canonical_schema else None,
        "strategy_rule_bindings": _strategy_rule_bindings(),
        "preleader_pretable_asof": pretable.get("as_of") if pretable else None,
        "preleader_pretable_status": pretable_reason,
        "research_only": True, "execution_eligible": False, "live_order_sent": False,
        "strategies": {},
    }
    for sid in STRATEGY_IDS:
        qualification = (qualifications or {}).get(sid) if canonical_schema else None
        if canonical_schema and (
            not isinstance(qualification, Mapping)
            or qualification.get("canonical_forward_eligible") is not True
        ):
            reasons = list((qualification or {}).get("reasons") or ["evidence_not_canonical_forward"])
            result["strategies"][sid] = _unavailable(sid, str(reasons[0]))
            continue
        result["strategies"][sid] = _run_one(
            sid, strategy_records[sid], market_state,
            pretable=pretable, pretable_reason=pretable_reason,
        )
    result["result_sha256"] = json_sha256(result)
    atomic_write_json(path, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="每日六策略 shadow 评估器")
    parser.add_argument("--input", default=None)
    parser.add_argument("--asof", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    requested_asof = args.asof or _today()
    output = run(args.input or _input_path(requested_asof), asof=requested_asof)
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(f"strategy-shadow-daily {output['asof']}: {output['record_count']} records")


if __name__ == "__main__":
    main()
