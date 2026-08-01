#!/usr/bin/env python3
"""
缠论结构信号生成器 — 去包含 → 分型 → 笔 → 中枢 → 三买/三卖 → 背驰
==================================================================
把缠论从"画图"升级为"出信号"：fractal_chart.py 只算分型且纯打印，本脚本在同一套
分型逻辑上往下做笔/中枢/三类买卖点/MACD 背驰，输出**结构化 JSON 信号对象**，供
four_dim_scorer 的技术面/择时消费。

纪律红线：本脚本只生成"研究假设"信号。任何信号在通过 chanlun-backtest 的 research_gate
（allowed_in_live_agent=true）之前，下游只能 display-only / 0 权重——闸门由 strategy_registry
统一裁决，不在此处加权。

实现取舍（务实严谨版）：实现笔 + 笔中枢 + 三类买卖点 + MACD 背驰；线段未单独实现
（笔中枢已足以给出三类买卖点）。所有核心步骤为纯函数，可用合成 K 线单测。

分层（2026-08 T1 升级）：去包含/分型/笔已拆到同目录 chan_kline.py，算法规格对齐 chan.py
（分型 4 档有效性检查 + 笔三条件 + 虚笔 is_sure）。本文件退化为门面 + 中枢/买卖点/背驰，
输出契约向后兼容：analyze() 旧字段全部保留，strokes 仅**新增** is_sure（False = 末段未确认笔）。

数据源（CLI）：腾讯前复权日线（经 common/a_stock_http，cron-safe）。analyze() 为纯函数，不触网。

Usage:
  python3 chan_structure.py 600519 贵州茅台 --json
  python3 chan_structure.py 000001 --days 120
"""

import importlib.util
import json
import os
import sys
from datetime import date
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "common"))
from indicators import macd_hist  # noqa: E402  技术指标统一走 common（去重）

try:  # 调用方（four_dim_scorer / chan_signal_backtest / CLI）已把本目录放进 sys.path
    import chan_kline  # noqa: E402
except ImportError:  # 被 importlib 按路径加载时（测试）本目录不在 sys.path，按文件名加载
    _KLINE_SPEC = importlib.util.spec_from_file_location(
        "chan_kline", os.path.join(os.path.dirname(os.path.abspath(__file__)), "chan_kline.py"))
    chan_kline = importlib.util.module_from_spec(_KLINE_SPEC)
    _KLINE_SPEC.loader.exec_module(chan_kline)

# strategy_id 命名（与 research_gate / strategy_registry 对齐）
SIGNAL_STRATEGY = {
    "third_buy": "chanlun_third_buy",
    "third_sell": "chanlun_third_sell",
    "top_divergence": "chanlun_top_divergence",
    "bottom_divergence": "chanlun_bottom_divergence",
}

DEFAULT_MIN_STROKE_GAP = 4   # 一笔至少跨 4 根去包含K线（经典"5根K线一笔"的简化）

# 去包含 / 分型 由 chan_kline 提供（同名同契约，旧调用方无需改动）
merge_klines = chan_kline.merge_klines
find_fractals = chan_kline.find_fractals


def bi_config(min_gap: int = DEFAULT_MIN_STROKE_GAP) -> "chan_kline.BiConfig":
    """把旧参数 min_gap 映射到 chan_kline 的笔配置：>=4 走严格笔（跨度>=4），否则走宽松笔。"""
    return chan_kline.BiConfig(fx_check="strict", is_strict=min_gap >= 4)


# ========== 中枢 ==========

def build_centers(strokes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """连续≥3笔的重叠区间构成中枢；后续笔与区间重叠则延伸。"""
    centers = []
    n = len(strokes)
    i = 0
    while i + 2 < n:
        window = strokes[i:i + 3]
        zg = min(s["high"] for s in window)
        zd = max(s["low"] for s in window)
        if zg > zd:
            j = i + 3
            while j < n and strokes[j]["low"] <= zg and strokes[j]["high"] >= zd:
                j += 1
            centers.append({
                "zg": round(zg, 3), "zd": round(zd, 3),
                "start_stroke": i, "end_stroke": j - 1,
                "start_idx": strokes[i]["start_idx"], "end_idx": strokes[j - 1]["end_idx"],
                "stroke_count": j - i,
            })
            i = j
        else:
            i += 1
    return centers


# ========== 三类买卖点 ==========

def detect_third_signals(strokes: List[Dict[str, Any]],
                         centers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """基于最后一个中枢判定三买/三卖：离开中枢后回踩不破上沿(三买)/反弹不过下沿(三卖)。"""
    if not centers:
        return []
    c = centers[-1]
    after = strokes[c["end_stroke"] + 1:]
    signals = []
    for k in range(len(after) - 1):
        leave, pull = after[k], after[k + 1]
        if leave["dir"] == "up" and leave["high"] > c["zg"] and \
           pull["dir"] == "down" and pull["low"] > c["zg"]:
            signals.append({
                "type": "third_buy", "idx": pull["end_idx"], "price": pull["end_price"],
                "detail": f"三买:回踩{pull['low']:.2f}未破中枢上沿ZG={c['zg']:.2f}",
            })
        elif leave["dir"] == "down" and leave["low"] < c["zd"] and \
                pull["dir"] == "up" and pull["high"] < c["zd"]:
            signals.append({
                "type": "third_sell", "idx": pull["end_idx"], "price": pull["end_price"],
                "detail": f"三卖:反弹{pull['high']:.2f}未过中枢下沿ZD={c['zd']:.2f}",
            })
    return signals


# ========== 背驰（MACD 柱面积）==========

def _stroke_macd_area(stroke: Dict[str, Any], hist: List[Optional[float]]) -> float:
    lo, hi = stroke["start_idx"], stroke["end_idx"]
    return sum(abs(hist[j]) for j in range(lo, hi + 1)
               if 0 <= j < len(hist) and hist[j] is not None)


def detect_divergence(strokes: List[Dict[str, Any]],
                      hist: List[Optional[float]]) -> List[Dict[str, Any]]:
    """比较最近两段同向笔的 MACD 柱面积：价格更极端但动能更弱 → 背驰。"""
    if len(strokes) < 3:
        return []
    last = strokes[-1]
    prev = next((strokes[i] for i in range(len(strokes) - 2, -1, -1)
                 if strokes[i]["dir"] == last["dir"]), None)
    if prev is None:
        return []
    area_last = _stroke_macd_area(last, hist)
    area_prev = _stroke_macd_area(prev, hist)
    if area_prev <= 0:
        return []
    if last["dir"] == "up" and last["high"] > prev["high"] and area_last < area_prev:
        return [{"type": "top_divergence", "idx": last["end_idx"], "price": last["end_price"],
                 "detail": f"顶背驰:价创新高但MACD动能{area_last:.1f}<{area_prev:.1f}"}]
    if last["dir"] == "down" and last["low"] < prev["low"] and area_last < area_prev:
        return [{"type": "bottom_divergence", "idx": last["end_idx"], "price": last["end_price"],
                 "detail": f"底背驰:价创新低但MACD动能{area_last:.1f}<{area_prev:.1f}"}]
    return []


# ========== 主入口（纯函数）==========

def analyze(bars: List[Dict[str, Any]], min_gap: int = DEFAULT_MIN_STROKE_GAP) -> Dict[str, Any]:
    """对一组K线做完整缠论结构分析。bars 按时间正序，含 high/low/close（可含 date）。"""
    if len(bars) < 5:
        return {"ok": False, "reason": "K线不足", "signals": [], "structure": {}}

    closes = [b["close"] for b in bars]
    hist = macd_hist(closes) if len(closes) >= 26 else [None] * len(closes)
    kline = chan_kline.analyze_klines(bars, bi_config(min_gap))
    merged, fractals = kline["merged"], kline["fractals"]
    strokes = kline["bis"]          # 含末段虚笔（is_sure=False），供下游按需过滤
    centers = build_centers(strokes)

    raw_signals = detect_third_signals(strokes, centers) + detect_divergence(strokes, hist)
    # 回填日期 + strategy_id
    signals = []
    for s in raw_signals:
        idx = s["idx"]
        s["date"] = bars[idx].get("date") if 0 <= idx < len(bars) else None
        s["strategy_id"] = SIGNAL_STRATEGY.get(s["type"])
        signals.append(s)

    last_center = centers[-1] if centers else None
    return {
        "ok": True,
        "last_close": round(closes[-1], 3),
        "structure": {
            "merged_count": len(merged),
            "fractal_count": len(fractals),
            "stroke_count": len(strokes),
            "sure_stroke_count": sum(1 for s in strokes if s.get("is_sure")),
            "center_count": len(centers),
            "last_center": last_center,
            "last_stroke": strokes[-1] if strokes else None,
        },
        "signals": signals,
        "summary": _summary(strokes, centers, signals),
    }


def _summary(strokes, centers, signals) -> str:
    parts = [f"笔{len(strokes)}/中枢{len(centers)}"]
    if centers:
        c = centers[-1]
        parts.append(f"最新中枢[{c['zd']:.2f},{c['zg']:.2f}]")
    if signals:
        parts.append("信号:" + ",".join(s["type"] for s in signals))
    else:
        parts.append("无三类买卖点/背驰")
    return " | ".join(parts)


# ========== CLI（取数后调 analyze）==========

def _fetch_bars(code: str, days: int) -> List[Dict[str, Any]]:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "common"))
    from a_stock_http import fetch_tencent_kline  # noqa: E402
    market = "sh" if str(code).startswith(("6", "9")) else "sz"
    return fetch_tencent_kline(code, market, days=days, ktype="day")


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="缠论结构信号生成器")
    parser.add_argument("code", help="股票代码")
    parser.add_argument("name", nargs="?", default="", help="股票名称")
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--min-gap", type=int, default=DEFAULT_MIN_STROKE_GAP)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        bars = _fetch_bars(args.code, args.days)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "reason": f"取数失败: {e}"}, ensure_ascii=False))
        return 1

    result = analyze(bars, args.min_gap)
    result.update({"code": str(args.code), "name": args.name, "asof": date.today().isoformat()})

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"## 缠论结构 — {args.name or args.code}")
        print(result.get("summary", ""))
        for s in result.get("signals", []):
            print(f"  · {s['type']} @ {s.get('date')} {s['price']} — {s['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
