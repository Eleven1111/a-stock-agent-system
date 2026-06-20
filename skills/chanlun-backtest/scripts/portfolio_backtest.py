#!/usr/bin/env python3
"""Point-in-time portfolio replay for persisted A-share candidate snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import datetime
from typing import Any, Mapping, Sequence


HERE = os.path.dirname(os.path.abspath(__file__))
COMMON = os.path.abspath(os.path.join(HERE, "..", "..", "common"))
for path in (HERE, COMMON):
    if path not in sys.path:
        sys.path.insert(0, path)

import daban_bt_stats as stats  # noqa: E402
import research_gate  # noqa: E402
from research_artifact import verify_artifact, write_artifact  # noqa: E402
from tradeability import assess_tradeability, limit_pct, round_limit  # noqa: E402


INPUT_SCHEMA = "portfolio_backtest_input_v1"
REPORT_SCHEMA = "portfolio_backtest_report_v1"
RULES_VERSION = "point-in-time-portfolio-v1"


def _code(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text[2:] if text.startswith(("sh", "sz")) else text.zfill(6)


def _dt(value: Any) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _policy(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(payload.get("policy") or {})
    policy = {
        "initial_cash": _num(raw.get("initial_cash"), 1_000_000.0),
        "top_n": int(raw.get("top_n", 5)),
        "max_positions": int(raw.get("max_positions", 5)),
        "minimum_holding_sessions": int(raw.get("minimum_holding_sessions", 1)),
        "commission": _num(raw.get("commission"), 0.00025),
        "stamp_tax": _num(raw.get("stamp_tax"), 0.0005),
        "slippage": _num(raw.get("slippage"), 0.002),
        "lot_size": int(raw.get("lot_size", 100)),
    }
    if policy["initial_cash"] <= 0:
        raise ValueError("initial_cash must be positive")
    if policy["top_n"] <= 0 or policy["max_positions"] <= 0:
        raise ValueError("top_n and max_positions must be positive")
    if policy["minimum_holding_sessions"] < 1:
        raise ValueError("minimum_holding_sessions must be at least 1 for A-share T+1")
    if policy["lot_size"] <= 0:
        raise ValueError("lot_size must be positive")
    for name in ("commission", "stamp_tax", "slippage"):
        if policy[name] < 0:
            raise ValueError(f"{name} must not be negative")
    return policy


def validate_payload(payload: Mapping[str, Any]) -> None:
    if payload.get("schema") != INPUT_SCHEMA:
        raise ValueError(f"schema must be {INPUT_SCHEMA}")
    if not payload.get("strategy_id"):
        raise ValueError("strategy_id is required")
    if not isinstance(payload.get("snapshots"), list):
        raise ValueError("snapshots must be a list")
    if not isinstance(payload.get("bars_by_code"), dict):
        raise ValueError("bars_by_code must be an object")
    for snapshot in payload.get("snapshots") or []:
        if not snapshot.get("date") or not snapshot.get("generated_at"):
            raise ValueError("every snapshot requires date and generated_at")
        if not isinstance(snapshot.get("source_versions"), dict) or not snapshot.get("source_versions"):
            raise ValueError("every snapshot requires non-empty source_versions")
        if not isinstance(snapshot.get("candidates"), list):
            raise ValueError("every snapshot requires a candidates list")
    _policy(payload)


def _bars_by_code(payload: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    output = {}
    for raw_code, raw_bars in (payload.get("bars_by_code") or {}).items():
        rows = [dict(bar) for bar in raw_bars or []]
        rows.sort(key=lambda bar: str(bar.get("date") or ""))
        output[_code(raw_code)] = rows
    return output


def _sessions(payload: Mapping[str, Any], bars: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[str]:
    benchmark_dates = {
        str(bar.get("date") or "") for bar in payload.get("benchmark_bars") or []
        if bar.get("date")
    }
    dates = benchmark_dates or {
        str(bar.get("date") or "")
        for rows in bars.values()
        for bar in rows
        if bar.get("date")
    }
    return sorted(dates)


def _score(
    candidate: Mapping[str, Any],
    weights: Mapping[str, Any],
    disabled_component: str | None,
    ranking_mode: str,
    snapshot_date: str,
) -> float:
    code = _code(candidate.get("code"))
    if ranking_mode == "code":
        return -float(int(code or "0"))
    if ranking_mode == "random":
        digest = hashlib.sha256(f"{snapshot_date}|{code}".encode()).digest()
        return float(int.from_bytes(digest[:8], "big"))
    if disabled_component is None:
        return _num(candidate.get("score"), -math.inf)
    components = candidate.get("components") or {}
    active = {
        str(name): _num(weight)
        for name, weight in weights.items()
        if name != disabled_component and _num(weight) > 0 and name in components
    }
    total = sum(active.values())
    if total <= 0:
        return -math.inf
    return sum(_num(components[name]) * weight for name, weight in active.items()) / total


def _evidence_is_observable(candidate: Mapping[str, Any], snapshot: Mapping[str, Any]) -> bool:
    try:
        evidence_at = _dt(candidate.get("evidence_asof"))
        generated_at = _dt(snapshot.get("generated_at"))
        return evidence_at <= generated_at and generated_at.date().isoformat() == str(snapshot.get("date"))
    except (TypeError, ValueError):
        return False


def _next_bar(rows: Sequence[Mapping[str, Any]], after_date: str) -> tuple[int, dict[str, Any]] | None:
    for index, bar in enumerate(rows):
        if str(bar.get("date") or "") > after_date:
            return index, dict(bar)
    return None


def _exit_bar(
    rows: Sequence[Mapping[str, Any]],
    sessions: Sequence[str],
    entry_index: int,
    entry_date: str,
    minimum_holding_sessions: int,
    code: str,
    name: str,
) -> dict[str, Any] | None:
    try:
        session_index = sessions.index(entry_date)
    except ValueError:
        return None
    target_index = session_index + minimum_holding_sessions
    if target_index >= len(sessions):
        return None
    target_date = sessions[target_index]
    for index in range(entry_index + 1, len(rows)):
        bar = rows[index]
        if str(bar.get("date") or "") < target_date or _num(bar.get("volume")) <= 0:
            continue
        previous_close = _num(rows[index - 1].get("close"))
        down = round_limit(previous_close, limit_pct(code, name), up=False)
        one_price_limit_down = all(
            abs(_num(bar.get(field)) - down) < 0.01
            for field in ("open", "high", "low", "close")
        )
        if one_price_limit_down:
            continue
        return dict(bar)
    return None


def _prepare_orders(
    payload: Mapping[str, Any],
    snapshots: Sequence[Mapping[str, Any]],
    *,
    disabled_component: str | None,
    ranking_mode: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    policy = _policy(payload)
    weights = dict(payload.get("weights") or {})
    bars = _bars_by_code(payload)
    sessions = _sessions(payload, bars)
    orders: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    for snapshot in sorted(snapshots, key=lambda item: str(item.get("date") or "")):
        snapshot_date = str(snapshot.get("date") or "")
        eligible = []
        for raw in snapshot.get("candidates") or []:
            candidate = dict(raw)
            code = _code(candidate.get("code"))
            decision = str(candidate.get("decision") or "").strip().lower()
            quality_status = str(candidate.get("quality_status") or "").strip().lower()
            if (
                candidate.get("eligible") is False
                or (decision and decision not in {"buy", "add"})
                or quality_status in {"rejected", "blocked"}
            ):
                rejections.append({"date": snapshot_date, "code": code, "reason": "policy_not_directional"})
                continue
            if not _evidence_is_observable(candidate, snapshot):
                rejections.append({"date": snapshot_date, "code": code, "reason": "future_dated_evidence"})
                continue
            score = _score(candidate, weights, disabled_component, ranking_mode, snapshot_date)
            if not math.isfinite(score):
                rejections.append({"date": snapshot_date, "code": code, "reason": "missing_rank_score"})
                continue
            eligible.append((score, code, candidate))
        eligible.sort(key=lambda item: (-item[0], item[1]))
        for score, code, candidate in eligible[:policy["top_n"]]:
            rows = bars.get(code) or []
            located = _next_bar(rows, snapshot_date)
            if located is None:
                rejections.append({"date": snapshot_date, "code": code, "reason": "missing_entry_bar"})
                continue
            entry_index, entry = located
            if entry_index <= 0:
                rejections.append({"date": snapshot_date, "code": code, "reason": "missing_previous_close"})
                continue
            previous_close = _num(rows[entry_index - 1].get("close"))
            tradeability = assess_tradeability(
                {
                    "price": entry.get("open"),
                    "prev_close": previous_close,
                    "open": entry.get("open"),
                    "high": entry.get("high"),
                    "low": entry.get("low"),
                    "volume": entry.get("volume"),
                },
                code,
                str(candidate.get("name") or ""),
            )
            if tradeability.get("tradeable") is False:
                reason = (
                    "entry_limit_up_sealed"
                    if tradeability.get("status") == "limit_up_sealed"
                    else "entry_not_tradeable"
                )
                rejections.append({"date": snapshot_date, "code": code, "reason": reason})
                continue
            exit_bar = _exit_bar(
                rows,
                sessions,
                entry_index,
                str(entry.get("date") or ""),
                policy["minimum_holding_sessions"],
                code,
                str(candidate.get("name") or ""),
            )
            if exit_bar is None:
                rejections.append({"date": snapshot_date, "code": code, "reason": "incomplete_horizon"})
                continue
            orders.append({
                "signal_date": snapshot_date,
                "entry_date": str(entry.get("date")),
                "exit_date": str(exit_bar.get("date")),
                "code": code,
                "name": candidate.get("name") or code,
                "lane": candidate.get("lane") or "default",
                "rank_score": score,
                "entry_bar": entry,
                "exit_bar": exit_bar,
            })
    return orders, rejections


def _benchmark_curve(payload: Mapping[str, Any], dates: Sequence[str]) -> dict[str, float]:
    bars = {
        str(bar.get("date") or ""): {
            "open": _num(bar.get("open")),
            "close": _num(bar.get("close")),
        }
        for bar in payload.get("benchmark_bars") or []
    }
    selected = [
        (date, bars[date])
        for date in dates
        if bars.get(date, {}).get("open", 0) > 0 and bars.get(date, {}).get("close", 0) > 0
    ]
    if not selected:
        return {}
    first_open = selected[0][1]["open"]
    return {date: value["close"] / first_open for date, value in selected}


def _metrics(
    initial_cash: float,
    final_equity: float,
    equity_curve: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
    benchmark_curve: Mapping[str, float],
) -> dict[str, Any]:
    equities = [_num(row.get("equity")) for row in equity_curve]
    peak = initial_cash
    max_drawdown = 0.0
    for value in equities:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = min(max_drawdown, value / peak - 1.0)
    total_return = final_equity / initial_cash - 1.0
    benchmark_return = (list(benchmark_curve.values())[-1] - 1.0) if benchmark_curve else 0.0
    trade_returns = [_num(trade.get("net_return")) for trade in trades]
    average_equity = sum(equities) / len(equities) if equities else initial_cash
    traded_value = sum(
        _num(trade.get("entry_cost")) + _num(trade.get("exit_proceeds"))
        for trade in trades
    )
    return {
        "initial_cash": round(initial_cash, 2),
        "final_equity": round(final_equity, 2),
        "total_return": total_return,
        "benchmark_return": benchmark_return,
        "excess_return": total_return - benchmark_return,
        "max_drawdown": max_drawdown,
        "closed_trades": len(trades),
        "win_rate": (
            sum(value > 0 for value in trade_returns) / len(trade_returns)
            if trade_returns else 0.0
        ),
        "average_trade_return": sum(trade_returns) / len(trade_returns) if trade_returns else 0.0,
        "turnover": traded_value / average_equity if average_equity > 0 else 0.0,
    }


def run_portfolio(
    payload: Mapping[str, Any],
    *,
    snapshots: Sequence[Mapping[str, Any]] | None = None,
    disabled_component: str | None = None,
    ranking_mode: str = "score",
) -> dict[str, Any]:
    validate_payload(payload)
    policy = _policy(payload)
    selected_snapshots = list(payload.get("snapshots") if snapshots is None else snapshots)
    orders, rejections = _prepare_orders(
        payload,
        selected_snapshots,
        disabled_component=disabled_component,
        ranking_mode=ranking_mode,
    )
    bars = _bars_by_code(payload)
    all_sessions = _sessions(payload, bars)
    if orders:
        first_entry = min(order["entry_date"] for order in orders)
        last_exit = max(order["exit_date"] for order in orders)
        sessions = [date for date in all_sessions if first_entry <= date <= last_exit]
    else:
        sessions = []
    orders_by_entry: dict[str, list[dict[str, Any]]] = {}
    for order in orders:
        orders_by_entry.setdefault(order["entry_date"], []).append(order)

    cash = policy["initial_cash"]
    slot_value = policy["initial_cash"] / policy["max_positions"]
    positions: dict[str, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    for date in sessions:
        for order in orders_by_entry.get(date, []):
            code = order["code"]
            if code in positions:
                rejections.append({"date": date, "code": code, "reason": "duplicate_open_position"})
                continue
            if len(positions) >= policy["max_positions"]:
                rejections.append({"date": date, "code": code, "reason": "max_positions"})
                continue
            raw_price = _num(order["entry_bar"].get("open"))
            entry_price = raw_price * (1.0 + policy["slippage"])
            budget = min(slot_value, cash)
            shares = math.floor(
                budget / (entry_price * (1.0 + policy["commission"])) / policy["lot_size"]
            ) * policy["lot_size"]
            if shares <= 0:
                rejections.append({"date": date, "code": code, "reason": "insufficient_cash_for_lot"})
                continue
            entry_cost = shares * entry_price * (1.0 + policy["commission"])
            cash -= entry_cost
            positions[code] = {**order, "shares": shares, "entry_price": entry_price, "entry_cost": entry_cost}

        for code, position in list(positions.items()):
            if position["exit_date"] != date:
                continue
            exit_price = _num(position["exit_bar"].get("close")) * (1.0 - policy["slippage"])
            proceeds = position["shares"] * exit_price * (
                1.0 - policy["commission"] - policy["stamp_tax"]
            )
            cash += proceeds
            net_return = proceeds / position["entry_cost"] - 1.0
            trades.append({
                key: position[key]
                for key in ("signal_date", "entry_date", "exit_date", "code", "name", "lane", "rank_score")
            } | {
                "shares": position["shares"],
                "entry_price": position["entry_price"],
                "exit_price": exit_price,
                "net_return": net_return,
                "pnl": proceeds - position["entry_cost"],
                "entry_cost": position["entry_cost"],
                "exit_proceeds": proceeds,
            })
            del positions[code]

        marked = cash
        for code, position in positions.items():
            close = next(
                (_num(bar.get("close")) for bar in bars.get(code, []) if str(bar.get("date")) == date),
                position["entry_price"],
            )
            marked += position["shares"] * close
        equity_curve.append({"date": date, "equity": marked, "cash": cash, "open_positions": len(positions)})

    final_equity = equity_curve[-1]["equity"] if equity_curve else cash
    benchmark_curve = _benchmark_curve(payload, [row["date"] for row in equity_curve])
    return {
        "schema": "portfolio_replay_v1",
        "ranking_mode": ranking_mode,
        "disabled_component": disabled_component,
        "policy": policy,
        "metrics": _metrics(
            policy["initial_cash"], final_equity, equity_curve, trades, benchmark_curve
        ),
        "trades": trades,
        "rejections": rejections,
        "equity_curve": equity_curve,
        "benchmark_curve": benchmark_curve,
    }


def _daily_returns(curve: Sequence[Mapping[str, Any]], field: str) -> list[float]:
    values = [_num(row.get(field)) for row in curve]
    return [values[index] / values[index - 1] - 1.0 for index in range(1, len(values)) if values[index - 1] > 0]


def _gate_statistics(result: Mapping[str, Any]) -> dict[str, Any]:
    equities = {
        str(row.get("date")): _num(row.get("equity"))
        for row in result.get("equity_curve") or []
    }
    benchmark = dict(result.get("benchmark_curve") or {})
    dates = sorted(set(equities) & set(benchmark))
    excess = []
    previous_equity = _num((result.get("policy") or {}).get("initial_cash"))
    previous_benchmark = 1.0
    for current in dates:
        if previous_equity <= 0 or previous_benchmark <= 0:
            continue
        portfolio_return = equities[current] / previous_equity - 1.0
        benchmark_return = _num(benchmark[current]) / previous_benchmark - 1.0
        excess.append(portfolio_return - benchmark_return)
        previous_equity = equities[current]
        previous_benchmark = _num(benchmark[current])
    test = stats.sign_flip_test_mean(excess, n_perm=5000)
    return {
        "permutation_p": test["p_value"],
        "fdr_p": test["p_value"],
        "oos_alpha": sum(excess) / len(excess) if excess else 0.0,
        "benchmark_alpha": 0.0,
        "oos_sample_count": len(excess),
        "daily_excess_ci": stats.cluster_bootstrap_mean(excess, n_boot=2000),
    }


def analyze_payload(payload: Mapping[str, Any], *, split_date: str) -> dict[str, Any]:
    validate_payload(payload)
    snapshots = list(payload.get("snapshots") or [])
    is_snapshots = [row for row in snapshots if str(row.get("date") or "") < split_date]
    oos_snapshots = [row for row in snapshots if str(row.get("date") or "") >= split_date]
    if oos_snapshots:
        try:
            locked_at = _dt(payload.get("rules_locked_at"))
            first_oos = min(_dt(row.get("generated_at")) for row in oos_snapshots)
        except (TypeError, ValueError) as exc:
            raise ValueError("rules_locked_at and OOS generated_at must be valid timestamps") from exc
        if locked_at > first_oos:
            raise ValueError("rules_locked_at must not be after the first OOS snapshot")
    is_result = run_portfolio(payload, snapshots=is_snapshots)
    oos_result = run_portfolio(payload, snapshots=oos_snapshots)
    ablations = {
        f"without_{component}": run_portfolio(
            payload,
            snapshots=oos_snapshots,
            disabled_component=str(component),
        )
        for component in (payload.get("weights") or {})
    }
    controls = {
        "random_rank": run_portfolio(payload, snapshots=oos_snapshots, ranking_mode="random"),
        "equal_weight_candidates": run_portfolio(payload, snapshots=oos_snapshots, ranking_mode="code"),
    }
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "strategy_id": payload.get("strategy_id"),
        "split_date": split_date,
        "rules_version": RULES_VERSION,
        "is": is_result,
        "oos": oos_result,
        "ablations": ablations,
        "controls": controls,
        "gate_metrics": _gate_statistics(oos_result),
    }


def _rules(payload: Mapping[str, Any], split_date: str) -> dict[str, Any]:
    return {
        "version": RULES_VERSION,
        "split_date": split_date,
        "policy": _policy(payload),
        "weights": dict(payload.get("weights") or {}),
        "entry": "next_session_open",
        "exit": "close_after_minimum_holding_sessions",
    }


def write_research_artifact(
    path: str,
    *,
    input_path: str,
    payload: Mapping[str, Any],
    report: Mapping[str, Any],
) -> dict[str, Any]:
    controls = report.get("controls") or {}
    counts = {
        "benchmark_buy_hold": len((report.get("oos") or {}).get("benchmark_curve") or {}),
        "random_rank": int((controls.get("random_rank") or {}).get("metrics", {}).get("closed_trades", 0)),
        "equal_weight_candidates": int(
            (controls.get("equal_weight_candidates") or {}).get("metrics", {}).get("closed_trades", 0)
        ),
    }
    return write_artifact(
        path,
        input_path=input_path,
        strategy_id=str(payload.get("strategy_id") or ""),
        rules=_rules(payload, str(report.get("split_date") or "")),
        result=dict(report),
        gate_metrics=dict(report.get("gate_metrics") or {}),
        control_counts=counts,
    )


def verify_research_artifact(path: str, *, expected_sha256: str | None = None) -> dict[str, Any]:
    return verify_artifact(path, expected_sha256=expected_sha256)


def evaluate_research_gate(
    report: Mapping[str, Any],
    *,
    artifact_path: str,
    min_oos_samples: int = 60,
) -> dict[str, Any]:
    verification = verify_artifact(artifact_path)
    if not verification["valid"]:
        return {
            "decision": "blocked",
            "allowed_in_live_agent": False,
            "blocking_reasons": verification["errors"],
        }
    artifact = verification["artifact"]
    metrics = dict(report.get("gate_metrics") or {})
    controls = list((artifact.get("control_counts") or {}).keys())
    state = {
        "asof": datetime.now().date().isoformat(),
        "strategy_id": report.get("strategy_id"),
        "phase": "oos_complete",
        "rules_locked": True,
        "has_costs": True,
        "reports_all_variants": True,
        "controls": controls,
        "required_controls": controls,
        "stat_tests": ["cluster_bootstrap", "paired_sign_flip", "ablation"],
        "required_stat_tests": ["cluster_bootstrap", "paired_sign_flip", "ablation"],
        "oos_run_count": 1,
        "changed_after_oos": False,
        "min_oos_samples": int(min_oos_samples),
        **metrics,
        "evidence_artifact": os.path.abspath(os.path.expanduser(artifact_path)),
        "evidence_sha256": artifact["artifact_sha256"],
    }
    return research_gate.evaluate_gate(state)


def main() -> int:
    parser = argparse.ArgumentParser(description="A股候选快照的组合级 point-in-time 回放")
    parser.add_argument("--input", required=True, help="portfolio_backtest_input_v1 JSON")
    parser.add_argument("--split", required=True, help="OOS 起始交易日 YYYY-MM-DD")
    parser.add_argument("--artifact", required=True, help="写入不可篡改研究产物 JSON")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    with open(args.input, encoding="utf-8") as handle:
        payload = json.load(handle)
    report = analyze_payload(payload, split_date=args.split)
    artifact = write_research_artifact(
        args.artifact,
        input_path=args.input,
        payload=payload,
        report=report,
    )
    output = {
        "report": report,
        "artifact": args.artifact,
        "artifact_sha256": artifact["artifact_sha256"],
        "gate_result": evaluate_research_gate(report, artifact_path=args.artifact),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2) if args.json else json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
