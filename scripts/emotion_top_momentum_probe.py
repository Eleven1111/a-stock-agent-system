#!/usr/bin/env python3
"""反向假设验证：爆量滞涨（emotion_top）是动量延续而非出货信号（研究探针，非实盘）。

背景
----
小样本探针 emotion_cycle_probe.py（28 只 × 250 天）发现 emotion_cycle 的
"情绪顶"信号后 20 天超额收益约 +4.52%（相对该股自身无条件平均），方向与
"出货=该跌"的原假设相反。本脚本扩大样本到 ~50 只跨行业流动性股票，做更严谨的
分析，判断"爆量滞涨=短期动量延续"这个反向假设是否值得做成新策略。

口径
----
- 数据：skills/common/market_adapters.py fetch_a_share_daily_kline(code, days=250)，
  前复权日 K，字段 {date, open, close, high, low, volume, amount}。
- 特征：skills/common/emotion_cycle_features.py compute_emotion_features(klines)，
  F1 volume_percentile_60d / F2 volume_spike_ratio / F3 ma_coil_ratio /
  F4 atr_contraction_pct + F5 emotion_extreme（label: emotion_top/emotion_bottom/neutral）。
- 滚动：对每只票每个时点 t（从第 80 根 bar 起），用 bars[:t+1] 算特征，只取
  label == "emotion_top" 的信号，记录 5/10/20/40 天前向收益。
- 超额：相对"该股自身无条件平均前向收益"（同一只票所有 bar 的前向收益均值），
  既做 per-stock 超额（方向一致性），也做 pooled 超额（整体效应）。
- 子条件拆解：emotion_top 由 F1(hot/extreme)、F2(distribution_suspect)、
  F3(expanding) 三选二及以上合成，分别统计命中各子条件的信号后 20 天超额，
  定位哪个子条件贡献最大。

纯研究用途，不触发任何交易，不 import 任何写盘/下单模块。
"""

from __future__ import annotations

import statistics
import sys
import time

sys.path.insert(0, "skills/common")

from market_adapters import fetch_a_share_daily_kline  # noqa: E402
from emotion_cycle_features import compute_emotion_features  # noqa: E402

# 跨行业流动性股票清单（~56 只；名称仅供注释，代码为主键）
UNIVERSE = [
    # 银行
    "600036", "601398", "601288", "000001",
    # 券商
    "600030", "300059", "601688", "000776",
    # 白酒
    "600519", "000858", "000568", "600809",
    # 医药
    "600276", "300760", "300015", "002821",
    # 汽车
    "601127", "002594", "600104", "000625",
    # 电子
    "002371", "600584", "002475", "300408",
    # 半导体
    "688981", "603986", "002049", "603501",
    # 电力设备 / 光伏
    "300750", "601012", "002459", "600438",
    # 有色
    "601899", "603993", "000878", "600111",
    # 煤炭
    "601088", "600188", "000983", "601225",
    # 地产
    "000002", "600048", "001979", "600383",
    # 传媒
    "002027", "300413", "300251", "002624",
    # 军工
    "600893", "002179", "600760", "000768",
    # 计算机
    "002230", "300033", "600588", "300454",
]

HORIZONS = (5, 10, 20, 40)
MIN_BARS = 80  # 从第 80 根 bar 起（与 emotion_cycle_probe.py 一致）

# 子条件（与 emotion_cycle_features.py synthesize_emotion_extreme 的 top_hits 口径一致）
SUB_CONDITIONS = ("f1_hot_extreme", "f2_distribution_suspect", "f3_atr_expanding")


def _sub_hits(feats: dict) -> dict[str, bool]:
    """抽取 emotion_top 的三个子条件命中情况（各自 available 才计入）。"""
    vp = feats.get("volume_percentile_60d") or {}
    vs = feats.get("volume_spike_ratio") or {}
    atr = feats.get("atr_contraction_pct") or {}
    return {
        "f1_hot_extreme": bool(vp.get("available")) and vp.get("bucket") in ("hot", "extreme"),
        "f2_distribution_suspect": bool(vs.get("available")) and vs.get("label") == "distribution_suspect",
        "f3_atr_expanding": bool(atr.get("available")) and atr.get("label") == "expanding",
    }


def _fwd_returns(bars: list[dict], i: int) -> dict[int, float | None]:
    """bar i 之后各 horizon 的前向收益；j 越界或 close 无效返回 None。"""
    close_t = bars[i]["close"]
    out: dict[int, float | None] = {}
    for h in HORIZONS:
        j = i + h
        if j >= len(bars) or close_t in (None, 0) or bars[j]["close"] in (None, 0):
            out[h] = None
        else:
            out[h] = (bars[j]["close"] - close_t) / close_t
    return out


def _mean(vals: list[float]) -> float:
    return statistics.mean(vals) if vals else 0.0


def _median(vals: list[float]) -> float:
    return statistics.median(vals) if vals else 0.0


def _winrate(vals: list[float]) -> float:
    return (sum(1 for r in vals if r > 0) / len(vals)) if vals else 0.0


def main() -> None:
    # pooled 汇总（跨所有股票拼接的收益样本）
    sig_rets: dict[int, list[float]] = {h: [] for h in HORIZONS}
    base_rets: dict[int, list[float]] = {h: [] for h in HORIZONS}
    # 子条件 standalone 前向 20 天收益（跨所有 bar，隔离各子条件独立预测力）
    sub_rets: dict[str, list[float]] = {c: [] for c in SUB_CONDITIONS}
    sub_base_rets_20: list[float] = []  # 无条件 20 天基准（pooled）

    # per-stock 超额（方向一致性用）
    per_stock: dict[str, dict] = {}
    n_signal_total = 0
    top_hits_hist: dict[int, int] = {2: 0, 3: 0}
    sub_hit_count_total: dict[str, int] = {c: 0 for c in SUB_CONDITIONS}

    ok_stocks = 0
    failed = []

    for code in UNIVERSE:
        try:
            bars = fetch_a_share_daily_kline(code, days=250)
        except Exception as exc:  # noqa: BLE001
            failed.append((code, f"fetch: {exc}"))
            continue
        if len(bars) < MIN_BARS:
            failed.append((code, f"bars={len(bars)}"))
            continue
        time.sleep(0.05)

        # 该股自身的无条件基准 & 信号 / 子条件 收益
        local_base: dict[int, list[float]] = {h: [] for h in HORIZONS}
        local_sig: dict[int, list[float]] = {h: [] for h in HORIZONS}
        local_sub: dict[str, list[float]] = {c: [] for c in SUB_CONDITIONS}
        n_sig = 0
        n_sub: dict[str, int] = {c: 0 for c in SUB_CONDITIONS}

        for i in range(MIN_BARS, len(bars)):
            if bars[i]["close"] in (None, 0):
                continue
            fwd = _fwd_returns(bars, i)

            # 无条件基准（每根 bar 都算）
            for h in HORIZONS:
                if fwd[h] is not None:
                    base_rets[h].append(fwd[h])
                    local_base[h].append(fwd[h])
            if fwd[20] is not None:
                sub_base_rets_20.append(fwd[20])

            feats = compute_emotion_features(bars[: i + 1])
            label = (feats.get("emotion_extreme") or {}).get("label")
            hits = _sub_hits(feats)

            # 子条件 standalone：该 bar 命中某子条件即计入该子条件的前向 20 天收益
            for c in SUB_CONDITIONS:
                if hits[c] and fwd[20] is not None:
                    sub_rets[c].append(fwd[20])
                    local_sub[c].append(fwd[20])
                    n_sub[c] += 1

            if label != "emotion_top":
                continue

            n_sig += 1
            th = (feats.get("emotion_extreme") or {}).get("top_hits", 0)
            top_hits_hist[th] = top_hits_hist.get(th, 0) + 1
            for c in SUB_CONDITIONS:
                if hits[c]:
                    sub_hit_count_total[c] += 1
            for h in HORIZONS:
                if fwd[h] is not None:
                    sig_rets[h].append(fwd[h])
                    local_sig[h].append(fwd[h])

        n_signal_total += n_sig
        ok_stocks += 1

        # per-stock 超额（信号均值 - 自身无条件均值）
        excess = {}
        for h in HORIZONS:
            excess[h] = _mean(local_sig[h]) - _mean(local_base[h])
        excess20_sub = {c: _mean(local_sub[c]) - _mean(local_base[20]) for c in SUB_CONDITIONS}
        per_stock[code] = {
            "n_sig": n_sig,
            "excess": excess,
            "excess20_sub": excess20_sub,
            "n_sub": n_sub,
        }
        print(
            f"  {code} 顶信号{n_sig} "
            + " ".join(f"{c.split('_')[1][:6]}={n_sub[c]}" for c in SUB_CONDITIONS)
        )

    print("\n" + "=" * 78)
    print(f"样本股票 {ok_stocks} 只 | emotion_top 信号总数 {n_signal_total}")
    print(f"拉取失败/数据不足: {len(failed)} 只 {failed if failed else ''}")
    print("top_hits 分布:", dict(sorted(top_hits_hist.items())))
    print("信号内子条件命中次数:", sub_hit_count_total)
    print("=" * 78)

    # ---- 主信号：各 horizon 的 pooled 收益 vs 无条件基准 ----
    print("\n【主信号 emotion_top：前向收益 vs 该股自身无条件平均】")
    for h in HORIZONS:
        s = sig_rets[h]
        b = base_rets[h]
        if len(s) < 5:
            print(f"  --- 后 {h} 天: 信号样本 {len(s)} 太少，跳过 ---")
            continue
        avg_s, avg_b = _mean(s), _mean(b)
        excess_pooled = avg_s - avg_b
        # per-stock 超额（方向一致性）
        excesses = [per_stock[c]["excess"][h] for c in per_stock if per_stock[c]["n_sig"] > 0]
        pos_stocks = sum(1 for e in excesses if e > 0)
        print(
            f"  --- 后 {h} 天 ---\n"
            f"    信号样本 {len(s)} | 平均 {avg_s:+.2%} | 中位数 {_median(s):+.2%} "
            f"| 胜率(上涨占比) {_winrate(s):.0%}\n"
            f"    无条件基准 {len(b)} | 平均 {avg_b:+.2%} | 中位数 {_median(b):+.2%}\n"
            f"    pooled 超额 {excess_pooled:+.2%} | per-stock 超额中位数 {_median(excesses):+.2%} "
            f"| {pos_stocks}/{len(excesses)} 只股票超额为正"
        )

    # ---- 方向一致性（20 天） ----
    print("\n【方向一致性（20 天 per-stock 超额）】")
    excess20 = [per_stock[c]["excess"][20] for c in per_stock if per_stock[c]["n_sig"] > 0]
    if excess20:
        pos = sum(1 for e in excess20 if e > 0)
        print(
            f"  有 emotion_top 信号的股票 {len(excess20)} 只；"
            f"20 天超额为正 {pos} 只 ({pos/len(excess20):.0%})，"
            f"为负 {len(excess20)-pos} 只"
        )
        print(
            f"  per-stock 20 天超额: 均值 {_mean(excess20):+.2%} "
            f"中位数 {_median(excess20):+.2%} "
            f"min {min(excess20):+.2%} max {max(excess20):+.2%}"
        )

    # ---- 子条件拆解：standalone（所有 bar 命中该子条件的 20 天收益） ----
    print("\n【子条件拆解（standalone：所有命中该子条件的 bar，20 天前向收益）】")
    b20_mean = _mean(sub_base_rets_20)
    print(f"  无条件 20 天基准: 平均 {b20_mean:+.2%} (样本 {len(sub_base_rets_20)})")
    for c, cn in (
        ("f1_hot_extreme", "F1 volume_percentile_60d∈{hot,extreme}"),
        ("f2_distribution_suspect", "F2 volume_spike_ratio=distribution_suspect"),
        ("f3_atr_expanding", "F3 atr_contraction_pct=expanding"),
    ):
        r = sub_rets[c]
        if len(r) < 5:
            print(f"  {cn}: 样本 {len(r)} 太少，跳过")
            continue
        excess_pooled = _mean(r) - b20_mean
        excesses = [per_stock[s]["excess20_sub"][c] for s in per_stock if per_stock[s]["n_sub"][c] > 0]
        pos = sum(1 for e in excesses if e > 0)
        print(
            f"  {cn}\n"
            f"    样本 {len(r)} | 平均 {_mean(r):+.2%} | 中位数 {_median(r):+.2%} "
            f"| 胜率 {_winrate(r):.0%}\n"
            f"    pooled 超额 {excess_pooled:+.2%} | per-stock 超额中位数 {_median(excesses):+.2%} "
            f"| {pos}/{len(excesses)} 只股票为正"
        )

    # ---- 子条件拆解：信号内的边际贡献（emotion_top 信号中命中/未命中某子条件） ----
    print("\n【子条件边际贡献（仅 emotion_top 信号内，20 天收益命中 vs 未命中）】")
    # 重新遍历信号，按子条件分组（需要重跑一次信号，这里复用 sig_rets 不可分，故重扫）
    # 简化：用 pooled 信号样本按子条件再切分需要保存信号级子条件，改在下面一次性重算
    # 为控制复杂度，此处单独重扫一遍数据
    sig_sub_rets: dict[str, list[float]] = {c: [] for c in SUB_CONDITIONS}
    sig_nosub_rets: dict[str, list[float]] = {c: [] for c in SUB_CONDITIONS}
    for code in per_stock:
        try:
            bars = fetch_a_share_daily_kline(code, days=250)
        except Exception:  # noqa: BLE001
            continue
        if len(bars) < MIN_BARS:
            continue
        for i in range(MIN_BARS, len(bars)):
            if bars[i]["close"] in (None, 0):
                continue
            feats = compute_emotion_features(bars[: i + 1])
            if (feats.get("emotion_extreme") or {}).get("label") != "emotion_top":
                continue
            fwd = _fwd_returns(bars, i)[20]
            if fwd is None:
                continue
            hits = _sub_hits(feats)
            for c in SUB_CONDITIONS:
                if hits[c]:
                    sig_sub_rets[c].append(fwd)
                else:
                    sig_nosub_rets[c].append(fwd)
    for c, cn in (
        ("f1_hot_extreme", "F1 hot/extreme"),
        ("f2_distribution_suspect", "F2 distribution_suspect"),
        ("f3_atr_expanding", "F3 atr_expanding"),
    ):
        hit, miss = sig_sub_rets[c], sig_nosub_rets[c]
        if len(hit) < 5 or len(miss) < 5:
            print(f"  {cn}: 命中 {len(hit)} / 未命中 {len(miss)}，一侧样本不足，跳过")
            continue
        print(
            f"  {cn}: 命中 {len(hit)} 平均 {_mean(hit):+.2%} | "
            f"未命中 {len(miss)} 平均 {_mean(miss):+.2%} | "
            f"边际差 {_mean(hit)-_mean(miss):+.2%}"
        )

    print("\n完成。")


if __name__ == "__main__":
    main()
