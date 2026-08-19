#!/usr/bin/env python3
"""emotion_cycle 策略小样本可行性验证（研究探针，非实盘）v2。

对一份覆盖多行业的流动性 A 股清单，拉 250 天历史日 K，滚动计算 emotion_cycle
特征（F1-F4 + F5 合成），记录 emotion_bottom / emotion_top 信号出现后 5/10/20 天
的前向收益。

关键：不只看绝对收益（熊市里什么都跌），还跟"该股自身的无条件平均前向收益"
比较，算出超额收益，判断信号有没有方向性 edge 苗头。

纯研究用途，不触发任何交易。
"""

from __future__ import annotations

import statistics
import sys
import time

sys.path.insert(0, "skills/common")

from market_adapters import fetch_a_share_daily_kline  # noqa: E402
from emotion_cycle_features import compute_emotion_features  # noqa: E402

# 覆盖多行业的流动性股票清单（名称仅供注释，代码为主键）
UNIVERSE = [
    "600036", "601398",  # 银行
    "600030", "300059",  # 券商
    "600519", "000858",  # 白酒
    "600276", "300760",  # 医药
    "601127", "002594",  # 汽车
    "002371", "600584",  # 电子/半导体设备
    "688981", "603986",  # 半导体
    "300750", "601012",  # 电力设备/光伏
    "601899", "603993",  # 有色
    "601088", "600188",  # 煤炭
    "000002", "600048",  # 地产
    "002027", "300413",  # 传媒
    "600893", "002179",  # 军工
    "002230", "300033",  # 计算机
]

HORIZONS = (5, 10, 20)
MIN_BARS = 80


def main() -> None:
    fwd = {"bottom": {h: [] for h in HORIZONS}, "top": {h: [] for h in HORIZONS}}
    baseline = {h: [] for h in HORIZONS}  # 无条件前向收益（基准）
    sig_count = {"bottom": 0, "top": 0}
    ok_stocks = 0

    for code in UNIVERSE:
        try:
            bars = fetch_a_share_daily_kline(code, days=250)
        except Exception as exc:  # noqa: BLE001
            print(f"  {code} 拉取失败: {exc}")
            continue
        if len(bars) < MIN_BARS:
            print(f"  {code} K线不足 {len(bars)} 条，跳过")
            continue
        time.sleep(0.15)

        b_sig = t_sig = 0
        for i in range(MIN_BARS, len(bars)):
            close_t = bars[i]["close"]
            if close_t in (None, 0):
                continue
            # 无条件基准：所有 bar 的前向收益
            for h in HORIZONS:
                j = i + h
                if j < len(bars) and bars[j]["close"] not in (None, 0):
                    baseline[h].append((bars[j]["close"] - close_t) / close_t)

            feats = compute_emotion_features(bars[: i + 1])
            label = (feats.get("emotion_extreme") or {}).get("label")
            if label not in ("emotion_bottom", "emotion_top"):
                continue
            bucket = "bottom" if label == "emotion_bottom" else "top"
            if bucket == "bottom":
                b_sig += 1
            else:
                t_sig += 1
            for h in HORIZONS:
                j = i + h
                if j >= len(bars):
                    continue
                close_j = bars[j]["close"]
                if close_j in (None, 0):
                    continue
                fwd[bucket][h].append((close_j - close_t) / close_t)

        sig_count["bottom"] += b_sig
        sig_count["top"] += t_sig
        ok_stocks += 1
        print(f"  {code} 底信号{b_sig} 顶信号{t_sig}")

    print("\n========== 汇总 ==========")
    print(f"样本股票 {ok_stocks} 只 | 底信号 {sig_count['bottom']} | 顶信号 {sig_count['top']}")
    for h in HORIZONS:
        bm = statistics.mean(baseline[h]) if baseline[h] else 0.0
        print(f"\n  --- 后 {h} 天 ---")
        print(f"  无条件基准: {bm:+.2%} (样本{len(baseline[h])})")
        for label, cn in (("bottom", "情绪底"), ("top", "情绪顶")):
            rets = fwd[label][h]
            if len(rets) < 3:
                print(f"  {cn}: 样本{len(rets)} 太少，跳过")
                continue
            avg = statistics.mean(rets)
            pos = sum(1 for r in rets if r > 0) / len(rets)
            excess = avg - bm
            print(f"  {cn}: 样本{len(rets)} 平均{avg:+.2%} 上涨占比{pos:.0%} 超额{excess:+.2%}")
    print()


if __name__ == "__main__":
    main()
