#!/usr/bin/env python3
"""
S2 龙头分歧回封（DivergenceReseal）信号 — 升级方案 §6.1，NON-LIVE 研究层
=========================================================================
信号：板块涨停 ≥ 3 ∧ 板块内存在大量一字/快速板（分歧的表征：追不进去）∧ 目标是
**前 2 个完成充分换手后回封**的前排股 ∧ 封板前换手 ≥ 20 日同期中位数的 1.5-3.0 倍。

本策略的成败点（务必读完再改代码）
----------------------------------
"前 2 个"必须按**回封时刻**的先后顺序确定，绝不能按结果（是否守住到收盘）挑选。
`reseal_rank` 只吃各标的的 `reseal_time`（HHMMSS），与该标的回封之后是否又炸板
（`later_break` 等字段）完全无关——本模块甚至不读取那类"后续结果"字段。一个先
回封、后来又炸板的标的，排名与信号判定都不受影响，因为判定发生在回封时刻，看
的是"截至该时刻"的信息。tests/test_divergence_reseal.py 有专门用例守这一点。

同理，"充分换手"用**20 日同期中位数**这个事前基准判断，不用当日绝对换手率拍
脑袋；基准样本不足（< min_baseline_sample_days）时 unavailable，不给默认基准。

fail-closed 纪律（缺证据 ≠ 不触发）：分钟线/板块映射缺失、回封时刻缺失、20 日
基准样本不足 → status="unavailable" 并给出 reasons，**绝不返回 0 或 no_signal**。
把"没数据"折叠成"不触发"会让零样本看起来像已验证的负结果，是假绿的一种。

阈值单一事实源：config/daban_thresholds.yaml 的 divergence_reseal 节
（缺失回退 daban_config.DEFAULTS，同值）。

红线：本模块未在 strategy_registry 注册，任何调用方都不得把它的输出折进实盘排序、
评分或仓位。升级路径见 config/strategy_packs/divergence_reseal.yaml。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

import daban_config as _cfg

SCHEMA = "divergence_reseal_signal_v1"

STATUS_SIGNAL = "signal"
STATUS_NO_SIGNAL = "no_signal"
STATUS_UNAVAILABLE = "unavailable"

# 入场条件 id —— 报告/测试按 id 逐条断言，避免用中文串匹配。
COND_SECTOR_BREADTH = "sector_limit_up_breadth"
COND_FAST_SEAL_DENSITY = "sector_fast_seal_density"
COND_RESEAL_RANK = "reseal_rank_top_n"
COND_TURNOVER_BAND = "reseal_turnover_band"
CONDITION_IDS = (
    COND_SECTOR_BREADTH, COND_FAST_SEAL_DENSITY, COND_RESEAL_RANK, COND_TURNOVER_BAND,
)


def config(path: Optional[str] = None) -> dict[str, Any]:
    """取 divergence_reseal 阈值（yaml 覆盖 DEFAULTS）。"""
    return _cfg.section("divergence_reseal", path)


def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN 不是数字（pandas 记录常见）


def _minutes(value: Any) -> Optional[int]:
    """'093500' / '09:35' / '0935' → 分钟数；非法/缺失返回 None。"""
    if value is None or value == "":
        return None
    text = str(value).strip().replace(":", "")
    if not text.isdigit() or len(text) < 4:
        return None
    return int(text[:2]) * 60 + int(text[2:4])


# --------------------------------------------------------------------------- #
# 20 日同期换手基准 —— 事前可算，是"充分换手"判定的成败点
# --------------------------------------------------------------------------- #
def turnover_baseline(
    *,
    median_pct: Any,
    sample_days: Any,
    cfg: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """20 日同期换手中位数基准。样本不足/缺失 → unavailable（不给默认基准）。"""
    settings = dict(cfg if cfg is not None else config())
    minimum = int(settings.get("min_baseline_sample_days", 15))
    preferred = int(settings.get("preferred_baseline_sample_days", 20))
    median = _num(median_pct)
    days_raw = _num(sample_days)
    days = int(days_raw) if days_raw is not None else None

    reasons: list[str] = []
    if median is None:
        reasons.append("turnover_baseline_median_missing")
    if days is None:
        reasons.append("turnover_baseline_sample_days_missing")
    elif days < minimum:
        reasons.append(f"turnover_baseline_sample_insufficient({days}<{minimum})")
    if reasons:
        return {"status": STATUS_UNAVAILABLE, "median": None, "sample_days": days,
                "reasons": reasons}
    return {
        "status": "ok", "median": median, "sample_days": days,
        "below_preferred_sample": days < preferred, "reasons": [],
    }


def turnover_ratio(
    *,
    pre_reseal_turnover_pct: Any,
    turnover_baseline_median_pct: Any,
    turnover_baseline_sample_days: Any,
    cfg: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Ratio = 封板前换手% / 20日同期中位数换手%。任一输入缺失 → unavailable。"""
    settings = dict(cfg if cfg is not None else config())
    actual = _num(pre_reseal_turnover_pct)
    baseline = turnover_baseline(
        median_pct=turnover_baseline_median_pct,
        sample_days=turnover_baseline_sample_days, cfg=settings,
    )
    reasons: list[str] = list(baseline.get("reasons") or [])
    if actual is None:
        reasons.append("pre_reseal_turnover_missing")
    if baseline["status"] != "ok" or actual is None:
        return {"status": STATUS_UNAVAILABLE, "value": None, "actual": actual,
                "baseline": baseline, "reasons": reasons}
    if baseline["median"] == 0:
        reasons.append("turnover_baseline_zero")
        return {"status": STATUS_UNAVAILABLE, "value": None, "actual": actual,
                "baseline": baseline, "reasons": reasons}
    return {
        "status": "ok", "value": actual / float(baseline["median"]),
        "actual": actual, "baseline": baseline, "reasons": [],
    }


# --------------------------------------------------------------------------- #
# 回封时刻排名 —— 只看"截至回封时刻"的信息，不看后续结果（本策略成败点）
# --------------------------------------------------------------------------- #
def reseal_rank(peers: Sequence[Mapping[str, Any]]) -> list[Optional[int]]:
    """按 reseal_time 升序给出 1-based 名次；未回封(reseal_time 缺失/非法)的标的为 None。

    只吃 reseal_time 这一个字段——不读取任何"后续是否守住/炸板"的字段，杜绝
    未来函数：排名在回封时刻就已确定，与收盘结果无关。并列 reseal_time 按 code
    做稳定 tiebreak（避免依赖输入顺序）。
    """
    keyed = [
        (i, _minutes(p.get("reseal_time")), str(p.get("code") or ""))
        for i, p in enumerate(peers)
    ]
    resealed = sorted(
        (item for item in keyed if item[1] is not None),
        key=lambda item: (item[1], item[2]),
    )
    ranks: list[Optional[int]] = [None] * len(peers)
    for rank, (index, _minute, _code) in enumerate(resealed, start=1):
        ranks[index] = rank
    return ranks


def _own_reseal_rank(
    record: Mapping[str, Any], peer_list: Sequence[Mapping[str, Any]],
) -> tuple[Optional[int], Optional[int]]:
    code = str(record.get("code") or "")
    index = next(
        (i for i, p in enumerate(peer_list) if code and str(p.get("code") or "") == code),
        None,
    )
    if index is None:
        return None, None
    ranks = reseal_rank(peer_list)
    return index, ranks[index]


# --------------------------------------------------------------------------- #
# 单标的信号
# --------------------------------------------------------------------------- #
def _condition(cid: str, ok: Optional[bool], detail: str) -> dict[str, Any]:
    return {"id": cid, "ok": ok, "detail": detail}


def _breadth_conditions(
    record: Mapping[str, Any], settings: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """条件 1/2：板块涨停≥3 ∧ 板块内一字/快速板家数达到"大量"的可执行下限。"""
    min_breadth = float(settings.get("min_sector_limit_up_count", 3))
    min_fast = float(settings.get("min_sector_fast_seal_count", 2))
    conditions: list[dict[str, Any]] = []
    reasons: list[str] = []

    breadth = _num(record.get("sector_limit_up_count"))
    if breadth is None:
        reasons.append("sector_limit_up_count_missing")
        conditions.append(_condition(COND_SECTOR_BREADTH, None, "板块涨停家数不可用"))
    else:
        ok = breadth >= min_breadth
        conditions.append(_condition(
            COND_SECTOR_BREADTH, ok, f"板块涨停{breadth:g}家{'≥' if ok else '<'}{min_breadth:g}"))

    fast = _num(record.get("sector_fast_seal_count"))
    if fast is None:
        reasons.append("sector_fast_seal_count_missing")
        conditions.append(_condition(COND_FAST_SEAL_DENSITY, None, "板块一字/快速板家数不可用"))
    else:
        ok = fast >= min_fast
        conditions.append(_condition(
            COND_FAST_SEAL_DENSITY, ok, f"一字/快速板{fast:g}家{'≥' if ok else '<'}{min_fast:g}"))
    return conditions, reasons


def _rank_condition(
    index: Optional[int], rank: Optional[int], settings: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """条件 3：按回封时刻先后排名前 N（方案原文"前 2 个"）。"""
    top_n = int(settings.get("reseal_rank_top_n", 2))
    if index is None:
        return _condition(COND_RESEAL_RANK, None, "标的不在同板块梯队中"), ["record_not_in_peer_group"]
    if rank is None:
        return (_condition(COND_RESEAL_RANK, None, "回封时刻缺失/未回封"),
                ["reseal_time_missing_or_not_resealed"])
    ok = rank <= top_n
    return _condition(
        COND_RESEAL_RANK, ok, f"回封先后名次{rank}{'≤' if ok else '>'}{top_n}"), []


def _turnover_band_condition(
    record: Mapping[str, Any], settings: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """条件 4：封板前换手 ∈ [20日同期中位数×1.5, ×3.0]。"""
    ratio = turnover_ratio(
        pre_reseal_turnover_pct=record.get("pre_reseal_turnover_pct"),
        turnover_baseline_median_pct=record.get("turnover_baseline_median_pct"),
        turnover_baseline_sample_days=record.get("turnover_baseline_sample_days"),
        cfg=settings,
    )
    if ratio["status"] != "ok":
        reasons = [f"turnover_ratio:{r}" for r in ratio.get("reasons") or []]
        return _condition(COND_TURNOVER_BAND, None, "换手倍数不可用"), reasons
    lo = float(settings.get("min_turnover_ratio", 1.5))
    hi = float(settings.get("max_turnover_ratio", 3.0))
    value = ratio["value"]
    ok = lo <= value <= hi
    return (_condition(
        COND_TURNOVER_BAND, ok, f"换手倍数{value:.3f}{'∈' if ok else '∉'}[{lo:g},{hi:g}]"), [])


def _degradations(baseline_status_reasons: Iterable[str], turnover: Mapping[str, Any]) -> list[str]:
    degraded: list[str] = []
    baseline = (turnover.get("baseline") or {}) if isinstance(turnover, Mapping) else {}
    if baseline.get("below_preferred_sample"):
        degraded.append(f"turnover_baseline_sample_below_preferred({baseline.get('sample_days')})")
    return degraded


def evaluate(
    record: Mapping[str, Any],
    *,
    peers: Sequence[Mapping[str, Any]],
    cfg: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """对单个标的判 S2，peers 必须含 record 自身（同板块、同交易日的候选池）。

    返回 {schema, status, conditions[], reasons[], turnover_ratio, reseal_rank}。
    status ∈ signal / no_signal / unavailable —— 缺任一必需证据一律 unavailable。
    """
    settings = dict(cfg if cfg is not None else config())
    peer_list = list(peers or [])
    code = str(record.get("code") or "")
    reasons: list[str] = []
    if not str(record.get("sector") or "").strip():
        reasons.append("sector_missing")

    breadth_conditions, breadth_reasons = _breadth_conditions(record, settings)
    index, rank = _own_reseal_rank(record, peer_list)
    rank_condition, rank_reasons = _rank_condition(index, rank, settings)
    ratio = turnover_ratio(
        pre_reseal_turnover_pct=record.get("pre_reseal_turnover_pct"),
        turnover_baseline_median_pct=record.get("turnover_baseline_median_pct"),
        turnover_baseline_sample_days=record.get("turnover_baseline_sample_days"),
        cfg=settings,
    )
    band_condition, band_reasons = _turnover_band_condition(record, settings)

    conditions = breadth_conditions + [rank_condition, band_condition]
    reasons.extend(breadth_reasons)
    reasons.extend(rank_reasons)
    reasons.extend(band_reasons)
    degraded = _degradations(reasons, ratio)

    if reasons:
        status = STATUS_UNAVAILABLE
    elif all(c["ok"] for c in conditions):
        status = STATUS_SIGNAL
    else:
        status = STATUS_NO_SIGNAL
    return {
        "schema": SCHEMA,
        "code": code,
        "sector": record.get("sector"),
        "date": record.get("date"),
        "status": status,
        "reseal_rank": rank,
        "turnover_ratio": ratio,
        "conditions": conditions,
        "reasons": reasons,
        "degraded": degraded,
        "peer_count": len(peer_list),
        "influences_live_ranking": False,
        "note": "S2 未在 strategy_registry 注册；输出仅供研究/回测，不得进入实盘排序或仓位",
    }


def evaluate_group(
    records: Sequence[Mapping[str, Any]],
    *,
    cfg: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """对一个（板块, 交易日）候选池内的全部标的逐个判 S2。"""
    settings = dict(cfg if cfg is not None else config())
    peers = list(records or [])
    return [evaluate(r, peers=peers, cfg=settings) for r in peers]


def evaluate_universe(
    records: Sequence[Mapping[str, Any]],
    *,
    cfg: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """按 (date, sector) 分组后逐组判 S2；板块缺失的记录单独成组必然 unavailable。"""
    settings = dict(cfg if cfg is not None else config())
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for record in records or []:
        key = (str(record.get("date") or ""), str(record.get("sector") or ""))
        groups.setdefault(key, []).append(record)
    out: list[dict[str, Any]] = []
    for key in sorted(groups):
        out.extend(evaluate_group(groups[key], cfg=settings))
    return out


def signal_codes(results: Iterable[Mapping[str, Any]]) -> set[tuple[str, str]]:
    """命中集合 {(code, date)}，供回测按事件键筛选。"""
    return {
        (str(r.get("code") or ""), str(r.get("date") or ""))
        for r in results if r.get("status") == STATUS_SIGNAL
    }


def summarize(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """状态分布 + unavailable 原因计数（零样本时必须能看出是缺哪类数据）。"""
    counts = {STATUS_SIGNAL: 0, STATUS_NO_SIGNAL: 0, STATUS_UNAVAILABLE: 0}
    reasons: dict[str, int] = {}
    for result in results or []:
        counts[str(result.get("status"))] = counts.get(str(result.get("status")), 0) + 1
        for reason in result.get("reasons") or []:
            key = str(reason).split("(")[0]
            reasons[key] = reasons.get(key, 0) + 1
    return {"schema": SCHEMA, "total": len(results or []), "status_counts": counts,
            "unavailable_reasons": dict(sorted(reasons.items()))}
