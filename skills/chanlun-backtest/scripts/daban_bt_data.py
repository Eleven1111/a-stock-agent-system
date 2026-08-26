#!/usr/bin/env python3
"""
打板回测 — 数据层（历史涨停事件 + 次日 K 线 → 事件表）
========================================================
默认 source="akshare" 逐日拉 stock_zt_pool_em（不走 push2，TUN 下可用）取涨停事件，
但免费历史仅最近约 3-4 周；source="mootdx" 走通达信 TCP 全市场日线重建，深历史 6 年+
（仅 H1 gap 假设，first_seal 等盘口字段为 None，见 fetch_limitup_events / mootdx_source）。
收集代码后批量拉日线（akshare→腾讯 ifzq，mootdx→同源同深度），把 T 收 / T+1 开 / T+1 收 join 进事件表。

为保证 gap 口径一致，t_close / t1_open / t1_close 统一取自同一份（qfq）K 线，
zt_pool 只负责事件筛选与 first_seal/连板/封单等元数据。

v4（2026-08）补齐 S1/S2 所需证据字段，逐字段按来源标可得性（见 V4_FIELDS 与
docs_private/event-schema-v4-2026-08.md）。铁律：不同来源可得性不同，缺就标 unavailable，
**绝不用日线代理值伪造**（全日换手率 ≠ 封板前换手，全日量 ≠ 09:45 量比）。

纯函数（kline_lookup / assemble_events / sector_cross_section / turnover_baseline /
derive_reseal_time）可用合成数据单测；触网函数（fetch_limitup_events / fetch_klines）
手动冒烟。
"""

import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path
import daban_config as _cfg  # noqa: E402
import execution_constraints as xc  # noqa: E402
import minute_derived as md  # noqa: E402
import minute_rows_source as mrs  # noqa: E402
from a_stock_http import fetch_tencent_kline, DataSourceError  # noqa: E402
from state_store import read_json, atomic_write_json  # noqa: E402
from paths import data_file  # noqa: E402


# v3 相对 v2 增补 T 日 OHLCV/成交额与 t_prev_close/t1_amount —— P5(a) 成交约束模型
# 判「一字禁买 / 回封参与率 / 跌停承接量」必需的字段。
# v4 相对 v3 增补 S1/S2 两个研究策略所需的证据字段（见下方 V4_FIELDS 与
# docs_private/event-schema-v4-2026-08.md）：上游 zt_pool 早已返回却被 _map_zt_row 丢弃的
# 换手率/最后封板时间/炸板次数、由它们派生的 reseal_time、按 date×sector 聚合的板块
# 横截面家数、以及从已抓 K 线算的 20 日换手基准。
# schema 提级同时让旧缓存自动失效重建（引擎对缺字段是 fail-closed 拒绝成交，
# 静默复用旧表会让样本清零）。
EVENT_SCHEMA = "daban_bt_event_table_v4"

# 字段可得性三态：有值 / 不可得（缺上游数据，绝不造代理值）/ 不适用（语义上不存在）。
AVAILABLE = "available"
UNAVAILABLE = "unavailable"
NOT_APPLICABLE = "not_applicable"

# v4 新增字段清单 —— 每个事件都必须在 field_availability 里对这些字段逐个表态，
# 「悄悄不写这个键」不是合法状态（那正是 v3 让 S1/S2 零命中却看不出原因的老毛病）。
V4_FIELDS = (
    "turnover_pct",
    "last_seal_time",
    "open_board_count",
    "reseal_time",
    "sector_limitup_count",
    "sector_one_word_count",
    "sector_fast_board_count",
    "turnover_baseline_median",
    "turnover_baseline_sample_days",
    "pre_reseal_turnover_pct",
    "volume_ratio",
)

# 两条来源都拿不到、且**不允许用日线代理值冒充**的字段：
#   volume_ratio           —— S1 条件3 要的是 09:45 前量比，日线只有全日量，口径不同。
#   pre_reseal_turnover_pct—— S2 条件4 要的是「封板前累计换手」，日线只有全日换手率。
# 两者都必须分钟线才能算，本版一律 unavailable 并带原因，等分钟线管道到位再补。
MINUTE_BAR_FIELDS = {
    "volume_ratio": "needs_intraday_minute_bars(0945_volume_ratio)",
    "pre_reseal_turnover_pct": "needs_intraday_minute_bars(pre_reseal_cumulative_turnover)",
}


def v4_config(path: Optional[str] = None) -> Dict[str, Any]:
    """事件表 v4 构建口径阈值（yaml 的 event_table_v4 节，缺失回退 DEFAULTS）。"""
    return _cfg.section("event_table_v4", path)


def market_prefix(code: str) -> str:
    """主板代码 → 腾讯市场前缀。60→sh，00→sz。"""
    code = str(code).zfill(6)
    return "sh" if code.startswith("6") else "sz"


def _norm_date(value: Any) -> str:
    """'20260603' / '2026-06-03' → '2026-06-03'。"""
    text = str(value).strip().replace("-", "")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return str(value)


def _float_or_none(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bar_amount(bar: Dict[str, Any]) -> Optional[float]:
    """日线自带的成交额（元）。腾讯 ifzq 日线不带 amount → None，
    由 execution_constraints 用 volume×close×每手股数单口径折算，避免两处各折算一次。"""
    direct = _float_or_none(bar.get("amount"))
    return direct if direct is not None and direct > 0 else None


def _seal_minutes(value: Any) -> Optional[int]:
    """'092500' / '09:25' / '0925' → 分钟数；缺失/非法 → None（不猜）。"""
    if value is None or value == "":
        return None
    text = str(value).strip().replace(":", "")
    if not text.isdigit() or len(text) < 4:
        return None
    return int(text[:2]) * 60 + int(text[2:4])


# --------------------------------------------------------------------------- #
# v4-1：回封时刻 —— 语义是「炸板之后重新封板的时刻」
# --------------------------------------------------------------------------- #
def derive_reseal_time(row: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """(reseal_time, availability)。

    判定（这是本字段唯一正确的语义，改动前先读 tests/test_event_schema_v4.py）：
      - 炸板次数缺失            → unavailable：不知道有没有炸过，不能断言"没回封"。
      - 炸板次数 == 0           → not_applicable，值 None：一次没炸过就**不存在**回封
                                  时刻，把当天的最后封板时间当回封是伪造。
      - 炸板次数 > 0            → 取「最后封板时间」＝最后一次炸板后重新封上的时刻；
                                  该时间缺失 → unavailable。
    """
    count = _float_or_none(row.get("open_board_count"))
    if count is None:
        return None, f"{UNAVAILABLE}:open_board_count_missing"
    if count <= 0:
        return None, f"{NOT_APPLICABLE}:never_opened_board_no_reseal"
    last_seal = row.get("last_seal_time")
    if _seal_minutes(last_seal) is None:
        return None, f"{UNAVAILABLE}:last_seal_time_missing"
    return str(last_seal), AVAILABLE


# --------------------------------------------------------------------------- #
# v4-2：板块横截面聚合 —— 按 date × sector 从当日全量涨停池算，不额外触网
# --------------------------------------------------------------------------- #
def sector_cross_section(raw_events: List[Dict[str, Any]],
                         cfg: Optional[Dict[str, Any]] = None
                         ) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """(date, sector) → 板块涨停家数 / 一字板家数 / 一字+快速板家数。

    纪律两条：
    1) sector 缺失的票**不进任何板块**，也不归到"未知板块"——把缺行业的票堆成一个
       伪板块，会让它在"板块涨停家数"上凭空排到前面。
    2) 组内只要有一条首次封板时间不可解析，一字/快速板家数就是已知的低估值 →
       整组标 unavailable，而不是报一个偏小的数（0 家更是伪造）。
    """
    settings = dict(cfg if cfg is not None else v4_config())
    one_word_max = int(settings.get("one_word_seal_minute", 565))
    fast_max = int(settings.get("fast_board_seal_minute", 571))

    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for event in raw_events or []:
        sector = str(event.get("sector") or "").strip()
        if not sector:
            continue
        key = (_norm_date(event.get("date")), sector)
        bucket = groups.setdefault(key, {
            "sector_limitup_count": 0, "sector_one_word_count": 0,
            "sector_fast_board_count": 0, "unknown_seal_time": 0,
        })
        bucket["sector_limitup_count"] += 1
        minute = _seal_minutes(event.get("first_seal"))
        if minute is None:
            bucket["unknown_seal_time"] += 1
            continue
        if minute <= one_word_max:
            bucket["sector_one_word_count"] += 1
        if minute <= fast_max:
            bucket["sector_fast_board_count"] += 1

    for bucket in groups.values():
        unknown = bucket.pop("unknown_seal_time")
        bucket["seal_time_complete"] = unknown == 0
        bucket["unknown_seal_time_count"] = unknown
        if unknown:
            bucket["sector_one_word_count"] = None
            bucket["sector_fast_board_count"] = None
    return groups


# --------------------------------------------------------------------------- #
# v4-3：20 日换手基准 —— 从已抓的 K 线历史算，缺流通股本一律 unavailable
# --------------------------------------------------------------------------- #
def turnover_baseline(kline: List[Dict[str, Any]], date: str,
                      float_mktcap: Any, ref_close: Any,
                      cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """事件日之前 N 个交易日的换手率中位数（%）。

    换手率 = 成交股数 / 流通股本。日线只有 volume（单位「手」）和价格，**没有流通
    股本**，流通股本只能由 zt_pool 的流通市值 ÷ 当日收盘价反推；流通市值缺失时本
    函数返回 unavailable —— 绝不退化成"用成交量当换手率"（量纲都不同，仓内已有
    volume 漏乘每手股数把成交额低估 100 倍的先例）。
    """
    settings = dict(cfg if cfg is not None else v4_config())
    window = int(settings.get("turnover_baseline_window", 20))
    minimum = int(settings.get("turnover_baseline_min_days", 15))
    lot = float(xc.constraints_config().get("volume_lot_shares", 100.0))

    cap = _float_or_none(float_mktcap)
    close = _float_or_none(ref_close)
    if cap is None or cap <= 0 or close is None or close <= 0:
        return {"median": None, "sample_days": None,
                "availability": f"{UNAVAILABLE}:float_shares_unavailable"}
    float_shares = cap / close

    target = _norm_date(date)
    index = next((i for i, bar in enumerate(kline or [])
                  if _norm_date(bar.get("date")) == target), None)
    if index is None:
        return {"median": None, "sample_days": None,
                "availability": f"{UNAVAILABLE}:event_date_not_in_kline"}

    samples: List[float] = []
    for bar in (kline or [])[max(0, index - window):index]:
        volume = _float_or_none(bar.get("volume"))
        if volume is None or volume <= 0:
            continue
        samples.append(volume * lot / float_shares * 100.0)
    if len(samples) < minimum:
        return {"median": None, "sample_days": len(samples),
                "availability": (f"{UNAVAILABLE}:baseline_sample_insufficient"
                                 f"({len(samples)}<{minimum})")}
    samples.sort()
    middle = len(samples) // 2
    median = (samples[middle] if len(samples) % 2
              else (samples[middle - 1] + samples[middle]) / 2.0)
    return {"median": round(median, 6), "sample_days": len(samples),
            "availability": AVAILABLE}


def _v4_event_fields(event: Dict[str, Any], kline: List[Dict[str, Any]],
                     cross_section: Dict[Tuple[str, str], Dict[str, Any]],
                     t_close: float, cfg: Optional[Dict[str, Any]] = None,
                     minute_rows: Optional[List[Dict[str, Any]]] = None
                     ) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """单事件的 v4 字段 + 逐字段可得性。缺一律 None + unavailable，不造代理值。"""
    fields: Dict[str, Any] = {}
    availability: Dict[str, str] = {}

    for name, reason in (("turnover_pct", "not_provided_by_source"),
                         ("last_seal_time", "not_provided_by_source"),
                         ("open_board_count", "not_provided_by_source")):
        value = event.get(name)
        fields[name] = value
        availability[name] = AVAILABLE if value is not None else f"{UNAVAILABLE}:{reason}"

    fields["reseal_time"], availability["reseal_time"] = derive_reseal_time(event)

    sector = str(event.get("sector") or "").strip()
    bucket = cross_section.get((_norm_date(event.get("date")), sector)) if sector else None
    for name in ("sector_limitup_count", "sector_one_word_count", "sector_fast_board_count"):
        if bucket is None:
            fields[name] = None
            availability[name] = f"{UNAVAILABLE}:sector_missing"
        elif bucket.get(name) is None:
            fields[name] = None
            availability[name] = (f"{UNAVAILABLE}:sector_first_seal_incomplete"
                                  f"({bucket['unknown_seal_time_count']})")
        else:
            fields[name] = bucket[name]
            availability[name] = AVAILABLE

    baseline = turnover_baseline(kline, event.get("date"),
                                 event.get("float_mktcap"), t_close, cfg=cfg)
    fields["turnover_baseline_median"] = baseline["median"]
    fields["turnover_baseline_sample_days"] = baseline["sample_days"]
    availability["turnover_baseline_median"] = baseline["availability"]
    availability["turnover_baseline_sample_days"] = baseline["availability"]

    minute_fields, minute_availability = _minute_event_fields(
        event, kline, t_close, fields["reseal_time"], availability["reseal_time"],
        minute_rows, cfg=cfg)
    fields.update(minute_fields)
    availability.update(minute_availability)
    return fields, availability


def _minute_event_fields(event: Dict[str, Any], kline: List[Dict[str, Any]],
                         t_close: float, reseal_time: Optional[str],
                         reseal_availability: str,
                         minute_rows: Optional[List[Dict[str, Any]]],
                         cfg: Optional[Dict[str, Any]] = None
                         ) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """S1/S2 的两个分钟级字段。没有分钟行 → 保持 v4 原来的 unavailable 原因不变。

    ``pre_reseal_turnover_pct`` 的可得性**不高于** reseal_time：一次没炸过板就没有
    「封板前」这个时刻，此时它是 not_applicable 而不是 unavailable，语义跟着
    derive_reseal_time 走，不在这里另起一套判定。
    """
    settings = dict(cfg if cfg is not None else v4_config())
    checkpoint = str(settings.get("volume_ratio_checkpoint", "09:45"))
    window_days = int(settings.get("volume_ratio_baseline_days", 5))
    fields: Dict[str, Any] = {name: None for name in MINUTE_BAR_FIELDS}
    availability = {name: f"{UNAVAILABLE}:{reason}"
                    for name, reason in MINUTE_BAR_FIELDS.items()}
    fields["volume_ratio_source"] = None
    if not minute_rows:
        return fields, availability

    baseline = md.baseline_per_minute_from_daily(
        kline, _norm_date(event.get("date")), window_days=window_days)
    ratio = md.volume_ratio_at(minute_rows, checkpoint=checkpoint,
                               baseline_per_minute=baseline["value"])
    fields["volume_ratio"] = ratio["value"]
    availability["volume_ratio"] = (
        ratio["availability"] if baseline["value"] is not None
        else baseline["availability"])
    fields["volume_ratio_source"] = (
        f"{str(event.get('minute_source') or 'minute_derived')}:{checkpoint}"
        if ratio["value"] is not None else None)

    if not str(reseal_availability).startswith(AVAILABLE):
        availability["pre_reseal_turnover_pct"] = reseal_availability
        return fields, availability
    float_shares = md.float_shares_from_mktcap(event.get("float_mktcap"), t_close)
    turnover = md.cumulative_turnover_before(minute_rows, reseal_time, float_shares)
    fields["pre_reseal_turnover_pct"] = turnover["value"]
    availability["pre_reseal_turnover_pct"] = turnover["availability"]
    return fields, availability


def availability_summary(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """事件表级别的可得性矩阵：字段 → {状态前缀: 事件数}。零命中时用来定位缺哪类数据。"""
    summary: Dict[str, Dict[str, int]] = {field: {} for field in V4_FIELDS}
    for event in events or []:
        matrix = event.get("field_availability") or {}
        for field in V4_FIELDS:
            state = str(matrix.get(field, f"{UNAVAILABLE}:field_absent")).split("(")[0]
            summary[field][state] = summary[field].get(state, 0) + 1
    return summary


def kline_lookup(kline: List[Dict[str, Any]], date: str
                 ) -> Optional[Tuple[float, float, float]]:
    """
    在一只票的日线序列里定位 date，返回 (t_close, t1_open, t1_close)。
    date 不在序列、或没有次日（最后一根）→ None（事件丢弃）。
    """
    target = _norm_date(date)
    for i, bar in enumerate(kline):
        if _norm_date(bar.get("date")) == target:
            if i + 1 >= len(kline):
                return None
            nxt = kline[i + 1]
            return float(bar["close"]), float(nxt["open"]), float(nxt["close"])
    return None


def kline_pair_lookup(
    kline: List[Dict[str, Any]], date: str
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Return complete signal-day and T+1 bars for execution checks."""
    target = _norm_date(date)
    for index, bar in enumerate(kline):
        if _norm_date(bar.get("date")) == target:
            if index + 1 >= len(kline):
                return None
            return dict(bar), dict(kline[index + 1])
    return None


def first_sellable_exit(
    kline: List[Dict[str, Any]],
    entry_index: int,
    code: str,
    name: str,
    minimum_holding_sessions: int = 1,
) -> Optional[Tuple[Dict[str, Any], int]]:
    """Find the first close that can be sold after the A-share T+1 boundary.

    P5(a) 3：跌停日不是只有「一字跌停」才卖不掉——跌停价上没有承接量同样成交不了，
    一律顺延到次一可成交时点（与 T+1 叠加）。判定走 execution_constraints，
    涨跌停价按事件日期取制度（P5(b)），停牌/缺量同样顺延。
    """
    start = entry_index + minimum_holding_sessions
    is_st = "ST" in str(name or "").upper()
    for index in range(start, len(kline)):
        bar = kline[index]
        if float(bar.get("volume", 0) or 0) <= 0:
            continue
        previous_close = float(kline[index - 1].get("close", 0) or 0)
        if previous_close <= 0:
            continue
        verdict = xc.assess_sell_fill(
            dict(bar),
            code=str(code).zfill(6),
            asof=_norm_date(bar.get("date")),
            prev_close=previous_close,
            is_st=is_st,
        )
        if not verdict["filled"]:
            continue
        return dict(bar), index - entry_index
    return None


def assemble_events(raw_events: List[Dict[str, Any]],
                    kline_by_code: Dict[str, List[Dict[str, Any]]],
                    cfg: Optional[Dict[str, Any]] = None,
                    minute_rows_by_key: Optional[
                        Dict[Tuple[str, str], List[Dict[str, Any]]]] = None
                    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    把原始涨停事件 + 各代码 K 线 join 成回测事件表。
    返回 (事件表, 丢弃统计{no_kline, no_next_day})。

    板块横截面聚合用的是**入参的全量涨停池**（丢弃前），因为"当日该板块几家涨停"
    是市场事实，不该被"这只票我们恰好没抓到 K 线"改写。
    """
    settings = dict(cfg if cfg is not None else v4_config())
    cross_section = sector_cross_section(raw_events, cfg=settings)
    out: List[Dict[str, Any]] = []
    dropped = {"no_kline": 0, "no_next_day": 0}
    for ev in raw_events:
        code = str(ev.get("code", "")).zfill(6)
        kline = kline_by_code.get(code)
        if not kline:
            dropped["no_kline"] += 1
            continue
        minute_rows = (minute_rows_by_key or {}).get(
            (_norm_date(ev.get("date")), code))
        row, reason = _join_one_event(ev, code, kline, cross_section, settings,
                                      minute_rows)
        if row is None:
            dropped.setdefault(str(reason), 0)
            dropped[str(reason)] += 1
            continue
        out.append(row)
    return out, dropped


def _join_one_event(ev: Dict[str, Any], code: str, kline: List[Dict[str, Any]],
                    cross_section: Dict[Tuple[str, str], Dict[str, Any]],
                    settings: Dict[str, Any],
                    minute_rows: Optional[List[Dict[str, Any]]] = None
                    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """单个涨停事件 + 该票 K 线 → 事件表一行；不可用时返回 (None, 丢弃原因)。"""
    pair = kline_pair_lookup(kline, ev.get("date"))
    if pair is None:
        return None, "no_next_day"
    current, nxt = pair
    entry_index = next(
        index for index, bar in enumerate(kline)
        if _norm_date(bar.get("date")) == _norm_date(nxt.get("date"))
    )
    sellable = first_sellable_exit(kline, entry_index, code, str(ev.get("name") or ""))
    if sellable is None:
        return None, "no_sellable_exit"
    exit_bar, holding_sessions = sellable
    t_close = float(current["close"])
    t1_open = float(nxt["open"])
    t1_close = float(nxt["close"])
    # T 日的前一根（昨收）——算 T 日涨跌停价必需；缺则留 None 由引擎 fail-closed。
    prev_index = entry_index - 2
    t_prev_close = (
        float(kline[prev_index].get("close"))
        if prev_index >= 0 and kline[prev_index].get("close")
        else None
    )
    v4_fields, v4_availability = _v4_event_fields(
        ev, kline, cross_section, t_close, cfg=settings, minute_rows=minute_rows)
    return {
        "code": code,
        "name": ev.get("name", code),
        "date": _norm_date(ev.get("date")),
        "t_close": t_close,
        "t1_open": t1_open,
        "t1_close": t1_close,
        "entry_date": _norm_date(nxt.get("date")),
        # v3：T 日成交约束字段（一字禁买 / 回封参与率）
        "t_prev_close": t_prev_close,
        "t_open": _float_or_none(current.get("open")),
        "t_high": _float_or_none(current.get("high")),
        "t_low": _float_or_none(current.get("low")),
        "t_volume": _float_or_none(current.get("volume")),
        "t_amount": _bar_amount(current),
        "t1_high": float(nxt.get("high", max(t1_open, t1_close))),
        "t1_low": float(nxt.get("low", min(t1_open, t1_close))),
        "t1_volume": float(nxt.get("volume", 0) or 0),
        "t1_amount": _bar_amount(nxt),
        "exit_date": _norm_date(exit_bar.get("date")),
        "exit_close": float(exit_bar["close"]),
        "holding_sessions": holding_sessions,
        "first_seal": ev.get("first_seal"),
        "lianban": ev.get("lianban"),
        "seal_amount": ev.get("seal_amount"),
        "float_mktcap": ev.get("float_mktcap"),
        "sector": ev.get("sector"),
        "is_st": bool(ev.get("is_st", False)),
        # v4：S1/S2 证据字段 + 逐字段可得性（unavailable 一律带原因）
        **v4_fields,
        "field_availability": v4_availability,
    }, None


def _map_zt_row(row: Dict[str, Any], date: str) -> Dict[str, Any]:
    """stock_zt_pool_em 单行 → 标准化原始事件（仅元数据，价格留给 K 线）。"""
    name = str(row.get("名称", ""))
    return {
        "code": str(row.get("代码", "")).zfill(6),
        "name": name,
        "date": _norm_date(date),
        "first_seal": row.get("首次封板时间"),
        "lianban": row.get("连板数"),
        "seal_amount": row.get("封板资金"),
        "float_mktcap": row.get("流通市值"),
        "sector": row.get("所属行业"),
        "is_st": "ST" in name.upper(),
        # v4：这三个上游一直在返回，v3 之前直接丢掉了 —— S2 要的回封时刻就藏在这里。
        "turnover_pct": _float_or_none(row.get("换手率")),
        "last_seal_time": row.get("最后封板时间"),
        "open_board_count": _float_or_none(row.get("炸板次数")),
    }


def fetch_limitup_events(start: str, end: str, sleep: float = 0.3,
                         source: str = "akshare") -> List[Dict[str, Any]]:
    """
    历史涨停事件。start/end 形如 '20260301'。
    source="akshare"（默认）：逐日 stock_zt_pool_em，元数据全（含 first_seal/封单），
        但免费历史仅最近约 3-4 周——深历史会退化（assess_coverage 告警）。
    source="mootdx"：通达信 TCP 全市场日线重建，历史 6 年+，但仅 code/date/lianban，
        first_seal/seal_amount/sector 为 None——只够 H1 gap 假设，H2 真竞价封不可用。
    """
    if source == "mootdx":
        sys.path.insert(0, os.path.dirname(__file__))
        from mootdx_source import reconstruct_limitup_events  # noqa: E402

        return reconstruct_limitup_events(_norm_date(start), _norm_date(end))

    import akshare as ak
    import pandas as pd

    raw: List[Dict[str, Any]] = []
    for day in pd.date_range(_norm_date(start), _norm_date(end), freq="D"):
        ymd = day.strftime("%Y%m%d")
        try:
            df = ak.stock_zt_pool_em(date=ymd)
        except Exception:
            continue
        if df is None or len(df) == 0:
            continue
        for _, row in df.iterrows():
            raw.append(_map_zt_row(row.to_dict(), ymd))
        time.sleep(sleep)
    return raw


def fetch_klines(codes: List[str], days: int = 180, sleep: float = 0.2
                 ) -> Dict[str, List[Dict[str, Any]]]:
    """批量拉各代码腾讯 qfq 日线。失败的代码跳过（事件随后按 no_kline 丢弃）。"""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for code in sorted(set(str(c).zfill(6) for c in codes)):
        try:
            kl = fetch_tencent_kline(code, market=market_prefix(code), days=days)
        except DataSourceError:
            kl = []
        if kl:
            out[code] = kl
        time.sleep(sleep)
    return out


def control_pools_from_klines(kline_by_code: Dict[str, List[Dict[str, Any]]],
                              n_random: int = 300, breakout_window: int = 20,
                              seed: int = 42) -> Dict[str, List[float]]:
    """
    用已抓的 universe 日线算两个对照组的次日净收益池（不额外触网）：
    - simple_breakout：close 突破前 N 日新高 → 买次日开、卖次日收。
    - random_entry：随机 (代码, 交易日) → 买次日开、卖次日收。
    注意：池子取自涨停票历史，带轻微热门偏差，仅作 MVP 弱基准，正式跑应换全市场样本。
    """
    sys.path.insert(0, os.path.dirname(__file__))
    from daban_bt_engine import net_return  # noqa: E402
    import random as _random

    breakout: List[float] = []
    pairs: List[Tuple[str, int]] = []
    for code, kline in kline_by_code.items():
        closes = [float(b["close"]) for b in kline]
        for i in range(len(kline) - 1):
            pairs.append((code, i))
            if i >= breakout_window and closes[i] > max(closes[i - breakout_window:i]):
                try:
                    breakout.append(net_return(kline[i + 1]["open"], kline[i + 1]["close"]))
                except (ValueError, KeyError, TypeError):
                    continue

    rng = _random.Random(seed)
    rand: List[float] = []
    for code, i in rng.sample(pairs, min(n_random, len(pairs))) if pairs else []:
        nxt = kline_by_code[code][i + 1]
        try:
            rand.append(net_return(nxt["open"], nxt["close"]))
        except (ValueError, KeyError, TypeError):
            continue
    return {"simple_breakout": breakout, "random_entry": rand}


def _auto_days(start: str, buffer: int = 30) -> int:
    """腾讯 K 线按「最近 N 根」返回，N 必须覆盖从 start 回溯到今天的交易日数。"""
    from datetime import date

    y, m, d = (int(x) for x in _norm_date(start).split("-"))
    span = (date.today() - date(y, m, d)).days
    return int(span * 5 / 7) + buffer   # 日历天→交易日近似 + buffer


def _expected_trading_days(start: str, end: str) -> int:
    """请求区间内的近似交易日数（日历天 × 5/7），用于核对实际覆盖度。"""
    from datetime import date

    ys, ms, ds = (int(x) for x in _norm_date(start).split("-"))
    ye, me, de = (int(x) for x in _norm_date(end).split("-"))
    span = (date(ye, me, de) - date(ys, ms, ds)).days
    return max(1, int(span * 5 / 7))


def assess_coverage(raw: List[Dict[str, Any]], start: str, end: str) -> Dict[str, Any]:
    """核对涨停事件实际覆盖的交易日 vs 请求区间，缺失过半即高声告警。"""
    dates = sorted({_norm_date(e.get("date")) for e in raw})
    expected = _expected_trading_days(start, end)
    covered = len(dates)
    ratio = covered / expected if expected else 0.0
    warning = None
    if ratio < 0.8:
        warning = (f"⚠️ 数据覆盖严重不足：请求约 {expected} 个交易日，实际只拿到 {covered} 天"
                   f"（{_norm_date(start)}~{_norm_date(end)} 实际覆盖 "
                   f"{dates[0] if dates else 'N/A'}~{dates[-1] if dates else 'N/A'}）。"
                   f"stock_zt_pool_em 免费历史仅最近约 3-4 周，更早历史拿不到——样本已退化，"
                   f"结论不可用。勿把此样本当 2 年回测。")
    return {
        "requested_start": _norm_date(start), "requested_end": _norm_date(end),
        "expected_trading_days": expected, "covered_trading_days": covered,
        "coverage_ratio": round(ratio, 3),
        "covered_first": dates[0] if dates else None,
        "covered_last": dates[-1] if dates else None,
        "warning": warning,
    }


def build_event_table(start: str, end: str, use_cache: bool = True,
                      source: str = "akshare",
                      minute_source: str = "auto") -> Dict[str, Any]:
    """
    端到端：涨停事件 + 次日 K 线 → 事件表，带本地缓存。覆盖度不足会在结果里高声标注。
    source="akshare"（默认）：元数据全但仅最近 3-4 周；K 线走腾讯 ifzq。
    source="mootdx"：通达信深历史重建，事件与 K 线同源同深度（6 年+），仅 H1 gap 假设可用。
    """
    # 缓存键必须带 minute_source：同一区间用 none 建过的表里两个分钟字段全是
    # unavailable，若被 minute=auto 的这次静默复用，就成了「填了字段但看不到」。
    cache = data_file(
        "chanlun-backtest",
        f"event_table_{source}_m-{minute_source}_{start}_{end}.json")
    if use_cache:
        cached = read_json(cache, default=None)
        if isinstance(cached, dict) and cached.get("schema") == EVENT_SCHEMA:
            return cached

    raw = fetch_limitup_events(start, end, source=source)
    coverage = assess_coverage(raw, start, end)
    if coverage["warning"]:
        print(coverage["warning"], file=sys.stderr)
    codes = [e["code"] for e in raw]
    if source == "mootdx":
        sys.path.insert(0, os.path.dirname(__file__))
        from mootdx_source import fetch_klines as fetch_klines_mootdx  # noqa: E402

        klines = fetch_klines_mootdx(codes, _norm_date(start))
    else:
        klines = fetch_klines(codes, days=_auto_days(start))
    minute_rows, minute_diagnostics = mrs.collect(raw, mode=minute_source)
    events, dropped = assemble_events(raw, klines, minute_rows_by_key=minute_rows)
    result = {
        "schema": EVENT_SCHEMA,
        "source": source,
        "minute_source": minute_source,
        "minute_coverage": minute_diagnostics,
        "start": start, "end": end,
        "raw_count": len(raw), "event_count": len(events), "dropped": dropped,
        "coverage": coverage,
        "events": events,
        "field_availability_summary": availability_summary(events),
        "control_pools": control_pools_from_klines(klines),
    }
    atomic_write_json(cache, result)
    return result
