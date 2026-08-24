#!/usr/bin/env python3
"""结算样本量只读诊断 —— 部署机上跑，回答"回测校准现在够不够数据"。

宿主机建议第 6 条（历史回测与实时校准）的前置问题：机器齐全，但样本量未知。
本机（开发机）看不到生产状态目录，必须在部署机上执行。

**只读**：不写任何文件、不触网、不改状态。可安全反复运行。

用法：
    python scripts/diagnose_settlement_samples.py
    python scripts/diagnose_settlement_samples.py --json
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
try:
    from ._repo_bootstrap import ensure_repo_importable  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - 直接执行时走这条
    from _repo_bootstrap import ensure_repo_importable  # type: ignore[no-redef]

ensure_repo_importable(ROOT)
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

import lifecycle_analytics  # noqa: E402
import signal_ledger  # noqa: E402
from paths import hermes_home  # noqa: E402

# 经验门槛，用于把"样本量"翻译成"能不能做什么"。不是统计学定论，
# 只是让报告直接给出可执行判断，而不是丢一个数字让人自己猜。
MIN_SAMPLES_FOR_IC = 30
MIN_SAMPLES_FOR_STAGE_BREAKDOWN = 100
MIN_DAYS_FOR_REGIME_SPLIT = 40


def _verdict(settled: int, days: int) -> dict[str, Any]:
    blockers = []
    if settled < MIN_SAMPLES_FOR_IC:
        blockers.append(
            f"已结算样本 {settled} < {MIN_SAMPLES_FOR_IC}，IC/胜率类结论无统计意义"
        )
    if settled < MIN_SAMPLES_FOR_STAGE_BREAKDOWN:
        blockers.append(
            f"已结算样本 {settled} < {MIN_SAMPLES_FOR_STAGE_BREAKDOWN}，无法做分阶段/分板块拆解"
        )
    if days < MIN_DAYS_FOR_REGIME_SPLIT:
        blockers.append(
            f"覆盖交易日 {days} < {MIN_DAYS_FOR_REGIME_SPLIT}，无法按市场环境分组校准权重"
        )
    return {
        "can_run_calibration": not blockers,
        "blockers": blockers,
        "next_action": (
            "可以开始回测校准"
            if not blockers
            else "先补数据管道：确认 candidate-discovery / 结算作业在部署机上按日产出"
        ),
    }


def collect() -> dict[str, Any]:
    days = lifecycle_analytics.available_days()
    settled = lifecycle_analytics.load_settled_records()

    per_day: dict[str, dict[str, int]] = {}
    for asof in days:
        records = lifecycle_analytics.load_day(asof).get("records") or []
        per_day[asof] = {
            "records": len(records),
            "settled": sum(1 for r in records if (r.get("outcome") or {}).get("resolved")),
        }

    stage_counts: dict[str, int] = {}
    for record in settled:
        for stage in lifecycle_analytics.passed_stages(record):
            stage_counts[stage] = stage_counts.get(stage, 0) + 1

    outcome_coverage = {
        key: sum(1 for r in settled if lifecycle_analytics.outcome_value(r, key) is not None)
        for key in ("t1_open_ret", "t1_close_ret", "t3_close_ret")
    }

    try:
        ledger_events = signal_ledger.read_events()
        ledger_error = None
    except (OSError, RuntimeError, ValueError) as exc:
        ledger_events = []
        ledger_error = str(exc)

    empty_days = [asof for asof, counts in per_day.items() if counts["records"] == 0]
    unsettled_days = [
        asof for asof, counts in per_day.items()
        if counts["records"] > 0 and counts["settled"] == 0
    ]

    return {
        "schema": "settlement_sample_diagnosis_v1",
        "state_home": hermes_home(),
        "lifecycle_day_count": len(days),
        "lifecycle_date_range": [days[0], days[-1]] if days else None,
        "total_records": sum(c["records"] for c in per_day.values()),
        "settled_records": len(settled),
        "empty_day_count": len(empty_days),
        "empty_days_sample": empty_days[:10],
        "days_with_records_but_no_settlement": unsettled_days[:10],
        "settled_by_stage": dict(sorted(stage_counts.items(), key=lambda kv: -kv[1])),
        "outcome_field_coverage": outcome_coverage,
        "ledger_event_count": len(ledger_events),
        "ledger_error": ledger_error,
        "verdict": _verdict(len(settled), len(days)),
    }


def _render(report: dict[str, Any]) -> str:
    lines = [
        "## 结算样本诊断（只读）",
        f"状态目录：{report['state_home']}",
        "",
        f"lifecycle 覆盖交易日：{report['lifecycle_day_count']}"
        + (f"（{report['lifecycle_date_range'][0]} → {report['lifecycle_date_range'][1]}）"
           if report["lifecycle_date_range"] else "（无数据）"),
        f"候选记录总数：{report['total_records']}",
        f"**已结算记录数：{report['settled_records']}**",
        f"ledger 事件数：{report['ledger_event_count']}",
    ]
    if report["ledger_error"]:
        lines.append(f"⚠️ ledger 读取失败：{report['ledger_error']}")
    if report["empty_day_count"]:
        lines.append(
            f"⚠️ 空白交易日 {report['empty_day_count']} 天，样本：{report['empty_days_sample']}"
        )
    if report["days_with_records_but_no_settlement"]:
        lines.append(
            "⚠️ 有候选但零结算的日子（结算作业可能没跑）："
            f"{report['days_with_records_but_no_settlement']}"
        )
    if report["settled_by_stage"]:
        lines.append("")
        lines.append("各阶段通过的已结算样本：")
        lines.extend(
            f"  - {stage}: {count}" for stage, count in report["settled_by_stage"].items()
        )
    lines.append("")
    lines.append("结算字段覆盖：" + "、".join(
        f"{k}={v}" for k, v in report["outcome_field_coverage"].items()
    ))
    lines.append("")
    verdict = report["verdict"]
    lines.append(f"结论：{'✅ 可以开始校准' if verdict['can_run_calibration'] else '❌ 数据不足'}")
    lines.extend(f"  - {item}" for item in verdict["blockers"])
    lines.append(f"下一步：{verdict['next_action']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    report = collect()
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else _render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
