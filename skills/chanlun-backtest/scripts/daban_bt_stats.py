#!/usr/bin/env python3
"""
打板回测 — 统计检验层（纯 numpy / 标准库）
============================================
研究闸门 research_gate 要求 t_test + bootstrap + permutation，多假设须 FDR 校正。
本模块只做统计，不碰行情、不碰策略，便于用合成数据独立单测。

约定：
- 收益序列为「每事件单笔净收益」（已扣成本），单位为小数（0.02 = +2%）。
- permutation 是主检验（对分布假设最弱）；t 检验用正态近似，仅作参考，字段标 _approx。
- 所有随机过程接受 seed，保证可复现。
"""

import math
from typing import Dict, List, Sequence, Tuple

import numpy as np


def _arr(x: Sequence[float]) -> np.ndarray:
    return np.asarray(list(x), dtype=float)


def _normal_sf(z: float) -> float:
    """标准正态上尾 P(Z > z)，用 erf，避免 scipy 依赖。"""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def summarize(returns: Sequence[float]) -> Dict[str, float]:
    r = _arr(returns)
    n = int(r.size)
    if n == 0:
        return {"n": 0, "mean": 0.0, "std": 0.0, "win_rate": 0.0}
    return {
        "n": n,
        "mean": float(r.mean()),
        "std": float(r.std(ddof=1)) if n > 1 else 0.0,
        "win_rate": float((r > 0).mean()),
    }


def t_test_vs_zero(returns: Sequence[float]) -> Tuple[float, float]:
    """单样本 t：均值是否显著 != 0。返回 (t_stat, p_two_sided_approx)。"""
    r = _arr(returns)
    n = r.size
    if n < 2:
        return 0.0, 1.0
    sd = r.std(ddof=1)
    if sd == 0:
        return (math.inf if r.mean() != 0 else 0.0), (0.0 if r.mean() != 0 else 1.0)
    t = r.mean() / (sd / math.sqrt(n))
    p = 2.0 * _normal_sf(abs(t))  # 正态近似，大样本可用
    return float(t), float(min(1.0, p))


def bootstrap_ci_mean(returns: Sequence[float], n_boot: int = 10000,
                      ci: float = 0.95, seed: int = 42) -> Tuple[float, float]:
    """均值的 bootstrap 置信区间。"""
    r = _arr(returns)
    if r.size == 0:
        return 0.0, 0.0
    rng = np.random.default_rng(seed)
    means = r[rng.integers(0, r.size, size=(n_boot, r.size))].mean(axis=1)
    lo = float(np.percentile(means, (1 - ci) / 2 * 100))
    hi = float(np.percentile(means, (1 + ci) / 2 * 100))
    return lo, hi


def permutation_test_diff(signal: Sequence[float], control: Sequence[float],
                          n_perm: int = 10000, seed: int = 42) -> Dict[str, float]:
    """
    标签置换检验：signal 与 control 合并后随机打散标签，统计 |均值差| >= 观测值的频率。
    返回 {observed_diff, p_value}。p 越小越说明 signal 的超额非偶然。
    """
    a, b = _arr(signal), _arr(control)
    if a.size == 0 or b.size == 0:
        return {"observed_diff": 0.0, "p_value": 1.0}
    observed = abs(a.mean() - b.mean())
    pooled = np.concatenate([a, b])
    n_a = a.size
    rng = np.random.default_rng(seed)
    count = 0
    for _ in range(n_perm):
        rng.shuffle(pooled)
        diff = abs(pooled[:n_a].mean() - pooled[n_a:].mean())
        if diff >= observed - 1e-12:
            count += 1
    return {"observed_diff": float(observed), "p_value": float((count + 1) / (n_perm + 1))}


def benjamini_hochberg(pvalues: Sequence[float], q: float = 0.10) -> List[Dict[str, float]]:
    """
    Benjamini-Hochberg FDR 校正。返回每个 p 的 {p, rank, adjusted, reject}，
    顺序与输入一致。adjusted = min over k>=i of p_(k)*m/k（单调化）。
    """
    p = _arr(pvalues)
    m = p.size
    if m == 0:
        return []
    order = np.argsort(p)
    ranks = np.empty(m, dtype=int)
    ranks[order] = np.arange(1, m + 1)
    adj = np.empty(m, dtype=float)
    prev = 1.0
    for rank in range(m, 0, -1):          # 从大 rank 往小做单调化
        idx = order[rank - 1]
        val = min(prev, p[idx] * m / rank)
        adj[idx] = val
        prev = val
    threshold_reject = adj <= q
    return [
        {"p": float(p[i]), "rank": int(ranks[i]), "adjusted": float(adj[i]),
         "reject": bool(threshold_reject[i])}
        for i in range(m)
    ]
