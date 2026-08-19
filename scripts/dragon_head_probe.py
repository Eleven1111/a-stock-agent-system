#!/usr/bin/env python3
"""dragon_head 龙头策略方向性 edge 探针（研究用途，非实盘）。

目标：用可计算的历史代理指标近似「龙头」逻辑（领涨 + 量能放大 → 动量延续），
判断这个方向直觉在历史日 K（仅 OHLCV）上有没有 edge 苗头。

历史日 K 没有换手率 / 竞价排名 / 板块催化，只能用以下代理：
- 领涨地位 L：当日涨幅 change_pct 相对全市场（样本内）当日涨幅的横截面分位 >= 0.9
- 量能放大 V：volume_spike_ratio = 当日量 / 前 20 日均量 >= 1.5
- 换手代理 T：volume 60 日时序分位 >= 0.7（换手率退化为 volume 时序分位）

信号变体：
- L   ：仅领涨
- V   ：仅量能放大
- T   ：仅换手代理
- DH  ：L & V（龙头主信号）
- DH_T：L & V & T（更严龙头）

分析口径（不只报均值）：
- 前向 5/10/20/40 天收益，以及相对「该股自身无条件平均前向收益」的超额收益
- 胜率（正向收益占比）+ 中位数 + 超额胜率（超额>0 占比）
- 方向一致性：有多少只股票在龙头信号后 20 天超额为正
- 变体对比：L / V / DH / DH_T 看叠加条件是否带来额外 edge

纯研究，不触发任何交易。
"""

from __future__ import annotations

import statistics
import sys
import time

sys.path.insert(0, "skills/common")

from market_adapters import fetch_a_share_daily_kline  # noqa: E402
from emotion_cycle_features import (  # noqa: E402
    compute_volume_percentile,
    compute_volume_spike,
)

# 覆盖多行业的流动性股票清单（~49 只，代码为主键，名称仅供注释）
UNIVERSE = [
    # 银行 (4)
    "600036", "601398", "601166", "000001",
    # 券商 (4)
    "600030", "300059", "601688", "000776",
    # 白酒 (4)
    "600519", "000858", "000568", "600809",
    # 医药 (4)
    "600276", "300760", "300015", "600196",
    # 汽车 (4)
    "601127", "002594", "000625", "600104",
    # 电子/半导体设备 (3)
    "002371", "600584", "603501",
    # 半导体 (3)
    "688981", "603986", "688012",
    # 电力设备/光伏 (4)
    "300750", "601012", "002459", "600438",
    # 有色 (4)
    "601899", "603993", "600111", "002460",
    # 煤炭 (3)
    "601088", "600188", "601898",
    # 地产 (3)
    "000002", "600048", "001979",
    # 传媒 (3)
    "002027", "300413", "002624",
    # 军工 (3)
    "600893", "002179", "600760",
    # 计算机 (3)
    "002230", "300033", "600570",
]

HORIZONS = (5, 10, 20, 40)
MIN_BARS = 80

LEADER_PCT_THRESHOLD = 0.9
VOLUME_SPIKE_THRESHOLD = 1.5
TURNOVER_PCT_THRESHOLD = 0.7

VARIANTS = ("L", "V", "T", "DH", "DH_T")


def percentile_ranks(values: list[float]) -> list[float]:
    """返回每个值在横截面中的分位（0=最低，1=最高，平局取平均秩）。纯标准库。"""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.5]
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0
        pct = avg_rank / (n - 1)
        for k in range(i, j + 1):
            ranks[order[k]] = pct
        i = j + 1
    return ranks


def _mean(xs: list[float]) -> float:
    return statistics.mean(xs) if xs else 0.0


def _median(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def main() -> None:
    # ---- 1. 拉取数据 + 计算每只票的时序 ----
    data = {}  # code -> {"bars", "rets", "dates"}
    ok_codes = []
    for code in UNIVERSE:
        try:
            bars = fetch_a_share_daily_kline(code, days=250)
        except Exception as exc:  # noqa: BLE001
            print(f"  {code} 拉取失败: {exc}")
            continue
        if len(bars) < MIN_BARS:
            print(f"  {code} K线不足 {len(bars)} 条，跳过")
            continue
        rets = [None] * len(bars)
        for t in range(1, len(bars)):
            c0, c1 = bars[t - 1]["close"], bars[t]["close"]
            if c0 in (None, 0) or c1 in (None, 0):
                continue
            rets[t] = (c1 - c0) / c0
        data[code] = {"bars": bars, "rets": rets, "dates": [b["date"] for b in bars]}
        ok_codes.append(code)
        time.sleep(0.1)

    if not data:
        print("没有可用样本，退出")
        return

    # ---- 2. 横截面领涨分位：date -> {code: pct} ----
    date_panel: dict[str, list[tuple[str, float]]] = {}
    for code, d in data.items():
        for t in range(1, len(d["bars"])):
            ret = d["rets"][t]
            if ret is None:
                continue
            date_panel.setdefault(d["dates"][t], []).append((code, ret))

    date_leader_pct: dict[str, dict[str, float]] = {}
    for date, code_rets in date_panel.items():
        if len(code_rets) < 5:  # 横截面样本太少时分位无意义
            continue
        codes = [c for c, _ in code_rets]
        vals = [v for _, v in code_rets]
        pcts = percentile_ranks(vals)
        date_leader_pct[date] = {c: p for c, p in zip(codes, pcts)}

    # ---- 3. 每只票无条件基准前向收益 ----
    baseline: dict[str, dict[int, float]] = {}
    for code, d in data.items():
        base = {h: [] for h in HORIZONS}
        for t in range(MIN_BARS, len(d["bars"])):
            close_t = d["bars"][t]["close"]
            if close_t in (None, 0):
                continue
            for h in HORIZONS:
                j = t + h
                if j < len(d["bars"]) and d["bars"][j]["close"] not in (None, 0):
                    base[h].append((d["bars"][j]["close"] - close_t) / close_t)
        baseline[code] = {h: _mean(base[h]) for h in HORIZONS}

    # ---- 4. 滚动计算信号，记录每个信号的 code/t/各 horizon 前向收益 ----
    # signals[variant] = list of {"code", "fwd": {h: ret}}
    signals = {v: [] for v in VARIANTS}
    sig_by_stock = {v: {} for v in VARIANTS}

    for code, d in data.items():
        bars = d["bars"]
        for t in range(MIN_BARS, len(bars)):
            close_t = bars[t]["close"]
            if close_t in (None, 0):
                continue
            ret_t = d["rets"][t]
            if ret_t is None:
                continue

            leader_pct = (date_leader_pct.get(d["dates"][t]) or {}).get(code)
            is_leader = leader_pct is not None and leader_pct >= LEADER_PCT_THRESHOLD

            spike = compute_volume_spike(bars[: t + 1])
            vol_spike = spike.get("ratio") if spike.get("available") else None
            is_spike = vol_spike is not None and vol_spike >= VOLUME_SPIKE_THRESHOLD

            vp = compute_volume_percentile(bars[: t + 1])
            vol_pct = vp.get("pct") if vp.get("available") else None
            is_turnover = vol_pct is not None and vol_pct >= TURNOVER_PCT_THRESHOLD

            flags = {
                "L": is_leader,
                "V": is_spike,
                "T": is_turnover,
                "DH": is_leader and is_spike,
                "DH_T": is_leader and is_spike and is_turnover,
            }

            fwd_here = {}
            for h in HORIZONS:
                j = t + h
                if j >= len(bars):
                    continue
                close_j = bars[j]["close"]
                if close_j in (None, 0):
                    continue
                fwd_here[h] = (close_j - close_t) / close_t

            for variant, hit in flags.items():
                if not hit:
                    continue
                sig_by_stock[variant][code] = sig_by_stock[variant].get(code, 0) + 1
                signals[variant].append({"code": code, "fwd": fwd_here})

    # ---- 5. 汇总输出 ----
    print("\n" + "=" * 78)
    print(f"样本股票 {len(ok_codes)} 只 | 从第 {MIN_BARS} 根 bar 起滚动")
    print("=" * 78)

    # 无条件基准（全池平均）
    base_pool = {h: _mean([baseline[c][h] for c in ok_codes]) for h in HORIZONS}
    print("无条件基准(全池平均前向收益):")
    for h in HORIZONS:
        print(f"   h={h:>2}d  {base_pool[h]:+.2%}")

    for variant in VARIANTS:
        sigs = signals[variant]
        n = len(sigs)
        print(f"\n### 变体 {variant}  信号总数 = {n}")

        if n < 3:
            print("   样本太少，跳过")
            continue

        # 方向一致性：>=3 次信号的股票中，20d 超额为正的占比
        ex20_by_stock = {}
        for code, cnt in sig_by_stock[variant].items():
            if cnt < 3:
                continue
            stock_sigs = [s for s in sigs if s["code"] == code]
            excesses = [s["fwd"][20] - baseline[code][20] for s in stock_sigs if 20 in s["fwd"]]
            if excesses:
                ex20_by_stock[code] = _mean(excesses)
        if ex20_by_stock:
            pos = sum(1 for e in ex20_by_stock.values() if e > 0)
            print(f"   方向一致性(20d 超额为正的股票占比): {pos}/{len(ex20_by_stock)} = {pos / len(ex20_by_stock):.0%}")

        for h in HORIZONS:
            rets = [s["fwd"][h] for s in sigs if h in s["fwd"]]
            if len(rets) < 3:
                print(f"   h={h:>2}d  n={len(rets)} 太少，跳过")
                continue
            excess = [s["fwd"][h] - baseline[s["code"]][h] for s in sigs if h in s["fwd"]]
            avg = _mean(rets)
            med = _median(rets)
            win = sum(1 for r in rets if r > 0) / len(rets)
            ex_avg = _mean(excess)
            ex_med = _median(excess)
            ex_win = sum(1 for e in excess if e > 0) / len(excess)
            print(
                f"   h={h:>2}d  n={len(rets):>4}  前向均值 {avg:+.2%}  中位 {med:+.2%}  "
                f"胜率 {win:.0%}  |  超额均值 {ex_avg:+.2%}  超额中位 {ex_med:+.2%}  "
                f"超额胜率 {ex_win:.0%}"
            )

    print("\n" + "=" * 78)
    print("完成")
    print("=" * 78)


if __name__ == "__main__":
    main()
