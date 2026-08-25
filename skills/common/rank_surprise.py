#!/usr/bin/env python3
"""
S1 超预期（RankSurprise）信号 — 升级方案 §6.1，NON-LIVE 研究层
================================================================
为什么先有预期基准：研究报告把"超预期"列为最通用的信号（弱→强、分歧→一致），
同时警告它**最容易事后解释**——"今天涨了所以是超预期"。因此本模块的核心不是
入场四条件，而是那条**事前可算、可证伪**的预期基准：

    ExpectedGap_i = Median(Gap_peer) + β₁·昨日收益% + β₂·连板高度
    Surprise_i    = ActualGap_i − ExpectedGap_i

peer 集合、β、全部阈值都来自配置与入参，模块内零硬编码；两个同样 ActualGap 的
标的，只要 peer 分布不同，Surprise 就必须不同——否则基准没起作用。

入场四条件（方案 §6.1）：
  1) 昨日板块内强度排名后 30%
  2) 今日竞价强度进板块内前 20%
  3) 09:45 前量比 > 1.5
  4) 题材未退潮（复用 market_temperature / market_cycle_state 的 S 状态口径）

fail-closed 纪律（缺证据 ≠ 不超预期）：peer 样本不足、竞价/量比缺失、板块映射缺失、
市场状态不可用 → status="unavailable" 并给出 reasons，**绝不返回 0 或 no_signal**。
把"没数据"折叠成"不触发"会让零样本看起来像已验证的负结果，是假绿的一种。

只消费已固化快照（09:24 全市场轻量竞价快照 / 昨日梯队 / 板块强度），不触网。
阈值单一事实源：config/daban_thresholds.yaml 的 rank_surprise 节
（缺失回退 daban_config.DEFAULTS，同值）。

红线：本模块未在 strategy_registry 注册，任何调用方都不得把它的输出折进实盘排序、
评分或仓位。升级路径见 config/strategy_packs/rank_surprise.yaml。
"""

from __future__ import annotations

from statistics import median
from typing import Any, Iterable, Mapping, Optional, Sequence

import daban_config as _cfg

SCHEMA = "rank_surprise_signal_v1"

STATUS_SIGNAL = "signal"
STATUS_NO_SIGNAL = "no_signal"
STATUS_UNAVAILABLE = "unavailable"

# 入场条件 id —— 报告/测试按 id 逐条断言，避免用中文串匹配。
COND_PRIOR_RANK = "prior_rank_bottom"
COND_AUCTION_RANK = "auction_rank_top"
COND_VOLUME_RATIO = "volume_ratio"
COND_THEME_ALIVE = "theme_not_ebbing"
CONDITION_IDS = (COND_PRIOR_RANK, COND_AUCTION_RANK, COND_VOLUME_RATIO, COND_THEME_ALIVE)


def config(path: Optional[str] = None) -> dict[str, Any]:
    """取 rank_surprise 阈值（yaml 覆盖 DEFAULTS）。"""
    return _cfg.section("rank_surprise", path)


def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN 不是数字（pandas 记录常见）


# --------------------------------------------------------------------------- #
# 预期基准 —— 事前可算，是整条策略的成败点
# --------------------------------------------------------------------------- #
def expected_gap(
    *,
    peer_gaps: Optional[Iterable[Any]],
    prior_return_pct: Any,
    board_height: Any,
    cfg: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """ExpectedGap = Median(Gap_peer) + β₁·昨日收益% + β₂·连板高度。

    peer 样本不足 / 昨日收益或连板高度缺失 → status=unavailable（不给默认基准）。
    """
    settings = dict(cfg if cfg is not None else config())
    minimum = int(settings.get("min_peer_count", 5))
    gaps = [g for g in (_num(v) for v in (peer_gaps or [])) if g is not None]
    prior = _num(prior_return_pct)
    height = _num(board_height)

    reasons: list[str] = []
    if len(gaps) < minimum:
        reasons.append(f"peer_gap_sample_insufficient({len(gaps)}<{minimum})")
    if prior is None:
        reasons.append("prior_return_pct_missing")
    if height is None:
        reasons.append("board_height_missing")
    if reasons:
        return {"status": STATUS_UNAVAILABLE, "value": None, "peer_median": None,
                "peer_count": len(gaps), "reasons": reasons}

    beta_prior = float(settings.get("beta_prior_return", 0.0))
    beta_height = float(settings.get("beta_board_height", 0.0))
    peer_median = float(median(gaps))
    value = peer_median + beta_prior * float(prior) + beta_height * float(height)
    return {
        "status": "ok",
        "value": value,
        "peer_median": peer_median,
        "peer_count": len(gaps),
        "beta_prior_return": beta_prior,
        "beta_board_height": beta_height,
        "betas_fitted": bool(settings.get("betas_fitted", False)),
        "reasons": [],
    }


def surprise(
    *,
    actual_gap: Any,
    peer_gaps: Optional[Iterable[Any]],
    prior_return_pct: Any,
    board_height: Any,
    cfg: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Surprise = ActualGap − ExpectedGap。任一输入缺失 → unavailable。"""
    settings = dict(cfg if cfg is not None else config())
    actual = _num(actual_gap)
    baseline = expected_gap(
        peer_gaps=peer_gaps, prior_return_pct=prior_return_pct,
        board_height=board_height, cfg=settings,
    )
    if actual is None or baseline["status"] != "ok":
        reasons = list(baseline.get("reasons") or [])
        if actual is None:
            reasons.append("actual_gap_missing")
        return {"status": STATUS_UNAVAILABLE, "value": None,
                "expected_gap": baseline, "actual_gap": actual, "reasons": reasons}
    return {
        "status": "ok",
        "value": actual - float(baseline["value"]),
        "actual_gap": actual,
        "expected_gap": baseline,
        "reasons": [],
    }


# --------------------------------------------------------------------------- #
# 板块内横截面排名 —— 0.0 = 最弱，1.0 = 最强；并列取相同分位
# --------------------------------------------------------------------------- #
def percentile_ranks(keys: Sequence[Any]) -> list[Optional[float]]:
    """把强度排序键映射成 [0,1] 分位（严格更弱者占比）。

    并列元素得到相同分位；样本量 <2 时全部返回 None（单点排名无意义）。
    keys 元素可为数值或可比较元组（如 (昨日收益, 封板早晚) 复合强度）。
    """
    total = len(keys)
    if total < 2 or any(key is None for key in keys):
        return [None] * total
    ranks: list[Optional[float]] = []
    for key in keys:
        weaker = sum(1 for other in keys if other < key)
        ranks.append(weaker / (total - 1))
    return ranks


def _strength_key(record: Mapping[str, Any], field: str, tiebreak_field: str) -> Optional[Any]:
    primary = _num(record.get(field))
    if primary is None:
        return None
    tiebreak = _num(record.get(tiebreak_field))
    return (primary, tiebreak if tiebreak is not None else 0.0)


# --------------------------------------------------------------------------- #
# 题材退潮 —— 复用既有 S 状态口径，不重造温度/周期判定
# --------------------------------------------------------------------------- #
def theme_alive(
    market_state: Optional[Mapping[str, Any]],
    *,
    cfg: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """题材是否未退潮。

    market_state 取 market_temperature.classify_market_state 或
    market_cycle_state 记忆层的输出形状：{available, dominant_state}。
    不可用 / 无状态 → available=False（fail-closed，不当作"未退潮"）。
    """
    settings = dict(cfg if cfg is not None else config())
    ebbing = {str(s) for s in (settings.get("ebbing_states") or [])}
    state_map = dict(market_state or {})
    state = state_map.get("dominant_state") or state_map.get("state")
    if state_map.get("available") is False or not state:
        return {"available": False, "alive": None, "state": state or None,
                "reason": "market_state_unavailable"}
    text = str(state)
    return {"available": True, "alive": text not in ebbing, "state": text,
            "ebbing_states": sorted(ebbing),
            "reason": "state_ebbing" if text in ebbing else "state_not_ebbing"}


# --------------------------------------------------------------------------- #
# 单标的信号
# --------------------------------------------------------------------------- #
def _condition(cid: str, ok: Optional[bool], detail: str) -> dict[str, Any]:
    return {"id": cid, "ok": ok, "detail": detail}


def _own_rank_pcts(
    record: Mapping[str, Any],
    peer_list: Sequence[Mapping[str, Any]],
    settings: Mapping[str, Any],
) -> tuple[Optional[int], Optional[float], Optional[float]]:
    """返回 (record 在 peer 列表中的下标, 昨日强度分位, 竞价强度分位)。"""
    prior_field = str(settings.get("prior_strength_field", "prior_strength"))
    prior_tiebreak = str(settings.get("prior_strength_tiebreak_field", "prior_strength_tiebreak"))
    auction_field = str(settings.get("auction_strength_field", "auction_strength"))
    code = str(record.get("code") or "")
    index = next(
        (i for i, p in enumerate(peer_list) if code and str(p.get("code") or "") == code),
        None,
    )
    if index is None:
        return None, None, None
    prior_pct = percentile_ranks([_strength_key(p, prior_field, prior_tiebreak) for p in peer_list])
    auction_pct = percentile_ranks([_strength_key(p, auction_field, auction_field) for p in peer_list])
    return index, prior_pct[index], auction_pct[index]


def _rank_conditions(
    own_prior: Optional[float],
    own_auction: Optional[float],
    settings: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """条件 1/2：昨日板块内强度后 N% ∧ 今日竞价强度前 M%。"""
    bottom = float(settings.get("prior_rank_bottom_pct", 0.30))
    top = float(settings.get("auction_rank_top_pct", 0.20))
    conditions: list[dict[str, Any]] = []
    reasons: list[str] = []
    if own_prior is None:
        reasons.append("prior_strength_rank_unavailable")
        conditions.append(_condition(COND_PRIOR_RANK, None, "昨日板块内强度排名不可用"))
    else:
        ok = own_prior <= bottom
        conditions.append(_condition(
            COND_PRIOR_RANK, ok, f"昨日强度分位{own_prior:.3f}{'≤' if ok else '>'}{bottom:g}"))
    if own_auction is None:
        reasons.append("auction_strength_rank_unavailable")
        conditions.append(_condition(COND_AUCTION_RANK, None, "今日竞价强度排名不可用"))
    else:
        ok = own_auction >= 1.0 - top
        conditions.append(_condition(
            COND_AUCTION_RANK, ok,
            f"竞价强度分位{own_auction:.3f}{'≥' if ok else '<'}{1.0 - top:g}"))
    return conditions, reasons


def _evidence_conditions(
    record: Mapping[str, Any],
    market_state: Optional[Mapping[str, Any]],
    settings: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """条件 3/4：09:45 前量比 > 阈值 ∧ 题材未退潮。"""
    conditions: list[dict[str, Any]] = []
    reasons: list[str] = []
    ratio = _num(record.get("volume_ratio"))
    threshold = float(settings.get("min_volume_ratio", 1.5))
    if ratio is None:
        reasons.append("volume_ratio_missing")
        conditions.append(_condition(COND_VOLUME_RATIO, None, "量比缺失"))
    else:
        ok = ratio > threshold
        conditions.append(_condition(
            COND_VOLUME_RATIO, ok, f"量比{ratio:g}{'>' if ok else '≤'}{threshold:g}"))
    alive = theme_alive(market_state, cfg=settings)
    if not alive["available"]:
        reasons.append("theme_state_unavailable")
        conditions.append(_condition(COND_THEME_ALIVE, None, "市场/题材状态不可用"))
    else:
        conditions.append(_condition(
            COND_THEME_ALIVE, bool(alive["alive"]), f"状态{alive['state']}:{alive['reason']}"))
    return conditions, reasons


def _degradations(record: Mapping[str, Any], settings: Mapping[str, Any]) -> list[str]:
    """标注"能算但口径打折"的地方——不阻断，但必须随结果传播到报告。"""
    degraded: list[str] = []
    if not bool(settings.get("betas_fitted", False)):
        degraded.append("betas_unfitted_placeholder")
    source = str(record.get("volume_ratio_source") or "")
    if source and source != "intraday_0945":
        degraded.append(f"volume_ratio_source={source}")
    return degraded


def evaluate(
    record: Mapping[str, Any],
    *,
    peers: Sequence[Mapping[str, Any]],
    market_state: Optional[Mapping[str, Any]] = None,
    cfg: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """对单个标的判 S1，peers 必须含 record 自身（同板块、同交易日的梯队）。

    返回 {schema, status, surprise, conditions[], reasons[]}。
    status ∈ signal / no_signal / unavailable —— 缺任一必需证据一律 unavailable。
    """
    settings = dict(cfg if cfg is not None else config())
    peer_list = list(peers or [])
    auction_field = str(settings.get("auction_strength_field", "auction_strength"))
    code = str(record.get("code") or "")
    reasons: list[str] = []
    if not str(record.get("sector") or "").strip():
        reasons.append("sector_missing")
    minimum = int(settings.get("min_peer_count", 5))
    if len(peer_list) < minimum:
        reasons.append(f"peer_sample_insufficient({len(peer_list)}<{minimum})")

    index, own_prior, own_auction = _own_rank_pcts(record, peer_list, settings)
    if index is None:
        reasons.append("record_not_in_peer_group")
    rank_conditions, rank_reasons = _rank_conditions(own_prior, own_auction, settings)
    evidence_conditions, evidence_reasons = _evidence_conditions(record, market_state, settings)
    conditions = rank_conditions + evidence_conditions
    reasons.extend(rank_reasons)
    reasons.extend(evidence_reasons)

    peer_gaps = [p.get(auction_field) for i, p in enumerate(peer_list) if i != index]
    delta = surprise(
        actual_gap=record.get(auction_field),
        peer_gaps=peer_gaps,
        prior_return_pct=record.get("prior_return_pct"),
        board_height=record.get("board_height"),
        cfg=settings,
    )
    if delta["status"] != "ok":
        reasons.extend(f"expected_gap:{r}" for r in delta.get("reasons") or [])
    degraded = _degradations(record, settings)

    if reasons:
        status = STATUS_UNAVAILABLE
    elif all(c["ok"] for c in conditions) and delta["status"] == "ok":
        status = STATUS_SIGNAL
    else:
        status = STATUS_NO_SIGNAL
    return {
        "schema": SCHEMA,
        "code": code,
        "sector": record.get("sector"),
        "date": record.get("date"),
        "status": status,
        "surprise": delta,
        "conditions": conditions,
        "reasons": reasons,
        "degraded": degraded,
        "peer_count": len(peer_list),
        "influences_live_ranking": False,
        "note": "S1 未在 strategy_registry 注册；输出仅供研究/回测，不得进入实盘排序或仓位",
    }


def evaluate_group(
    records: Sequence[Mapping[str, Any]],
    *,
    market_state: Optional[Mapping[str, Any]] = None,
    cfg: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """对一个（板块, 交易日）梯队内的全部标的逐个判 S1。"""
    settings = dict(cfg if cfg is not None else config())
    peers = list(records or [])
    return [evaluate(r, peers=peers, market_state=market_state, cfg=settings) for r in peers]


def evaluate_universe(
    records: Sequence[Mapping[str, Any]],
    *,
    market_state: Optional[Mapping[str, Any]] = None,
    cfg: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """按 (date, sector) 分组后逐组判 S1；板块缺失的记录单独成组必然 unavailable。"""
    settings = dict(cfg if cfg is not None else config())
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for record in records or []:
        key = (str(record.get("date") or ""), str(record.get("sector") or ""))
        groups.setdefault(key, []).append(record)
    out: list[dict[str, Any]] = []
    for key in sorted(groups):
        out.extend(evaluate_group(groups[key], market_state=market_state, cfg=settings))
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
