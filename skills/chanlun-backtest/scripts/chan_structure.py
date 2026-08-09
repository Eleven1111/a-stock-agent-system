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

实现取舍（务实严谨版）：实现笔 + 笔中枢 + 三类买卖点 + MACD 背驰。所有核心步骤为纯函数，
可用合成 K 线单测。

分层：
- 2026-08 T1：去包含/分型/笔拆到同目录 chan_kline.py（分型 4 档有效性检查 + 笔三条件 + 虚笔）；
- 2026-08 T2：线段拆到同目录 chan_segment.py（特征序列分型 + 缺口情形 + 未确认段 is_sure）；
- 2026-08 T3：中枢拆到同目录 chan_center.py（段内构造 + 重叠合并 + bi_in/bi_out），旧的
  "滑窗 3 笔重叠"近似删除；
- 2026-08 T4：买卖点拆到同目录 chan_bsp.py（T1/T1P/T2/T2S/T3A/T3B 全谱系 + 背驰算法族 +
  feature_dict），旧的 detect_third_signals / detect_divergence 删除。

本文件退化为纯门面，输出契约向后兼容：analyze() 旧字段全部保留，只增不删
（last_center 的旧键 zg/zd/start_stroke/end_stroke/start_idx/end_idx/stroke_count 保留，
值会因中枢算法升级而变化）。signals 里旧四类型（third_buy/third_sell/top_divergence/
bottom_divergence）继续产出、strategy_id 不变，另增 bsp_type/is_buy/is_sure/feature_dict/
strategy_id_v2 五个新键；新谱系类型（bsp1p_* / bsp2_* / bsp2s_*）无 legacy strategy_id。

数据源（CLI）：腾讯前复权日线（经 common/a_stock_http，cron-safe）。analyze() 为纯函数，不触网。

Usage:
  python3 chan_structure.py 600519 贵州茅台 --json
  python3 chan_structure.py 000001 --days 120
"""

import importlib.util
import json
import os
from datetime import date
from typing import Any, Dict, List

import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path
from indicators import macd_hist  # noqa: E402  技术指标统一走 common（去重）

def _load_sibling(name: str):
    """同目录模块：调用方（four_dim_scorer / chan_signal_backtest / CLI）通常已把本目录放进
    sys.path；被 importlib 按路径加载时（测试）没有，退回按文件名加载。"""
    try:
        return importlib.import_module(name)
    except ImportError:
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{name}.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


chan_kline = _load_sibling("chan_kline")
chan_segment = _load_sibling("chan_segment")
chan_center = _load_sibling("chan_center")
chan_bsp = _load_sibling("chan_bsp")

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


# ========== 中枢（构造在 chan_center，此处只做旧契约映射）==========

def _legacy_center(center: Dict[str, Any]) -> Dict[str, Any]:
    """新中枢 → analyze() 的 last_center 字段：旧键全部保留（值随算法升级而变），
    另增 is_sure/seg_idx/bi_in_idx/bi_out_idx 四个新键。"""
    return {"zg": round(center["zg"], 3), "zd": round(center["zd"], 3),
            "start_stroke": center["start_bi_idx"], "end_stroke": center["end_bi_idx"],
            "start_idx": center["start_idx"], "end_idx": center["end_idx"],
            "stroke_count": center["bi_count"],
            "is_sure": center["is_sure"], "seg_idx": center["seg_idx"],
            "bi_in_idx": center["bi_in_idx"], "bi_out_idx": center["bi_out_idx"]}


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
    seg_view = chan_segment.analyze_segs(strokes)
    # 中枢：段内构造（无线段时不产出中枢，对齐参考实现 cal_bi_zs 的 normal 分支）
    centers = chan_center.build_centers(strokes, seg_view["segs"])

    raw_signals = chan_bsp.build_signals(strokes, seg_view["segs"], centers, bars, hist)
    # 回填日期 + strategy_id（新谱系类型无 legacy strategy_id → None，下游天然 0 权重）
    signals = []
    for s in raw_signals:
        idx = s["idx"]
        s["date"] = bars[idx].get("date") if 0 <= idx < len(bars) else None
        s["strategy_id"] = SIGNAL_STRATEGY.get(s["type"])
        signals.append(s)

    last_center = _legacy_center(centers[-1]) if centers else None
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
            # 2026-08 T2 新增：线段层（T3/T4 起中枢与买卖点均已迁到线段口径）
            "seg_count": seg_view["seg_count"],
            "sure_seg_count": seg_view["sure_seg_count"],
            "last_seg": seg_view["last_seg"],
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
        parts.append("无买卖点")
    return " | ".join(parts)


# ========== CLI（取数后调 analyze）==========

def _fetch_bars(code: str, days: int) -> List[Dict[str, Any]]:
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
