#!/usr/bin/env python3
"""结算样本量只读诊断 —— 部署机上跑，回答"回测校准现在够不够数据"。

宿主机建议第 6 条（历史回测与实时校准）的前置问题。本机（开发机）看不到生产
状态目录，必须在部署机上执行。

**只读**：不改动任何数据文件、不触网、不改状态，可安全反复运行。
（读取经 ``state_store`` 的文件锁——生产机上 cron 正在写这些文件，无锁读会
读到写一半的内容——因此会留下 ``.lock`` 边车文件，这是仓库所有读路径的常规
行为，不是本脚本的额外副作用。）

判据要点（第一版曾在此处出错，这里写明避免重蹈）：

- **不能拿"已结算记录总数"比门槛**。``candidate_lifecycle`` 每天会把当日
  *全部* evaluated 候选入队（含落选的），结算也会覆盖它们，所以总数被落选
  候选主导——2026-08-25 实测 48855 条已结算里，真正通过 discovery 的只有
  3262 条，通过 auction_shortlist 的只有 33 条。按总数判定会得出"样本充足"
  的相反结论。判定必须落在 ``passed_stages`` 的分阶段样本上。
- **研究层与执行层要分开判**。"某板块是不是当天主线"用 discovery 层样本即可
  验证，不需要真的交易过；"打板/条件交易有没有 edge"才需要执行层样本。两者
  样本量能差两个数量级，合并成一个结论会同时误导两边。
- **"还没到结算日"不是"漏结算"**。t3 要 3 个交易日，最近几天没结算是正常
  的；只有已过期仍未结算才是故障。

用法：
    python scripts/diagnose_settlement_samples.py
    python scripts/diagnose_settlement_samples.py --json
    python scripts/diagnose_settlement_samples.py --asof 2026-08-25   # 固定基准日
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
try:
    from ._repo_bootstrap import ensure_repo_importable  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - 直接执行时走这条
    from _repo_bootstrap import ensure_repo_importable  # type: ignore[no-redef]

ensure_repo_importable(ROOT)
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

import lifecycle_analytics  # noqa: E402
import signal_ledger  # noqa: E402
from a_share_rules import add_trading_days, is_trading_day  # noqa: E402
from paths import hermes_home  # noqa: E402

# t3 结算需要 3 个交易日；未满不算漏结算。
SETTLEMENT_HORIZON_DAYS = 3
# 经验门槛：把样本量翻译成"能不能做什么"，不是统计学定论。
MIN_SAMPLES_FOR_IC = 30
MIN_DAYS_FOR_REGIME_SPLIT = 40

# 研究层只需候选进入观察池即可验证；执行层要求真正走完收口。
RESEARCH_STAGE = "discovery"
EXECUTION_STAGES = ("auction_shortlist", "open_confirmed")


def _per_day_counts(days: Sequence[str]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for asof in days:
        records = lifecycle_analytics.load_day(asof).get("records") or []
        counts[asof] = {
            "records": len(records),
            "settled": sum(1 for r in records if (r.get("outcome") or {}).get("resolved")),
        }
    return counts


def _calendar_gaps(days: Sequence[str]) -> dict[str, Any]:
    """区间内应有而完全没有 lifecycle 文件的交易日（与"有文件但零记录"不同）。"""
    if not days:
        return {"missing_days": [], "calendar_status": "no_data"}
    try:
        start = date.fromisoformat(days[0])
        end = date.fromisoformat(days[-1])
        present = set(days)
        missing = []
        cursor = start
        while cursor <= end:
            if is_trading_day(cursor) and cursor.isoformat() not in present:
                missing.append(cursor.isoformat())
            cursor += timedelta(days=1)
    except (ValueError, RuntimeError) as exc:
        # 日历未覆盖该年份时 fail-closed：报不出来就说报不出来，不猜。
        return {"missing_days": [], "calendar_status": f"unavailable: {exc}"}
    return {"missing_days": missing, "calendar_status": "ok"}


def _settlement_lag(
    per_day: Mapping[str, Mapping[str, int]], asof_today: str,
) -> dict[str, list[str]]:
    """把"有候选但零结算"拆成待结算（正常）与逾期未结算（故障）。"""
    pending: list[str] = []
    overdue: list[str] = []
    for asof, counts in sorted(per_day.items()):
        if counts["records"] == 0 or counts["settled"] > 0:
            continue
        try:
            due = add_trading_days(asof, SETTLEMENT_HORIZON_DAYS).isoformat()
        except (ValueError, RuntimeError):
            overdue.append(asof)
            continue
        (overdue if due <= asof_today else pending).append(asof)
    return {"pending_settlement_days": pending, "overdue_settlement_days": overdue}


def _stage_counts(settled: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in settled:
        for stage in lifecycle_analytics.passed_stages(record):
            counts[stage] = counts.get(stage, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _layer(name: str, stage: str, samples: int) -> dict[str, Any]:
    return {
        "layer": name,
        "binding_stage": stage,
        "samples": samples,
        "threshold": MIN_SAMPLES_FOR_IC,
        "sufficient": samples >= MIN_SAMPLES_FOR_IC,
    }


def _verdict(stage_counts: Mapping[str, int], day_count: int) -> dict[str, Any]:
    research = _layer("research", RESEARCH_STAGE, stage_counts.get(RESEARCH_STAGE, 0))
    exec_stage = min(EXECUTION_STAGES, key=lambda s: stage_counts.get(s, 0))
    execution = _layer("execution", exec_stage, stage_counts.get(exec_stage, 0))

    limitations = []
    if day_count < MIN_DAYS_FOR_REGIME_SPLIT:
        limitations.append(
            f"覆盖交易日 {day_count} < {MIN_DAYS_FOR_REGIME_SPLIT}："
            "不足以按市场环境分组校准权重（不影响单组 IC）"
        )
    if not execution["sufficient"]:
        limitations.append(
            f"执行层最窄阶段 {exec_stage} 仅 {execution['samples']} 条："
            "任何「打板/条件交易有无 edge」的结论都无统计意义，"
            "local_theme_conditional_trade_enabled 应保持关闭"
        )

    if research["sufficient"]:
        action = (
            "研究层样本充足，可开始校准板块/主线判断指标；"
            "执行层继续 shadow 直到样本量上来"
            if not execution["sufficient"]
            else "两层样本均充足，可开始完整校准"
        )
    else:
        action = "研究层样本不足，先确认 candidate-discovery 在部署机上按日产出"

    return {
        "research_layer": research,
        "execution_layer": execution,
        "limitations": limitations,
        "next_action": action,
    }


def collect(asof_today: str | None = None) -> dict[str, Any]:
    today = asof_today or date.today().isoformat()
    days = lifecycle_analytics.available_days()
    settled = lifecycle_analytics.load_settled_records()
    per_day = _per_day_counts(days)
    stage_counts = _stage_counts(settled)

    try:
        ledger_events = signal_ledger.read_events()
        ledger_error = None
    except (OSError, RuntimeError, ValueError) as exc:
        ledger_events = []
        ledger_error = str(exc)

    empty_days = [asof for asof, c in per_day.items() if c["records"] == 0]
    return {
        "schema": "settlement_sample_diagnosis_v2",
        "asof": today,
        "state_home": hermes_home(),
        "lifecycle_day_count": len(days),
        "lifecycle_date_range": [days[0], days[-1]] if days else None,
        "total_records": sum(c["records"] for c in per_day.values()),
        # 保留但显式标注：这个数被落选候选主导，不可用于判定样本是否充足。
        "settled_records_including_rejected": len(settled),
        "settled_by_stage": stage_counts,
        "stage_adequacy": {
            stage: {"samples": count, "sufficient_for_ic": count >= MIN_SAMPLES_FOR_IC}
            for stage, count in stage_counts.items()
        },
        "empty_day_count": len(empty_days),
        "empty_days": sorted(empty_days),
        **_calendar_gaps(days),
        **_settlement_lag(per_day, today),
        "outcome_field_coverage": {
            key: sum(1 for r in settled if lifecycle_analytics.outcome_value(r, key) is not None)
            for key in ("t1_open_ret", "t1_close_ret", "t3_close_ret")
        },
        "ledger_event_count": len(ledger_events),
        "ledger_error": ledger_error,
        "verdict": _verdict(stage_counts, len(days)),
    }


def _render_layer(layer: Mapping[str, Any]) -> str:
    mark = "✅" if layer["sufficient"] else "❌"
    return (
        f"  {mark} {layer['layer']:9s} 绑定阶段={layer['binding_stage']:20s}"
        f" 样本={layer['samples']:<6d} 门槛={layer['threshold']}"
    )


def _render(report: dict[str, Any]) -> str:
    date_range = report["lifecycle_date_range"]
    lines = [
        "## 结算样本诊断（只读）",
        f"状态目录：{report['state_home']}｜基准日：{report['asof']}",
        "",
        f"lifecycle 覆盖交易日：{report['lifecycle_day_count']}"
        + (f"（{date_range[0]} → {date_range[1]}）" if date_range else "（无数据）"),
        f"候选记录总数：{report['total_records']}",
        f"已结算记录数（含落选候选，**不可据此判定样本充足**）："
        f"{report['settled_records_including_rejected']}",
        f"ledger 事件数：{report['ledger_event_count']}",
    ]
    if report["ledger_error"]:
        lines.append(f"⚠️ ledger 读取失败：{report['ledger_error']}")

    lines.append("")
    lines.append("各阶段通过的已结算样本（判定依据）：")
    for stage, info in report["stage_adequacy"].items():
        mark = "✅" if info["sufficient_for_ic"] else "⚠️"
        lines.append(f"  {mark} {stage}: {info['samples']}")

    lines.append("")
    lines.append("分层结论：")
    lines.append(_render_layer(report["verdict"]["research_layer"]))
    lines.append(_render_layer(report["verdict"]["execution_layer"]))

    gaps = []
    if report["missing_days"]:
        gaps.append(f"⚠️ 区间内完全缺失 lifecycle 文件的交易日 {len(report['missing_days'])} 天："
                    f"{report['missing_days']}")
    if report["empty_day_count"]:
        gaps.append(f"⚠️ 有文件但零记录 {report['empty_day_count']} 天：{report['empty_days']}")
    if report["overdue_settlement_days"]:
        gaps.append(f"❌ 逾期未结算（已过 t+{SETTLEMENT_HORIZON_DAYS} 交易日）："
                    f"{report['overdue_settlement_days']}")
    if report["pending_settlement_days"]:
        gaps.append(f"ℹ️ 待结算（尚未到 t+{SETTLEMENT_HORIZON_DAYS}，正常）："
                    f"{report['pending_settlement_days']}")
    if report["calendar_status"] != "ok":
        gaps.append(f"⚠️ 交易日历不可用，缺失日无法判定：{report['calendar_status']}")
    if gaps:
        lines.extend(["", *gaps])

    lines.extend([
        "",
        "结算字段覆盖：" + "、".join(
            f"{k}={v}" for k, v in report["outcome_field_coverage"].items()
        ),
    ])
    if report["verdict"]["limitations"]:
        lines.append("")
        lines.append("限制：")
        lines.extend(f"  - {item}" for item in report["verdict"]["limitations"])
    lines.extend(["", f"下一步：{report['verdict']['next_action']}"])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--asof", help="基准日（默认今天），用于判定结算是否逾期")
    args = parser.parse_args()
    report = collect(args.asof)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else _render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
