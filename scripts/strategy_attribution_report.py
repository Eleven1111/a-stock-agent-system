#!/usr/bin/env python3
"""Strategy attribution report: where does daban T+1 gain evaporate by T+3?

The complaint this answers: daban (limit-up chasing) signals often look good
on the signal day, but the T+1 rebound reverses hard, and the T+1 settlement
rule means the position can only be closed on T+1 at the earliest. Before
changing the strategy, this report answers *where* the reversal concentrates:
weak market, late-cycle theme fade, high-board entries, or chase-in patterns.

Data sources (all real fields, confirmed against the code that writes them):
  - skills/stock-triage/scripts/performance_tracker.py (`evaluate_signal`)
    writes T+1/T+3 settlement onto each signal_history.json row:
      t1_open_premium, t1_close_ret, horizon_ret (T+3 final), outcome,
      settlement_status ("final"/"provisional"), promoted, alpha_t1.
  - skills/common/signal_ledger.py projects signal.opened + signal.*_settled
    events into the same row shape (`project_signals`); signal_history.json
    is the legacy/compat projection. Both share field names above.
  - Entry pattern: `strategy_id` is written as f"daban:{pattern}" by
    daban_candidate_api.py / hot_money_selection.py. Real enum values seen in
    the codebase: daban:first_board_reseal, daban:second_board_weak_to_strong,
    daban:mainline_leader_confirm.
  - Market temperature: `selection_context.market_timing.tier` (五档:
    冰点/修复/发酵/加速/极热), written by
    hot_money_selection.selection_context_for() and carried into the ledger
    payload by signal_ledger.signal_opened_event(). Only present on records
    that flowed through the full candidate-discovery pipeline; CLI-recorded
    signals (`performance_tracker.py --record`) do not set it.

Confirmed UNAVAILABLE (no fabricated proxy used):
  - Ladder height / theme-stage proxy (连板梯队高度): `lianban_ladder` only
    exists as a live daily cache at signal_context.json
    (skills/common/signal_context.py), overwritten each trading day. It is
    never persisted per historical signal_date, and selection_context does
    not embed a numeric ladder height. There is no historical join key.
  - Board level of the signal's own stock (首板/二板/三板+) as a numeric
    field: no such field is written onto signal_history rows. It could be
    guessed from `pattern` (first_board_reseal ~ 首板, second_board ~ 二板),
    but that would duplicate the entry-pattern dimension under a fake label
    and there is no 三板+ pattern in the enum at all. Reported unavailable
    rather than invented.

Research-only: reads settled signals and prints evidence. Never mutates
signal_history.json, never changes a weight or gate.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from statistics import mean, median
from typing import Any, Mapping, Sequence

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
STOCK_TRIAGE_SCRIPTS = os.path.join(ROOT, "skills", "stock-triage", "scripts")
for path in (COMMON, STOCK_TRIAGE_SCRIPTS, ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

import signal_ledger  # noqa: E402
from paths import data_file  # noqa: E402
from state_store import read_json  # noqa: E402

SCHEMA = "strategy_attribution_v1"
DEFAULT_MIN_SAMPLES = 10

# Market-temperature tier buckets. Thresholds are ladder heights, not tiers
# themselves; the temperature module already returns discrete tiers, so this
# just groups the five-tier scale into 强/中/弱 for a readable report.
TEMPERATURE_STRONG_TIERS = ("加速", "极热")
TEMPERATURE_MID_TIERS = ("发酵",)
TEMPERATURE_WEAK_TIERS = ("冰点", "修复")

LEGACY_HISTORY_FILE = data_file("stock-triage", "signal_history.json")
LEDGER_FILE = signal_ledger.LEDGER_FILE


def _num(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def load_settled_signals() -> list[dict[str, Any]]:
    """Canonical ledger projection merged with legacy signal_history.json rows."""
    canonical = signal_ledger.project_signals(ledger_file=LEDGER_FILE)
    legacy = read_json(LEGACY_HISTORY_FILE, [])
    if not isinstance(legacy, list):
        legacy = []
    return signal_ledger.merge_legacy_signals(canonical, legacy)


def _is_t1_observed(record: Mapping[str, Any]) -> bool:
    """T+1 收益已发生（含 provisional，即尚未到 T+3 终结算的记录）。"""
    return _num(record.get("t1_close_ret")) is not None


def _is_final(record: Mapping[str, Any]) -> bool:
    """终结算口径，对齐 performance_tracker.compute_stats 的 gating 判定：
    已有 t1_close_ret 且 settlement_status != "provisional"。"""
    return _is_t1_observed(record) and record.get("settlement_status") != "provisional"


T1_COHORT_LABEL = "t1_observed_includes_provisional"
T3_COHORT_LABEL = "final_only"


def _return_stats(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"median": None, "mean": None, "win_rate": None}
    wins = sum(1 for v in values if v >= 0)
    return {
        "median": round(median(values), 2),
        "mean": round(mean(values), 2),
        "win_rate": round(wins / len(values) * 100, 1),
    }


def _bucket_summary(
    records: Sequence[Mapping[str, Any]],
    *,
    min_samples: int,
) -> dict[str, Any]:
    """One bucket's T+1/T+3 return distributions plus sample-size gating.

    Cohort discipline: T+1 stats include provisional rows (the T+1 return has
    already happened, so the observation is valid), explicitly labeled; T+3
    stats use only final settlements so immature samples never inflate the
    horizon distribution. Values go through _num so dirty rows (strings,
    bools) are skipped instead of crashing or miscounting.
    """
    n = len(records)
    t1_values = [v for r in records if (v := _num(r.get("t1_close_ret"))) is not None]
    final_records = [r for r in records if _is_final(r)]
    t3_values = [
        v for r in final_records if (v := _num(r.get("horizon_ret"))) is not None
    ]
    premiums = [v for r in records if (v := _num(r.get("t1_open_premium"))) is not None]
    summary: dict[str, Any] = {
        "sample_count": n,
        "final_sample_count": len(final_records),
        "insufficient_sample": n < min_samples,
        "t1_cohort": T1_COHORT_LABEL,
        "t1_close_ret": _return_stats(t1_values),
        "t3_cohort": T3_COHORT_LABEL,
        "t3_horizon_ret": _return_stats(t3_values),
        "t3_sample_count": len(t3_values),
    }
    if premiums:
        summary["auction_premium"] = _return_stats(premiums)
    return summary


def _temperature_tier(record: Mapping[str, Any]) -> str | None:
    context = record.get("selection_context")
    if not isinstance(context, Mapping):
        return None
    timing = context.get("market_timing")
    if not isinstance(timing, Mapping):
        return None
    tier = timing.get("tier")
    return str(tier) if tier else None


def _temperature_bucket_label(tier: str) -> str | None:
    if tier in TEMPERATURE_STRONG_TIERS:
        return "强"
    if tier in TEMPERATURE_MID_TIERS:
        return "中"
    if tier in TEMPERATURE_WEAK_TIERS:
        return "弱"
    return None


def dimension_market_temperature(
    records: Sequence[Mapping[str, Any]],
    *,
    min_samples: int,
) -> dict[str, Any]:
    """市场温度分层：signal 记录里的 selection_context.market_timing.tier。

    缺 tier 的信号不静默丢弃(那会引入选择偏差——走完整流水线的信号可能系统性
    更好)，而是归入 "unknown" 桶一并展示，覆盖率字段保留。
    """
    tagged = [(r, _temperature_tier(r)) for r in records]
    with_tier = [(r, tier) for r, tier in tagged if tier]
    if not with_tier:
        return {
            "status": "unavailable",
            "reason": (
                "selection_context.market_timing.tier 在已结算记录中全部缺失。"
                "该字段仅在信号经过完整候选发现流水线(candidate_discovery/"
                "hot_money_selection)时才会写入 selection_context 并沉淀到"
                "signal_history；performance_tracker.py --record 等旧口径/"
                "命令行记录不会附带此字段。"
            ),
        }
    buckets: dict[str, list[Mapping[str, Any]]] = {
        "强": [], "中": [], "弱": [], "unknown": [],
    }
    unclassified_tiers: set[str] = set()
    for record, tier in tagged:
        if tier is None:
            buckets["unknown"].append(record)
            continue
        label = _temperature_bucket_label(tier)
        if label is None:
            unclassified_tiers.add(tier)
            buckets["unknown"].append(record)
            continue
        buckets[label].append(record)
    result: dict[str, Any] = {
        "status": "ok",
        "field_source": "selection_context.market_timing.tier",
        "tier_groups": {
            "强": list(TEMPERATURE_STRONG_TIERS),
            "中": list(TEMPERATURE_MID_TIERS),
            "弱": list(TEMPERATURE_WEAK_TIERS),
            "unknown": ["缺失或无法归类的 tier"],
        },
        "coverage": {
            "with_tier": len(with_tier),
            "without_tier": len(records) - len(with_tier),
            "total_settled": len(records),
        },
        "buckets": {
            label: _bucket_summary(recs, min_samples=min_samples)
            for label, recs in buckets.items()
        },
    }
    if unclassified_tiers:
        result["unclassified_tiers_seen"] = sorted(unclassified_tiers)
    return result


def dimension_ladder_height(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """主题阶段代理(连板梯队高度)：确认历史记录中不存在，诚实降级。"""
    return {
        "status": "unavailable",
        "reason": (
            "lianban_ladder 仅作为当日缓存存在于 "
            "skills/common/signal_context.py 的 signal_context.json，"
            "每个交易日被覆写，从未按 signal_date 持久化到已结算记录；"
            "selection_context 中也不含逐信号的连板高度数值字段。"
            "没有可靠的历史 join key，因此不编造该维度的分层。"
        ),
    }


def dimension_board_level(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """板位分层(首板/二板/三板+)：确认历史记录中不存在真实数值字段，诚实降级。"""
    return {
        "status": "unavailable",
        "reason": (
            "signal_history/signal_ledger 记录不写入标的自身的连板数(板位)字段。"
            "唯一可关联的是 strategy_id 里的 pattern 枚举"
            "(first_board_reseal≈首板, second_board_weak_to_strong≈二板)，"
            "但这与'入场模式分层'维度是同一份数据，伪装成独立的板位数值分层"
            "会造成虚假的交叉验证；且枚举里没有三板+模式，无法覆盖高位板。"
            "因此不编造该维度，改由入场模式维度承载可得的板位信息。"
        ),
    }


def _entry_pattern(record: Mapping[str, Any]) -> str | None:
    strategy_id = str(record.get("strategy_id") or "")
    if strategy_id.startswith("daban:"):
        return strategy_id.split(":", 1)[1]
    return None


def dimension_entry_pattern(
    records: Sequence[Mapping[str, Any]],
    *,
    min_samples: int,
) -> dict[str, Any]:
    """入场模式分层：strategy_id 里的 daban:{pattern} 真实枚举值。"""
    tagged = [(r, _entry_pattern(r)) for r in records]
    with_pattern = [(r, p) for r, p in tagged if p]
    if not with_pattern:
        return {
            "status": "unavailable",
            "reason": (
                "已结算记录中没有 strategy_id 以 'daban:' 前缀标注入场模式的信号"
                "（strategy_id 全部是其他策略或缺失）。"
            ),
        }
    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for record, pattern in with_pattern:
        buckets.setdefault(pattern, []).append(record)
    return {
        "status": "ok",
        "field_source": "strategy_id (daban:{pattern} prefix)",
        "coverage": {
            "with_pattern": len(with_pattern),
            "total_settled": len(records),
        },
        "buckets": {
            pattern: _bucket_summary(recs, min_samples=min_samples)
            for pattern, recs in sorted(buckets.items())
        },
    }


def _is_daban_signal(record: Mapping[str, Any]) -> bool:
    strategy_id = str(record.get("strategy_id") or "")
    return strategy_id.startswith("daban:") or strategy_id == "default"


def build_report(
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    records: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    all_records = list(records) if records is not None else load_settled_signals()
    daban_records = [r for r in all_records if _is_daban_signal(r)]
    t1_observed = [r for r in daban_records if _is_t1_observed(r)]
    final_count = sum(1 for r in t1_observed if _is_final(r))

    if not t1_observed:
        return {
            "schema": SCHEMA,
            "status": "insufficient_data",
            "research_only": True,
            "min_samples": min_samples,
            "note": (
                "尚无已结算的打板类信号(需至少到 T+1)，无法归因。"
                "本报告不编造数据，如实反映当前样本量。"
            ),
            "total_signals": len(all_records),
            "daban_signals": len(daban_records),
            "t1_observed_signals": 0,
            "final_signals": 0,
            "baseline": None,
            "dimensions": {
                "market_temperature": dimension_market_temperature([], min_samples=min_samples),
                "theme_stage_ladder_height": dimension_ladder_height([]),
                "board_level": dimension_board_level([]),
                "entry_pattern": dimension_entry_pattern([], min_samples=min_samples),
            },
        }

    baseline = _bucket_summary(t1_observed, min_samples=min_samples)
    return {
        "schema": SCHEMA,
        "status": "ok",
        "research_only": True,
        "min_samples": min_samples,
        "note": (
            "四个维度独立统计，不做交叉(样本量不支持交叉验证)。"
            "样本数低于 min_samples 的桶标 insufficient_sample=true，不构成结论。"
            "T+1 统计含 provisional 记录(收益已发生)，T+3 统计只用 final 终结算。"
        ),
        "total_signals": len(all_records),
        "daban_signals": len(daban_records),
        "t1_observed_signals": len(t1_observed),
        "final_signals": final_count,
        "baseline": baseline,
        "dimensions": {
            "market_temperature": dimension_market_temperature(t1_observed, min_samples=min_samples),
            "theme_stage_ladder_height": dimension_ladder_height(t1_observed),
            "board_level": dimension_board_level(t1_observed),
            "entry_pattern": dimension_entry_pattern(t1_observed, min_samples=min_samples),
        },
    }


def _fmt_stats(stats: Mapping[str, Any]) -> str:
    if stats.get("median") is None:
        return "N/A"
    return f"median={stats['median']:+.2f}% mean={stats['mean']:+.2f}% win_rate={stats['win_rate']}%"


def format_markdown(report: Mapping[str, Any]) -> str:
    lines = ["# 打板策略归因报告", ""]
    if report.get("status") == "insufficient_data":
        lines.append(report.get("note", "数据不足"))
        return "\n".join(lines)

    lines.append(
        f"T+1 已观察打板类信号: {report['t1_observed_signals']}"
        f"（其中 final 终结算 {report['final_signals']}，总信号 {report['total_signals']}）"
    )
    lines.append("T+1 统计含 provisional（收益已发生）；T+3 统计仅用 final 终结算。")
    baseline = report.get("baseline") or {}
    lines.append("")
    lines.append("## 总体基线")
    lines.append(f"- 样本数: {baseline.get('sample_count')}"
                 f"（final {baseline.get('final_sample_count', 0)}）"
                 + ("（样本不足，仅供观察）" if baseline.get("insufficient_sample") else ""))
    lines.append(f"- T+1 收盘收益: {_fmt_stats(baseline.get('t1_close_ret', {}))}")
    lines.append(f"- T+3 最终收益: {_fmt_stats(baseline.get('t3_horizon_ret', {}))}"
                 f" (final 样本 {baseline.get('t3_sample_count', 0)})")
    if baseline.get("auction_premium"):
        lines.append(f"- 竞价溢价: {_fmt_stats(baseline['auction_premium'])}")

    dims = report.get("dimensions") or {}
    dim_titles = {
        "market_temperature": "市场温度分层",
        "theme_stage_ladder_height": "主题阶段代理(连板梯队高度)分层",
        "board_level": "板位分层",
        "entry_pattern": "入场模式分层",
    }
    for key, title in dim_titles.items():
        dim = dims.get(key) or {}
        lines.append("")
        lines.append(f"## {title}")
        if dim.get("status") == "unavailable":
            lines.append(f"不可用: {dim.get('reason')}")
            continue
        buckets = dim.get("buckets") or {}
        if not buckets:
            lines.append("无分桶数据。")
            continue
        for bucket_name, summary in buckets.items():
            flag = "（样本不足）" if summary.get("insufficient_sample") else ""
            lines.append(
                f"- **{bucket_name}**{flag} 样本={summary['sample_count']}"
                f"（final {summary.get('final_sample_count', 0)}）"
            )
            lines.append(f"  - T+1(含provisional): {_fmt_stats(summary.get('t1_close_ret', {}))}")
            lines.append(
                f"  - T+3(仅final): {_fmt_stats(summary.get('t3_horizon_ret', {}))}"
                f" (样本 {summary.get('t3_sample_count', 0)})"
            )
            if summary.get("auction_premium"):
                lines.append(f"  - 竞价溢价: {_fmt_stats(summary['auction_premium'])}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print JSON report")
    parser.add_argument("--markdown", action="store_true", help="Print markdown table report")
    parser.add_argument("--out", help="Write JSON report to this path")
    parser.add_argument(
        "--min-samples", type=int, default=DEFAULT_MIN_SAMPLES,
        help=f"Minimum bucket sample size before a conclusion is trusted (default {DEFAULT_MIN_SAMPLES})",
    )
    args = parser.parse_args()

    report = build_report(min_samples=args.min_samples)

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)

    if args.markdown:
        print(format_markdown(report))
    elif args.json or not args.out:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
