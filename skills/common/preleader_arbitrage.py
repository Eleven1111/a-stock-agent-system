#!/usr/bin/env python3
"""
S4 先于龙头套利（PreleaderArbitrage）信号 — 升级方案 §6.1，NON-LIVE 研究层
=========================================================================
原书航天通信案例的精髓不是"买某只票"，而是纪律：**D-1 晚间**已经建立
「龙头候选 → 属性 → 同属性首板候选」的映射表（并在建表时排除重大利空、流动性
不足的成分），**D0 盘中只负责"确认"**——龙头一旦确认，只允许买盘前表内已经列
好的候选，不做临时选股。2019-01 鹏起科技开盘快速确认后，留给判断航天通信的时间
大约只有数分钟，这就是本策略成立的前提：盘中没有时间重新构建候选池。

本策略的成败点（务必读完再改代码）
----------------------------------
"盘前表"必须是真的盘前产物，不能在盘中现算。如果实现允许 D0 当天临时构造候选集，
就等于用当日信息选样本，"反应时间"这个前提就不成立，回测也会虚高。因此：

  1) ``build_pretable`` 只吃 ``as_of``（D-1 日期）及更早的数据——任何
     ``date > as_of`` 的输入记录一律被丢弃，绝不掺进候选池或排除表；
  2) 盘前表自带 ``generated_at``（构表时刻）与 ``as_of``（D-1 日期）两个时间戳；
  3) ``evaluate`` 校验候选必须出现在盘前表对应（龙头, 属性）条目的候选列表里，
     不在表内一律 **不触发**（``status=no_signal``，reason=
     ``not_in_pretable_entry`` / 条目本身不存在），绝不允许调用方把候选临时补
     进表里再判定——不在表内就是不在表内。
  4) 同时校验盘前表的 ``as_of`` 严格早于候选的交易日（``COND_PRETABLE_FRESH``）：
     如果调用方把一张当日现算的表伪装成"盘前表"传进来，这条条件会失败而不是
     被静默接受。

属性映射优先复用既有 ``theme_registry``（L3 动态题材登记）或既有板块字段
（``sector``），本模块不自造第三套题材体系——``attribute`` 字段的取值由调用方
决定填题材 id 还是 sector 名，模块本身只做等值匹配，不关心其语义来源。

fail-closed 纪律（缺证据 ≠ 不触发）：属性缺失、龙头不可判定、龙头确认状态/时刻
缺失、候选流动性缺失、盘前表 as_of 缺失 → status="unavailable" 并给出 reasons，
绝不返回 no_signal。"没数据"和"明确不满足/不在表内"是两种不同的结论，混淆二者
会让零样本看起来像已验证的负结果，是假绿的一种。

阈值单一事实源：config/daban_thresholds.yaml 的 preleader_arbitrage 节
（缺失回退 daban_config.DEFAULTS，同值）。

红线：本模块未在 strategy_registry 注册，任何调用方都不得把它的输出折进实盘
排序、评分或仓位。升级路径见 config/strategy_packs/preleader_arbitrage.yaml。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Sequence

import daban_config as _cfg

PRETABLE_SCHEMA = "preleader_arbitrage_pretable_v1"
SCHEMA = "preleader_arbitrage_signal_v1"

STATUS_SIGNAL = "signal"
STATUS_NO_SIGNAL = "no_signal"
STATUS_UNAVAILABLE = "unavailable"

# 入场条件 id（方案 §6.1，四条：盘前表新鲜度→盘前表候选成员资格→龙头确认反应
# 窗口→候选流动性）—— 报告/测试按 id 逐条断言，避免用中文串匹配。
COND_PRETABLE_FRESH = "pretable_generated_before_d0"
COND_PRETABLE_MEMBERSHIP = "candidate_in_pretable"
COND_REACTION_WINDOW = "leader_confirmed_reaction_window"
COND_LIQUIDITY = "candidate_liquidity_min"
CONDITION_IDS = (
    COND_PRETABLE_FRESH, COND_PRETABLE_MEMBERSHIP, COND_REACTION_WINDOW, COND_LIQUIDITY,
)


def config(path: Optional[str] = None) -> dict[str, Any]:
    """取 preleader_arbitrage 阈值（yaml 覆盖 DEFAULTS）。"""
    return _cfg.section("preleader_arbitrage", path)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


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


def _attr(value: Any) -> str:
    return str(value or "").strip()


def _date(value: Any) -> str:
    return str(value or "")


def _condition(cid: str, ok: Optional[bool], detail: str) -> dict[str, Any]:
    return {"id": cid, "ok": ok, "detail": detail}


# =========================================================================== #
# 盘前表构造 —— 只用 D-1 及更早数据，本策略的成败点
# =========================================================================== #
def build_pretable(
    leader_records: Sequence[Mapping[str, Any]],
    member_records: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
    generated_at: Optional[str] = None,
    cfg: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """构建 D-1 晚间的「龙头候选 → 属性 → 同属性候选」映射表。

    ``leader_records``：D-1（及更早）识别出的龙头候选，形如
    ``{code, attribute, date}``；``member_records``：属性成分股候选池，形如
    ``{code, name, attribute, date, is_st, avg_turnover_20d, material_bad_news}``。

    只用 ``date <= as_of`` 的记录——任何 ``date > as_of`` 的输入（含 D0 当天
    喂进来的记录）一律被丢弃，不参与候选池也不参与排除表，这是"表是真盘前产物"
    的核心保证：喂 D0 数据进来不会改变输出。

    候选池排除重大利空（``material_bad_news`` 为真）与流动性不足
    （``avg_turnover_20d`` 缺失或低于 ``min_member_avg_turnover``），排除原因
    记在 ``excluded`` 里，不做静默丢弃。
    """
    settings = dict(cfg if cfg is not None else config())
    as_of_key = _date(as_of)
    min_turnover = float(settings.get("min_member_avg_turnover", 0.0))

    leaders = [
        leader for leader in (leader_records or [])
        if _date(leader.get("date")) and _date(leader.get("date")) <= as_of_key
    ]
    members_by_attr: dict[str, list[Mapping[str, Any]]] = {}
    for member in member_records or []:
        member_date = _date(member.get("date"))
        if not member_date or member_date > as_of_key:
            continue
        attribute = _attr(member.get("attribute"))
        if not attribute:
            continue
        members_by_attr.setdefault(attribute, []).append(member)

    entries: list[dict[str, Any]] = []
    for leader in leaders:
        leader_code = _code(leader.get("code"))
        attribute = _attr(leader.get("attribute"))
        if not leader_code or not attribute:
            continue
        pool = members_by_attr.get(attribute, [])
        candidates: list[str] = []
        excluded: list[dict[str, str]] = []
        seen_codes: set[str] = set()
        for member in pool:
            member_code = _code(member.get("code"))
            if not member_code or member_code == leader_code or member_code in seen_codes:
                continue
            seen_codes.add(member_code)
            if member.get("is_st"):
                excluded.append({"code": member_code, "reason": "is_st"})
                continue
            if member.get("material_bad_news"):
                excluded.append({"code": member_code, "reason": "material_bad_news"})
                continue
            turnover = _num(member.get("avg_turnover_20d"))
            if turnover is None or turnover < min_turnover:
                excluded.append({"code": member_code, "reason": "insufficient_liquidity"})
                continue
            candidates.append(member_code)
        entries.append({
            "leader_code": leader_code,
            "attribute": attribute,
            "candidates": sorted(candidates),
            "excluded": excluded,
        })

    return {
        "schema": PRETABLE_SCHEMA,
        "as_of": as_of_key,
        "generated_at": generated_at or _now_iso(),
        "entries": entries,
    }


def lookup_entry(
    pretable: Optional[Mapping[str, Any]], leader_code: Any, attribute: Any,
) -> Optional[Mapping[str, Any]]:
    """在盘前表里找 (leader_code, attribute) 对应条目；找不到返回 None。"""
    if not isinstance(pretable, Mapping):
        return None
    leader_code_key = _code(leader_code)
    attribute_key = _attr(attribute)
    if not leader_code_key or not attribute_key:
        return None
    for entry in pretable.get("entries") or []:
        if (_code(entry.get("leader_code")) == leader_code_key
                and _attr(entry.get("attribute")) == attribute_key):
            return entry
    return None


# =========================================================================== #
# 龙头识别（D0）—— 组内已确认(confirmed=True)者，取确认时刻最早的一个
# =========================================================================== #
def pick_confirmed_leader(peers: Sequence[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    """在同属性同交易日候选池中挑已确认(``confirmed`` 为真)且确认时刻最早者。

    没有任何一个 peer 带可用的 ``confirmed=True`` + ``confirmed_time`` 时返回
    None（不可判定龙头，不是"没有龙头"）——调用方据此把依赖龙头的条件全部
    fail-closed 成 unavailable。并列按 code 升序取最小。
    """
    candidates = [
        (peer, _minutes(peer.get("confirmed_time")))
        for peer in (peers or []) if peer.get("confirmed") is True
    ]
    candidates = [(peer, minute) for peer, minute in candidates if minute is not None]
    if not candidates:
        return None
    best_minute = min(minute for _peer, minute in candidates)
    tied = [peer for peer, minute in candidates if minute == best_minute]
    tied.sort(key=lambda peer: _code(peer.get("code")))
    return dict(tied[0])


# --------------------------------------------------------------------------- #
# 条件 1：盘前表必须真的是盘前产物（as_of 严格早于候选交易日）
# --------------------------------------------------------------------------- #
def pretable_fresh_condition(
    pretable: Optional[Mapping[str, Any]], candidate_date: Any,
) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(pretable, Mapping):
        return (_condition(COND_PRETABLE_FRESH, None, "盘前表缺失"), ["pretable_missing"])
    as_of = _date(pretable.get("as_of"))
    date_key = _date(candidate_date)
    if not as_of or not date_key:
        return (_condition(COND_PRETABLE_FRESH, None, "盘前表日期或候选交易日缺失"),
                ["pretable_as_of_or_candidate_date_missing"])
    ok = as_of < date_key
    return (_condition(
        COND_PRETABLE_FRESH, ok,
        f"盘前表as_of={as_of}{'<' if ok else '>='}候选交易日={date_key}"), [])


# --------------------------------------------------------------------------- #
# 条件 2：候选必须出现在盘前表对应(龙头,属性)条目的候选列表里
# --------------------------------------------------------------------------- #
def pretable_membership_condition(
    pretable: Optional[Mapping[str, Any]],
    leader_code: Optional[str],
    attribute: Any,
    code: str,
) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(pretable, Mapping):
        return (_condition(COND_PRETABLE_MEMBERSHIP, None, "盘前表缺失"), ["pretable_missing"])
    if not leader_code:
        return (_condition(COND_PRETABLE_MEMBERSHIP, None, "龙头不可判定"), ["leader_missing"])
    if not _attr(attribute):
        return (_condition(COND_PRETABLE_MEMBERSHIP, None, "属性缺失"), ["attribute_missing"])
    entry = lookup_entry(pretable, leader_code, attribute)
    if entry is None:
        return (_condition(
            COND_PRETABLE_MEMBERSHIP, False, "盘前表内无此(龙头,属性)条目"), [])
    ok = code in (entry.get("candidates") or [])
    return (_condition(
        COND_PRETABLE_MEMBERSHIP, ok,
        f"候选{'在' if ok else '不在'}盘前表候选列表内"), [])


# --------------------------------------------------------------------------- #
# 条件 3：龙头已确认 ∧ 候选须在确认后 max_reaction_minutes 分钟内完成自身反应
# --------------------------------------------------------------------------- #
def reaction_window_condition(
    leader: Optional[Mapping[str, Any]],
    evaluation_time: Any,
    settings: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    max_minutes = float(settings.get("max_reaction_minutes", 10))
    if leader is None:
        return (_condition(COND_REACTION_WINDOW, None, "龙头不可判定"), ["leader_missing"])
    confirmed = leader.get("confirmed")
    if confirmed is None:
        return (_condition(COND_REACTION_WINDOW, None, "龙头是否已确认不可知"),
                ["leader_confirmed_unknown"])
    if not confirmed:
        return (_condition(COND_REACTION_WINDOW, False, "龙头尚未确认"), [])
    confirmed_minute = _minutes(leader.get("confirmed_time"))
    eval_minute = _minutes(evaluation_time)
    if confirmed_minute is None or eval_minute is None:
        return (_condition(COND_REACTION_WINDOW, None, "龙头确认时刻或候选反应时刻缺失"),
                ["reaction_time_missing"])
    elapsed = eval_minute - confirmed_minute
    if elapsed < 0:
        return (_condition(
            COND_REACTION_WINDOW, False, "候选反应时刻早于龙头确认时刻"), [])
    ok = elapsed <= max_minutes
    return (_condition(
        COND_REACTION_WINDOW, ok,
        f"确认后经过{elapsed:g}分钟{'≤' if ok else '>'}{max_minutes:g}"), [])


# --------------------------------------------------------------------------- #
# 条件 4：候选流动性 ≥ 下限（build 阶段已排除的是成分库；这里对候选自身再做一次
# 运行期兜底，避免调用方绕过 build_pretable 直接拼表）
# --------------------------------------------------------------------------- #
def liquidity_condition(
    candidate: Mapping[str, Any], settings: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    minimum = float(settings.get("min_candidate_amount", 0.0))
    amount = _num(candidate.get("amount"))
    if amount is None:
        return (_condition(COND_LIQUIDITY, None, "候选流动性(成交额)不可用"),
                ["candidate_liquidity_missing"])
    ok = amount >= minimum
    return (_condition(
        COND_LIQUIDITY, ok, f"候选成交额{amount:g}{'≥' if ok else '<'}{minimum:g}"), [])


# --------------------------------------------------------------------------- #
# 单标的入场信号
# --------------------------------------------------------------------------- #
def evaluate(
    candidate: Mapping[str, Any],
    *,
    leader: Optional[Mapping[str, Any]],
    pretable: Optional[Mapping[str, Any]],
    cfg: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """对单个候选判 S4。``leader`` 由调用方给出（通常是 pick_confirmed_leader
    的结果）；``pretable`` 是 D-1 晚间构建的盘前表（``build_pretable`` 的产物）。

    返回 {schema, status, conditions[], reasons[]}。
    status ∈ signal / no_signal / unavailable —— 缺证据一律 unavailable；
    "不在盘前表内" 是明确的 no_signal，不是 unavailable。
    """
    settings = dict(cfg if cfg is not None else config())
    code = _code(candidate.get("code"))
    date = candidate.get("date")
    attribute = candidate.get("attribute")
    reasons: list[str] = []
    if not _attr(attribute):
        reasons.append("attribute_missing")

    leader_code = _code(leader.get("code")) if isinstance(leader, Mapping) else None
    if leader is None:
        reasons.append("leader_missing")

    fresh_cond, fresh_reasons = pretable_fresh_condition(pretable, date)
    member_cond, member_reasons = pretable_membership_condition(
        pretable, leader_code, attribute, code)
    window_cond, window_reasons = reaction_window_condition(
        leader, candidate.get("evaluation_time"), settings)
    liquidity_cond, liquidity_reasons = liquidity_condition(candidate, settings)

    conditions = [fresh_cond, member_cond, window_cond, liquidity_cond]
    reasons.extend(fresh_reasons)
    reasons.extend(member_reasons)
    reasons.extend(window_reasons)
    reasons.extend(liquidity_reasons)
    seen: set[str] = set()
    deduped_reasons = [r for r in reasons if not (r in seen or seen.add(r))]

    if deduped_reasons:
        status = STATUS_UNAVAILABLE
    elif all(c["ok"] for c in conditions):
        status = STATUS_SIGNAL
    else:
        status = STATUS_NO_SIGNAL

    return {
        "schema": SCHEMA,
        "code": code,
        "attribute": attribute,
        "date": date,
        "status": status,
        "leader_code": leader_code,
        "conditions": conditions,
        "reasons": deduped_reasons,
        "degraded": [],
        "influences_live_ranking": False,
        "note": "S4 未在 strategy_registry 注册；输出仅供研究/回测，不得进入实盘排序或仓位",
    }


def evaluate_group(
    records: Sequence[Mapping[str, Any]],
    *,
    pretable: Optional[Mapping[str, Any]],
    cfg: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """对一个（属性, 交易日）候选池评估 S4：先挑 D0 已确认的龙头，再把其余成员
    当候选逐个判。龙头本身不作为候选参与判定。"""
    settings = dict(cfg if cfg is not None else config())
    peers = list(records or [])
    leader = pick_confirmed_leader(peers)
    leader_code = _code(leader.get("code")) if leader is not None else None
    return [
        evaluate(record, leader=leader, pretable=pretable, cfg=settings)
        for record in peers
        if not leader_code or _code(record.get("code")) != leader_code
    ]


def evaluate_universe(
    records: Sequence[Mapping[str, Any]],
    *,
    pretable: Optional[Mapping[str, Any]],
    cfg: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """按 (date, attribute) 分组后逐组判 S4；属性缺失的记录单独成组必然
    unavailable。"""
    settings = dict(cfg if cfg is not None else config())
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for record in records or []:
        key = (_date(record.get("date")), _attr(record.get("attribute")))
        groups.setdefault(key, []).append(record)
    out: list[dict[str, Any]] = []
    for key in sorted(groups):
        out.extend(evaluate_group(groups[key], pretable=pretable, cfg=settings))
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
