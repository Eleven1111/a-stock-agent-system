#!/usr/bin/env python3
"""只读真实集合竞价探针；不写交易状态、不发送 Discord、不触发交易。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import skills.common  # noqa: F401,E402
from auction_data_provider import (  # noqa: E402
    fetch_easy_tdx_auction,
    fetch_previous_day_metrics,
)


EXPECTED_TIMEPOINTS = 11


def build_probe_report(codes: list[str], asof: str) -> dict[str, Any]:
    failures: dict[str, str] = {}
    rows = []
    point_count = 0
    minute_slot_count = 0
    auction_success = 0
    trade_ready = 0
    for code in codes:
        series = fetch_easy_tdx_auction(code)
        previous = fetch_previous_day_metrics(code, asof=asof) if series else {}
        if not series:
            failures[code] = "easy_tdx 0x123D 无有效 09:15-09:25 竞价数据"
        else:
            auction_success += 1
        if not previous.get("prev_day_volume"):
            if series:
                failures[code] = (
                    "prev_day_volume 缺失或无效（mootdx 与腾讯历史 K 线均失败）"
                )
        else:
            trade_ready += 1
        points = {str(row.get("t", ""))[:8] for row in series}
        minute_points = {point[:5] for point in points}
        point_count += len(points)
        minute_slot_count += len(minute_points)
        rows.append({
            "code": code,
            "provider": {
                "auction": "easy_tdx_mac_0x123d" if series else None,
                "previous_day_volume": previous.get("prev_day_provider"),
            },
            "timepoints": sorted(points),
            "timepoint_count": len(points),
            "coverage": round(len(minute_points) / EXPECTED_TIMEPOINTS, 4),
            "minute_count": len(minute_points),
            "trade_ready": bool(series and previous.get("prev_day_volume")),
            "prev_day_date": previous.get("prev_day_date"),
            "latest": series[-1] if series else None,
        })
    requested = len(codes)
    return {
        "schema": "auction_data_probe_v1",
        "status": "ok" if trade_ready == requested and requested else "blocked",
        "asof": asof,
        "probed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "request": {
            "codes": codes,
            "count": requested,
            "provider": "easy_tdx_mac_0x123d + easy_tdx_daily→mootdx→tencent_kline",
        },
        "coverage": {
            "auction_code_success_rate": round(auction_success / requested, 4) if requested else 0.0,
            "auction_successful_codes": auction_success,
            "trade_ready_rate": round(trade_ready / requested, 4) if requested else 0.0,
            "trade_ready_codes": trade_ready,
            "requested_codes": requested,
            "observed_timepoint_count": point_count,
            "observed_minute_slot_count": minute_slot_count,
            "expected_timepoints_per_code": EXPECTED_TIMEPOINTS,
        },
        "rows": rows,
        "failures": failures,
        "safety": {
            "discord_sent": False,
            "trade_triggered": False,
            "shortlist_written": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", default="sh600519,sz000001", help="逗号分隔证券代码")
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--output", type=Path, help="可选：保存 JSON 探针记录")
    args = parser.parse_args(argv)
    codes = [code.strip() for code in args.codes.split(",") if code.strip()]
    report = build_probe_report(codes, args.asof)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if report["status"] == "ok" else 75


if __name__ == "__main__":
    sys.exit(main())
