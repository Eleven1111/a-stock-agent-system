#!/usr/bin/env python3
"""
集合竞价采集器 — 9:15-9:25 真竞价微观结构因子
=================================================
腾讯 qt.gtimg.cn 全天候免费、不受 ClashX TUN 影响，且其报文 parts[9..28] 自带
五档盘口（原 data_sources.md 漏记）。竞价期间这五档反映累积委买/委卖，是免费源
最接近 L2 的竞价信号。

本采集器把单一手填的 `auction_gap_pct` 升级为 6 个可审计的真竞价因子，
输出可直接并入 daban_candidate_api 的候选字段，不替代回测闸门，不自动下单。

工作流（cron 9:15-9:25 每隔 ~10s）：
  python auction_collector.py --codes sh600519,sz002156 --snapshot   # 多次，累积快照
  python auction_collector.py --codes sh600519,sz002156 --finalize --json  # 9:25 收口算因子

即时/调试：
  python auction_collector.py --codes sh600519 --once --json   # 单次抓取+计算（不落盘）
  python auction_collector.py --example --json                  # 合成竞价数据演示
"""

import argparse
import json
import os
import sys
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "common"))
from a_stock_http import fetch_tencent_snapshot, DataSourceError  # noqa: E402
from tradeability import limit_pct, round_limit  # noqa: E402
from state_store import mutate_json, read_json  # noqa: E402
from paths import data_file  # noqa: E402

AUCTION_OPEN_FREEZE = "09:20"  # 9:20 后委托不可撤单，9:20→9:25 委买净增 = 无撤单窗口真实意图


def _sum_vol(levels: List[Tuple[Optional[float], Optional[float]]]) -> float:
    return sum(v for _, v in levels if v)


def _snapshot_bid_vol(snap: Dict[str, Any]) -> float:
    return _sum_vol(snap.get("bids", []))


def _net_bid_delta(snapshots: List[Dict[str, Any]]) -> Optional[float]:
    """9:20→9:25 委买净增量（手）。需 >=2 个快照且跨越 9:20，否则 None。"""
    post_freeze = [s for s in snapshots if str(s.get("t", "")) >= AUCTION_OPEN_FREEZE]
    if len(post_freeze) < 2:
        return None
    return round(_snapshot_bid_vol(post_freeze[-1]) - _snapshot_bid_vol(post_freeze[0]), 1)


def compute_auction_factors(snapshots: List[Dict[str, Any]], code: str, name: str = "") -> Dict[str, Any]:
    """从一只票的竞价快照序列算出 6 个真竞价因子（纯函数，不触网）。"""
    if not snapshots:
        return {"code": code, "name": name, "error": "无竞价快照"}
    last = snapshots[-1]
    price = last.get("price")
    prev_close = last.get("prev_close")
    if price is None or prev_close in (None, 0):
        return {"code": code, "name": name, "error": "缺少现价/昨收，无法计算竞价因子"}

    bid_vol = _snapshot_bid_vol(last)
    ask_vol = _sum_vol(last.get("asks", []))
    best_bid_vol = (last.get("bids") or [(None, None)])[0][1] or 0.0
    volume = last.get("volume") or 0.0           # 竞价累计成交量（手）
    market_cap_yi = last.get("market_cap")       # 流通市值（亿元）

    gap_pct = round((price - prev_close) / prev_close * 100, 2)
    limit_up = round_limit(prev_close, limit_pct(code, name), up=True)
    at_limit = price >= limit_up - 1e-6

    if at_limit and ask_vol == 0:
        board_status = "yizi_seal"          # 竞价一字封死
    elif at_limit:
        board_status = "limit_up_with_ask"  # 竞价上板但有卖盘（T字/可撬）
    elif gap_pct > 0:
        board_status = "high_open"
    else:
        board_status = "flat_or_low_open"

    seal_ratio_pct = None
    if at_limit and market_cap_yi:
        seal_ratio_pct = round(best_bid_vol * 100 * price / (market_cap_yi * 1e8) * 100, 3)

    return {
        "code": code,
        "name": name,
        "auction_gap_pct": gap_pct,
        "auction_volume": round(volume, 1),
        "auction_amount": round(price * volume * 100, 0),
        "auction_bid_ask_ratio": round(bid_vol / ask_vol, 2) if ask_vol else None,
        "auction_net_bid_delta": _net_bid_delta(snapshots),
        "board_status": board_status,
        "seal_amount_ratio_pct": seal_ratio_pct,
        "snapshots_used": len(snapshots),
        "is_yiziban": board_status == "yizi_seal",
    }


def take_snapshot(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """抓一次腾讯实时+五档，打上时间戳。"""
    quotes = fetch_tencent_snapshot(codes)
    now = datetime.now().strftime("%H:%M:%S")
    for q in quotes.values():
        q["t"] = now
    return quotes


def _state_path(asof: str) -> str:
    return data_file("daban-stock-picker", f"auction_{asof}.json")


def append_snapshot(codes: List[str], asof: str) -> Dict[str, Any]:
    """事务式把一次快照追加到当日状态文件（单锁 read-modify-write）。"""
    quotes = take_snapshot(codes)

    def mutator(state: Dict[str, Any]) -> Dict[str, Any]:
        state.setdefault("asof", asof)
        series = state.setdefault("series", {})
        for code, q in quotes.items():
            series.setdefault(code, []).append(q)
        return state

    return mutate_json(_state_path(asof), mutator, default={"asof": asof, "series": {}})


def finalize(asof: str) -> Dict[str, Any]:
    state = read_json(_state_path(asof), default={"series": {}})
    return _build_result(state.get("series", {}), asof)


def _build_result(series: Dict[str, List[Dict[str, Any]]], asof: str) -> Dict[str, Any]:
    factors = [
        compute_auction_factors(snaps, code, (snaps[-1].get("name") if snaps else "") or "")
        for code, snaps in series.items()
    ]
    return {
        "schema": "auction_factors_v1",
        "asof": asof,
        "generated_at": datetime.now().isoformat(),
        "note": "免费腾讯五档竞价因子；撤单率类信号需 L2；阈值须经 chanlun-backtest 验证后方可实盘",
        "factors": factors,
    }


def example_result() -> Dict[str, Any]:
    """合成竞价快照：一字封死 + 高开放量，验证因子计算，无需开盘/触网。"""
    series = {
        "sz002156": [
            {"t": "09:18:00", "name": "通富微电", "price": 11.0, "prev_close": 10.0,
             "volume": 8000, "market_cap": 80.0,
             "bids": [(11.0, 50000), (None, None), (None, None), (None, None), (None, None)],
             "asks": [(None, None)] * 5},
            {"t": "09:24:50", "name": "通富微电", "price": 11.0, "prev_close": 10.0,
             "volume": 12000, "market_cap": 80.0,
             "bids": [(11.0, 90000), (None, None), (None, None), (None, None), (None, None)],
             "asks": [(None, None)] * 5},
        ],
        "sh600111": [
            {"t": "09:21:00", "name": "北方稀土", "price": 21.5, "prev_close": 20.0,
             "volume": 30000, "market_cap": 300.0,
             "bids": [(21.5, 4000), (21.49, 3000), (21.48, 2000), (21.47, 1000), (21.46, 800)],
             "asks": [(21.51, 6000), (21.52, 5000), (21.53, 4000), (21.54, 3000), (21.55, 2000)]},
        ],
    }
    return _build_result(series, "2026-06-04")


def main() -> None:
    parser = argparse.ArgumentParser(description="A股集合竞价真竞价因子采集器")
    parser.add_argument("--codes", help="逗号分隔，带市场前缀，如 sh600519,sz002156")
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--snapshot", action="store_true", help="抓一次快照并落盘（cron 多次调用）")
    parser.add_argument("--finalize", action="store_true", help="读当日快照算因子")
    parser.add_argument("--once", action="store_true", help="单次抓取+计算，不落盘（调试）")
    parser.add_argument("--example", action="store_true", help="合成数据演示，不触网")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",")] if args.codes else []

    if args.example:
        result = example_result()
    elif args.once:
        if not codes:
            parser.error("--once 需要 --codes")
        try:
            series = {code: [snap] for code, snap in take_snapshot(codes).items()}
        except DataSourceError as e:
            print(json.dumps({"error": str(e)}, ensure_ascii=False))
            sys.exit(1)
        result = _build_result(series, args.asof)
    elif args.snapshot:
        if not codes:
            parser.error("--snapshot 需要 --codes")
        try:
            state = append_snapshot(codes, args.asof)
        except DataSourceError as e:
            print(json.dumps({"error": str(e)}, ensure_ascii=False))
            sys.exit(1)
        counts = {c: len(s) for c, s in state.get("series", {}).items()}
        print(json.dumps({"ok": True, "asof": args.asof, "snapshot_counts": counts}, ensure_ascii=False))
        return
    elif args.finalize:
        result = finalize(args.asof)
    else:
        parser.error("需指定 --snapshot / --finalize / --once / --example 之一")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"## 集合竞价因子 | {result['asof']}")
        for f in result["factors"]:
            if f.get("error"):
                print(f"- {f['name']}({f['code']}): {f['error']}")
                continue
            print(f"- {f['name']}({f['code']}): gap={f['auction_gap_pct']}% "
                  f"状态={f['board_status']} 委比={f['auction_bid_ask_ratio']} "
                  f"净委买增={f['auction_net_bid_delta']} 封单/流通={f['seal_amount_ratio_pct']}%")


if __name__ == "__main__":
    main()
