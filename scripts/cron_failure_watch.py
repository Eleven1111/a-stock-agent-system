#!/usr/bin/env python3
"""集合竞价链路失败汇总 watchdog。

本机（na）的调度器用 ``Popen(start_new_session=True)`` fire-and-forget 起作业，
没有任何消费者读子进程退出码，作业日志也无人消费。因此"链路某一环挂了"在这台
机器上是完全不可见的 —— issue #159 的当日事故就是这样静默过去的。失败可见性的
SSOT 只能是 artifact：本脚本读当日竞价链各作业最新 artifact 的 ``status``，把
异常汇总成一条可推送的文案，与调度器实现完全解耦，两条部署拓扑共用一份代码。

两类异常必须分开报，运维动作完全不同：

- ``missing``：当日压根没有 artifact。多半是 cron 没触发 —— Mac 睡眠错过 launchd
  心跳在本机是常态（``StartInterval`` 不补发错过的心跳），要查调度器。
- ``failed``/``timeout``/``blocked``：作业触发了但没跑通，要查 artifact 与
  ``$A_STOCK_STATE_HOME/cron/dispatch-jobs.log``。

全绿时不输出一个字，配合 manifest 的 ``silent_when_no_signal`` 不产生推送。
脚本自身永远退出 0：告警是它的正常产出，不是它自己失败。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional, Sequence

import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

import runtime_context  # noqa: E402

REPORT_SCHEMA = "a_stock_cron_failure_watch_v1"

# 竞价链：盘前候选池 → 竞价快照 → 全市场扫描 → 收口 → 简报。
AUCTION_CHAIN_JOBS = (
    "candidate-preopen",
    "auction-snapshot",
    "auction-market-snapshot",
    "auction-finalize",
    "auction-intelligence-brief",
)

# 跑到终态且不需要人介入的状态。auction-snapshot 每分钟触发，末次 attempt 常常
# 是 duplicate_skipped；日历/退避跳过同样是设计内行为，都不算故障。
HEALTHY_STATUSES = frozenset({
    "ok",
    "duplicate_skipped",
    "skipped_adaptive_backoff",
    "skipped_non_trading_day",
})


def inspect_job(job_id: str, trading_date: str) -> Dict[str, Any]:
    """把一个作业当日的最新 artifact 归类为 ok / missing / failed。"""
    artifact = runtime_context.load_latest_artifact(job_id, trading_date=trading_date)
    if not artifact:
        return {"job_id": job_id, "state": "missing", "status": None, "finished_at": None}
    status = str(artifact.get("status") or "unknown")
    return {
        "job_id": job_id,
        "state": "ok" if status in HEALTHY_STATUSES else "failed",
        "status": status,
        "finished_at": artifact.get("finished_at"),
        "artifact_path": artifact.get("artifact_path"),
    }


def scan(
    *,
    trading_date: Optional[str] = None,
    job_ids: Sequence[str] = AUCTION_CHAIN_JOBS,
) -> Dict[str, Any]:
    day = runtime_context.resolve_trading_date(trading_date)
    checked = [inspect_job(job_id, day) for job_id in job_ids]
    missing = [item for item in checked if item["state"] == "missing"]
    failed = [item for item in checked if item["state"] == "failed"]
    return {
        "schema": REPORT_SCHEMA,
        "trading_date": day,
        "status": "alert" if (missing or failed) else "ok",
        "checked": checked,
        "missing": missing,
        "failed": failed,
    }


def _healthy_job_ids(report: Dict[str, Any]) -> List[str]:
    return [item["job_id"] for item in report["checked"] if item["state"] == "ok"]


def render_text(report: Dict[str, Any]) -> str:
    """全绿返回空串（不推送）；否则返回按运维动作分组的告警文案。"""
    if report["status"] != "alert":
        return ""
    lines = [f"⚠️ 集合竞价链路异常 | {report['trading_date']}"]
    if report["missing"]:
        lines.append(
            f"未触发 {len(report['missing'])} 个"
            "（当日无 artifact，疑似错过 cron 触发：Mac 睡眠 / 调度器未运行）"
        )
        lines.extend(f"  - {item['job_id']}" for item in report["missing"])
    if report["failed"]:
        lines.append(
            f"执行失败 {len(report['failed'])} 个"
            "（已触发但未跑通，查 artifact 与 cron/dispatch-jobs.log）"
        )
        lines.extend(
            f"  - {item['job_id']} status={item['status']} finished_at={item['finished_at']}"
            for item in report["failed"]
        )
    healthy = _healthy_job_ids(report)
    if healthy:
        lines.append(f"正常 {len(healthy)} 个：{'、'.join(healthy)}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="集合竞价链路失败汇总 watchdog")
    parser.add_argument("--trading-date", help="默认取最近交易日")
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出完整报告（含全绿明细）；cron 走文本模式，靠空输出实现静默",
    )
    args = parser.parse_args(argv)

    report = scan(trading_date=args.trading_date)
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
        return 0
    text = render_text(report)
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
