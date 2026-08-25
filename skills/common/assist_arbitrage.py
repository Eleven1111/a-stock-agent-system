#!/usr/bin/env python3
"""
S3 最强助攻套利（AssistArbitrage）信号 — 升级方案 §6.1，NON-LIVE 研究层
=========================================================================
信号：LeaderScore ≥ 80（复用 P2 已合入的 leader_score_shadow，不再造第二份龙头分）∧
板块广度 ≥ 3 ∧ 候选连板高度 ≤ 龙头连板高度 − 1（助攻股必须比龙头矮至少一级）∧
候选相对强度位于题材 Top 20% ∧ 龙头已确认后候选率先突破日内关键位。

本策略的成败点（务必读完再改代码）
----------------------------------
助攻股没有独立的持有理由——它的价值完全来自龙头/主线。因此退出条件不是解释层的
文档描述，是这个模块的一部分：``exit_signal`` 与入场 ``evaluate`` 完全独立评估，
即使候选自身量价依然很强（entry 仍是 signal），只要龙头走弱 ∧ 题材广度下降，或
新主线 DirectionScore 超原主线 ``min_rotation_gap``（默认15分），都必须给出退出，
调用方不得因为候选自身强势就继续持有。

LeaderScore 复用纪律：本模块**不**重新实现六因子评分，只读龙头记录上已经挂好的
``leader_score_shadow``（skills/common/hot_money_selection.leader_score 的输出）。
不可得（缺失/status != ok）时本策略的该条件一律 unavailable，绝不退化成"默认合格"
或自己按其它字段拍一个替代分——两份龙头分实现迟早分叉。

fail-closed 纪律（缺证据 ≠ 不触发）：龙头记录缺失、LeaderScore 不可得、板块广度/
连板高度/相对强度/突破时刻缺失 → status="unavailable" 并给出 reasons，**绝不返回
no_signal**。把"没数据"折叠成"不触发"会让零样本看起来像已验证的负结果，是假绿
的一种。

阈值单一事实源：config/daban_thresholds.yaml 的 assist_arbitrage 节
（缺失回退 daban_config.DEFAULTS，同值）。

红线：本模块未在 strategy_registry 注册，任何调用方都不得把它的输出折进实盘排序、
评分或仓位。升级路径见 config/strategy_packs/assist_arbitrage.yaml。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

import daban_config as _cfg

SCHEMA = "assist_arbitrage_signal_v1"

STATUS_SIGNAL = "signal"
STATUS_NO_SIGNAL = "no_signal"
STATUS_UNAVAILABLE = "unavailable"

STATUS_EXIT = "exit"
STATUS_HOLD = "hold"
# 退出判定与入场共用 STATUS_UNAVAILABLE：证据不足时既不能说"该退"也不能说"该留"。

# 入场条件 id（方案 §6.1 四条主条件）—— 报告/测试按 id 逐条断言，避免用中文串匹配。
COND_LEADER_SCORE = "leader_score_min"
COND_SECTOR_BREADTH = "sector_breadth_min"
COND_BOARD_LEVEL_GAP = "board_level_below_leader"
COND_RELATIVE_STRENGTH = "relative_strength_top20"
CONDITION_IDS = (
    COND_LEADER_SCORE, COND_SECTOR_BREADTH, COND_BOARD_LEVEL_GAP, COND_RELATIVE_STRENGTH,
)
# 入场触发（不是四条主条件之一，是方案原文"入场："那一句：龙头确认后候选率先突破
# 日内关键位）。缺失同样 fail-closed，但单列 id 避免和上面四条混在一起统计。
COND_ENTRY_TRIGGER = "leader_confirmed_breakout_first"


def config(path: Optional[str] = None) -> dict[str, Any]:
    """取 assist_arbitrage 阈值（yaml 覆盖 DEFAULTS）。"""
    return _cfg.section("assist_arbitrage", path)


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


def _code(value: Any) -> str:
    return str(value or "")


# --------------------------------------------------------------------------- #
# 龙头识别 —— 板块×交易日候选池内连板高度最高者；缺连板高度信息一律不选
# --------------------------------------------------------------------------- #
def pick_leader(peers: Sequence[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    """在同板块同交易日候选池中挑连板高度最高者作为龙头；并列按 code 升序取最小。

    没有任何一个 peer 带可用的 board_height 时返回 None（不可判定龙头，不是"没有
    龙头"）——调用方据此把依赖龙头的条件全部 fail-closed 成 unavailable。
    """
    candidates = [
        (p, _num(p.get("board_height"))) for p in (peers or [])
    ]
    candidates = [(p, h) for p, h in candidates if h is not None]
    if not candidates:
        return None
    best_height = max(h for _p, h in candidates)
    tied = [p for p, h in candidates if h == best_height]
    tied.sort(key=lambda p: _code(p.get("code")))
    return dict(tied[0])


# --------------------------------------------------------------------------- #
# 条件 1：LeaderScore ≥ 80 —— 只读 leader_score_shadow，不重算
# --------------------------------------------------------------------------- #
def _condition(cid: str, ok: Optional[bool], detail: str) -> dict[str, Any]:
    return {"id": cid, "ok": ok, "detail": detail}


def leader_score_condition(
    leader: Optional[Mapping[str, Any]], settings: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    minimum = float(settings.get("min_leader_score", 80.0))
    if leader is None:
        return (_condition(COND_LEADER_SCORE, None, "龙头不可判定"),
                ["leader_missing"])
    shadow = leader.get("leader_score_shadow")
    if not isinstance(shadow, Mapping) or shadow.get("status") != "ok" or shadow.get("score") is None:
        return (_condition(COND_LEADER_SCORE, None, "龙头 LeaderScore 不可得"),
                ["leader_score_shadow_unavailable"])
    score = _num(shadow.get("score"))
    if score is None:
        return (_condition(COND_LEADER_SCORE, None, "龙头 LeaderScore 不可得"),
                ["leader_score_shadow_unavailable"])
    ok = score >= minimum
    return (_condition(
        COND_LEADER_SCORE, ok, f"龙头LeaderScore{score:.2f}{'≥' if ok else '<'}{minimum:g}"), [])


# --------------------------------------------------------------------------- #
# 条件 2：板块广度 ≥ 3
# --------------------------------------------------------------------------- #
def sector_breadth_condition(
    record: Mapping[str, Any], settings: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    minimum = float(settings.get("min_sector_breadth_count", 3))
    breadth = _num(record.get("sector_breadth_count"))
    if breadth is None:
        return (_condition(COND_SECTOR_BREADTH, None, "板块广度不可用"),
                ["sector_breadth_count_missing"])
    ok = breadth >= minimum
    return (_condition(
        COND_SECTOR_BREADTH, ok, f"板块广度{breadth:g}{'≥' if ok else '<'}{minimum:g}"), [])


# --------------------------------------------------------------------------- #
# 条件 3：候选连板高度 ≤ 龙头连板高度 − min_board_level_gap（默认1，即至少矮一级）
# --------------------------------------------------------------------------- #
def board_level_condition(
    candidate: Mapping[str, Any],
    leader: Optional[Mapping[str, Any]],
    settings: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    gap = float(settings.get("min_board_level_gap", 1))
    if leader is None:
        return (_condition(COND_BOARD_LEVEL_GAP, None, "龙头不可判定"), ["leader_missing"])
    candidate_level = _num(candidate.get("board_height"))
    leader_level = _num(leader.get("board_height"))
    if candidate_level is None or leader_level is None:
        return (_condition(COND_BOARD_LEVEL_GAP, None, "连板高度不可用"),
                ["board_height_missing"])
    threshold = leader_level - gap
    ok = candidate_level <= threshold
    return (_condition(
        COND_BOARD_LEVEL_GAP, ok,
        f"候选连板{candidate_level:g}{'≤' if ok else '>'}龙头{leader_level:g}-{gap:g}"), [])


# --------------------------------------------------------------------------- #
# 条件 4：候选相对强度位于题材 Top 20%（横截面分位，0.0=最弱，1.0=最强）
# --------------------------------------------------------------------------- #
def percentile_ranks(keys: Sequence[Any]) -> list[Optional[float]]:
    """把强度键映射成 [0,1] 分位（严格更弱者占比）；样本量 <2 或含 None 全部返回 None。"""
    total = len(keys)
    if total < 2 or any(key is None for key in keys):
        return [None] * total
    ranks: list[Optional[float]] = []
    for key in keys:
        weaker = sum(1 for other in keys if other < key)
        ranks.append(weaker / (total - 1))
    return ranks


def _own_relative_strength_pct(
    candidate: Mapping[str, Any],
    peer_list: Sequence[Mapping[str, Any]],
    settings: Mapping[str, Any],
) -> tuple[Optional[int], Optional[float]]:
    field = str(settings.get("relative_strength_field", "change_pct"))
    code = _code(candidate.get("code"))
    index = next(
        (i for i, p in enumerate(peer_list) if code and _code(p.get("code")) == code), None,
    )
    if index is None:
        return None, None
    pcts = percentile_ranks([_num(p.get(field)) for p in peer_list])
    return index, pcts[index]


def relative_strength_condition(
    pct: Optional[float], settings: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    top_pct = float(settings.get("relative_strength_top_pct", 0.20))
    if pct is None:
        return (_condition(COND_RELATIVE_STRENGTH, None, "题材内相对强度分位不可用"),
                ["relative_strength_rank_unavailable"])
    ok = pct >= 1.0 - top_pct
    return (_condition(
        COND_RELATIVE_STRENGTH, ok,
        f"相对强度分位{pct:.3f}{'≥' if ok else '<'}{1.0 - top_pct:g}"), [])


# --------------------------------------------------------------------------- #
# 入场触发：龙头已确认（已封板）∧ 候选在题材内率先突破日内关键位
# --------------------------------------------------------------------------- #
def breakout_rank(peers: Sequence[Mapping[str, Any]]) -> list[Optional[int]]:
    """按 breakout_time 升序给出 1-based 名次；未突破(breakout_time 缺失/非法)为 None。

    只吃 breakout_time——与回封判定同构（见 divergence_reseal.reseal_rank）：
    "率先"必须按截至该时刻的信息判定，不掺入任何后续结果字段。
    """
    keyed = [
        (i, _minutes(p.get("breakout_time")), _code(p.get("code")))
        for i, p in enumerate(peers)
    ]
    broke_out = sorted(
        (item for item in keyed if item[1] is not None),
        key=lambda item: (item[1], item[2]),
    )
    ranks: list[Optional[int]] = [None] * len(peers)
    for rank, (index, _minute, _code_) in enumerate(broke_out, start=1):
        ranks[index] = rank
    return ranks


def _own_breakout_rank(
    candidate: Mapping[str, Any], peer_list: Sequence[Mapping[str, Any]],
) -> tuple[Optional[int], Optional[int]]:
    code = _code(candidate.get("code"))
    index = next(
        (i for i, p in enumerate(peer_list) if code and _code(p.get("code")) == code), None,
    )
    if index is None:
        return None, None
    ranks = breakout_rank(peer_list)
    return index, ranks[index]


def entry_trigger_condition(
    candidate: Mapping[str, Any],
    peer_list: Sequence[Mapping[str, Any]],
    leader: Optional[Mapping[str, Any]],
    settings: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    top_n = int(settings.get("breakout_rank_top_n", 1))
    if leader is None:
        return (_condition(COND_ENTRY_TRIGGER, None, "龙头不可判定"), ["leader_missing"])
    leader_confirmed = leader.get("leader_confirmed")
    if leader_confirmed is None:
        return (_condition(COND_ENTRY_TRIGGER, None, "龙头是否已确认不可知"),
                ["leader_confirmed_unknown"])
    index, rank = _own_breakout_rank(candidate, peer_list)
    if index is None:
        return (_condition(COND_ENTRY_TRIGGER, None, "候选不在题材同组中"),
                ["record_not_in_theme_peer_group"])
    if rank is None:
        return (_condition(COND_ENTRY_TRIGGER, None, "突破时刻缺失/未突破"),
                ["breakout_time_missing_or_not_broken_out"])
    ok = bool(leader_confirmed) and rank <= top_n
    detail = f"龙头确认={bool(leader_confirmed)}∧突破先后名次{rank}{'≤' if rank <= top_n else '>'}{top_n}"
    return _condition(COND_ENTRY_TRIGGER, ok, detail), []


# --------------------------------------------------------------------------- #
# 单标的入场信号
# --------------------------------------------------------------------------- #
def evaluate(
    candidate: Mapping[str, Any],
    *,
    leader: Optional[Mapping[str, Any]],
    peers: Sequence[Mapping[str, Any]],
    cfg: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """对单个候选判 S3，peers 必须含 candidate 自身（同题材、同交易日的候选池）。

    leader 由调用方给出（通常是 pick_leader(peers) 的结果，也允许显式指定角色）；
    缺失一律让所有依赖龙头的条件 unavailable，绝不假设"没有龙头就不是龙头股票池"。

    返回 {schema, status, conditions[], reasons[]}。
    status ∈ signal / no_signal / unavailable —— 缺任一必需证据一律 unavailable。
    """
    settings = dict(cfg if cfg is not None else config())
    peer_list = list(peers or [])
    code = _code(candidate.get("code"))
    reasons: list[str] = []
    if not str(candidate.get("sector") or "").strip():
        reasons.append("sector_missing")
    minimum = int(settings.get("min_theme_peer_count", 5))
    if len(peer_list) < minimum:
        reasons.append(f"theme_peer_sample_insufficient({len(peer_list)}<{minimum})")

    leader_cond, leader_reasons = leader_score_condition(leader, settings)
    breadth_cond, breadth_reasons = sector_breadth_condition(candidate, settings)
    level_cond, level_reasons = board_level_condition(candidate, leader, settings)
    index, pct = _own_relative_strength_pct(candidate, peer_list, settings)
    if index is None:
        reasons.append("record_not_in_theme_peer_group")
    rs_cond, rs_reasons = relative_strength_condition(pct, settings)
    trigger_cond, trigger_reasons = entry_trigger_condition(candidate, peer_list, leader, settings)

    conditions = [leader_cond, breadth_cond, level_cond, rs_cond, trigger_cond]
    reasons.extend(leader_reasons)
    reasons.extend(breadth_reasons)
    reasons.extend(level_reasons)
    reasons.extend(rs_reasons)
    reasons.extend(trigger_reasons)
    # 去重但保序，reasons 里 leader_missing 可能被多个条件重复追加。
    seen: set[str] = set()
    deduped_reasons = [r for r in reasons if not (r in seen or seen.add(r))]

    if deduped_reasons:
        status = STATUS_UNAVAILABLE
    elif all(c["ok"] for c in conditions):
        status = STATUS_SIGNAL
    else:
        status = STATUS_NO_SIGNAL

    leader_score_value = None
    if isinstance(leader, Mapping):
        shadow = leader.get("leader_score_shadow")
        if isinstance(shadow, Mapping):
            leader_score_value = shadow.get("score")

    return {
        "schema": SCHEMA,
        "code": code,
        "sector": candidate.get("sector"),
        "date": candidate.get("date"),
        "status": status,
        "leader_code": _code(leader.get("code")) if isinstance(leader, Mapping) else None,
        "leader_score": leader_score_value,
        "relative_strength_pct": pct,
        "conditions": conditions,
        "reasons": deduped_reasons,
        "degraded": [],
        "peer_count": len(peer_list),
        "influences_live_ranking": False,
        "note": "S3 未在 strategy_registry 注册；输出仅供研究/回测，不得进入实盘排序或仓位",
    }


def evaluate_group(
    records: Sequence[Mapping[str, Any]],
    *,
    cfg: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """对一个（题材, 交易日）候选池评估 S3：先选龙头，再把其余成员当候选逐个判。

    龙头本身不作为候选参与判定（它不是"助攻别人的人"）。
    """
    settings = dict(cfg if cfg is not None else config())
    peers = list(records or [])
    leader = pick_leader(peers)
    leader_code = _code(leader.get("code")) if leader is not None else None
    return [
        evaluate(r, leader=leader, peers=peers, cfg=settings)
        for r in peers
        if not leader_code or _code(r.get("code")) != leader_code
    ]


def evaluate_universe(
    records: Sequence[Mapping[str, Any]],
    *,
    cfg: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """按 (date, sector) 分组后逐组判 S3；板块缺失的记录单独成组必然 unavailable。"""
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


# =========================================================================== #
# 退出条件 —— 本策略的成败点，独立于入场判定，硬编码在代码里（不是文档描述）
# =========================================================================== #
EXIT_SCHEMA = "assist_arbitrage_exit_v1"


def leader_weakening(
    record: Mapping[str, Any], settings: Mapping[str, Any],
) -> dict[str, Any]:
    """龙头是否走弱：龙头开板/失守（``leader_board_broken``）或当日涨跌幅跌破阈值。

    两个信号字段都缺失 → unavailable；任一可用即可判定（不要求两者都有，
    否则会把"只知道开板"这种部分证据也当不可用而丢掉）。
    """
    broken = record.get("leader_board_broken")
    change = _num(record.get("leader_change_pct"))
    if broken is None and change is None:
        return {"status": STATUS_UNAVAILABLE, "weak": None,
                "reasons": ["leader_weakening_evidence_missing"]}
    threshold = float(settings.get("leader_weak_change_pct_max", -3.0))
    weak = bool(broken is True or (change is not None and change <= threshold))
    return {"status": "ok", "weak": weak, "leader_board_broken": broken,
            "leader_change_pct": change, "reasons": []}


def breadth_declining(
    record: Mapping[str, Any], settings: Mapping[str, Any],
) -> dict[str, Any]:
    """题材广度是否下降：当前板块涨停家数相对对照时点下降 ≥ 阈值家数。

    当前值或对照值任一缺失 → unavailable（不用"没有对照就当没降"顶替）。
    """
    current = _num(record.get("sector_breadth_count"))
    prior = _num(record.get("sector_breadth_count_prior"))
    if current is None or prior is None:
        return {"status": STATUS_UNAVAILABLE, "declining": None,
                "reasons": ["sector_breadth_trend_missing"]}
    min_drop = float(settings.get("breadth_decline_min_drop", 1.0))
    declining = (prior - current) >= min_drop
    return {"status": "ok", "declining": declining, "current": current, "prior": prior,
            "drop": round(prior - current, 6), "reasons": []}


def mainline_rotation(
    record: Mapping[str, Any], settings: Mapping[str, Any],
) -> dict[str, Any]:
    """新主线 DirectionScore 是否超原主线 ``min_rotation_gap``（默认15分）。

    两个 DirectionScore 任一缺失 → unavailable。
    """
    original = _num(record.get("original_mainline_direction_score"))
    new = _num(record.get("new_mainline_direction_score"))
    if original is None or new is None:
        return {"status": STATUS_UNAVAILABLE, "triggered": None,
                "reasons": ["mainline_direction_score_missing"]}
    min_gap = float(settings.get("min_rotation_gap", 15.0))
    gap = new - original
    triggered = gap >= min_gap
    return {"status": "ok", "triggered": triggered, "gap": round(gap, 6),
            "original": original, "new": new, "reasons": []}


def exit_signal(
    record: Mapping[str, Any],
    *,
    cfg: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """独立于入场判定的退出信号——助攻股没有独立持有理由，只要满足以下任一路径
    就必须退出，即使候选自身量价依然很强（entry 仍可能是 signal）：

      A) 龙头走弱 ∧ 题材广度下降
      B) 新主线 DirectionScore 超原主线 ``min_rotation_gap``

    status ∈ exit / hold / unavailable：只有当能排除两条路径（两组证据都齐全且
    均未触发）才敢报 hold；任一路径证据不足且另一路径也未触发时报 unavailable，
    绝不用"证据不足"顶替"可以继续持有"。
    """
    settings = dict(cfg if cfg is not None else config())
    weak = leader_weakening(record, settings)
    breadth = breadth_declining(record, settings)
    rotation = mainline_rotation(record, settings)

    leader_path_available = weak["status"] == "ok" and breadth["status"] == "ok"
    leader_path_exit = bool(leader_path_available and weak["weak"] and breadth["declining"])
    rotation_available = rotation["status"] == "ok"
    rotation_exit = bool(rotation_available and rotation["triggered"])

    reasons: list[str] = []
    reasons.extend(weak.get("reasons") or [])
    reasons.extend(breadth.get("reasons") or [])
    reasons.extend(rotation.get("reasons") or [])

    if leader_path_exit or rotation_exit:
        status = STATUS_EXIT
    elif leader_path_available and rotation_available:
        status = STATUS_HOLD
    else:
        status = STATUS_UNAVAILABLE

    return {
        "schema": EXIT_SCHEMA,
        "code": _code(record.get("code")),
        "date": record.get("date"),
        "status": status,
        "leader_weakening": weak,
        "breadth_declining": breadth,
        "mainline_rotation": rotation,
        "reasons": reasons,
        "note": "S3 退出判定独立于入场信号；候选自身强势不能否决龙头走弱+广度下降的退出",
    }
