#!/usr/bin/env python3
"""状态分阶段收益归因 State PnL（升级方案 P1）— 纯计算 + CLI，不触网。

系统里并存三套情绪口径：``market_temperature`` 的五档、``classify_market_state``
的 S0-S6、P0 落地的连续分 S_t。**谁更能区分次日赚钱效应，从来没有用历史数据检验
过。** P3 的策略要拿情绪状态当过滤条件，若这层没有统计价值，那个过滤就是装饰。
本脚本只产出证据：读 ``sentiment_daily`` 序列，按三套口径分别给每个交易日打标签，
统计各状态下**次日**的梯队溢价 / 涨停红盘率 / 炸板率变化，输出 E[R|state] 矩阵。
**不改任何阈值**——校准动作走后续独立 PR。

四条硬性质（每条都对应一类曾经发生过的假绿）：

1. **禁未来函数。** 第 t 日的标签只由 ``records[:t+1]`` 计算：``label_series`` 逐日
   把切片喂给打标签函数，t+1 之后的行在物理上进不了标签。被解释变量取自
   ``records[t+1]``，两者严格分离。
2. **空集不产出数字。** 任何一格样本为空返回 ``status="unavailable"`` 且
   ``mean=None``，绝不是 0.0 / 1.0——"这个状态没赚钱"和"这个状态没样本"必须可区分。
3. **样本门槛 30。** ``0 < n < 30`` 的格子标 ``UNVERIFIED`` 并**扣住均值不输出**
   （方案 §4.2）：留着数字，早晚有人拿去当结论。
4. **只有 ``coverage_status == "full"`` 的日子进结论集。** ``partial`` 日单独统计并
   打 ``conclusive=False``：半个市场的涨停家数不是全市场口径。

制度分段直接复用 ``a_share_rules`` 的断点常量（创业板 2020-08-24 等），不另抄一份；
跨制度的相邻两日不配对，也不跨制度合并统计。
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from statistics import mean as _mean
from statistics import median as _median
from typing import Any, Mapping, Sequence

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
try:
    from ._repo_bootstrap import ensure_repo_importable  # type: ignore[import-not-found]
except ImportError:
    from _repo_bootstrap import ensure_repo_importable  # type: ignore[no-redef]

ensure_repo_importable(ROOT)
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

import market_temperature  # noqa: E402
import sentiment_daily  # noqa: E402
import sentiment_score  # noqa: E402
from a_share_rules import (  # noqa: E402
    BSE_OPEN,
    CHINEXT_20PCT_FROM,
    SSE_RISK_WARNING_10PCT_FROM,
    STAR_MARKET_OPEN,
)

SCHEMA = "state_pnl_report_v1"

#: 方案 §4.2：任何一格样本数低于此值一律 UNVERIFIED，不给方向性结论。
MIN_SAMPLES = 30

#: 三套口径。顺序即报告顺序。
SCHEMES = ("five_tier", "market_state", "sentiment_band")

#: 被解释变量：全部取自 t+1 日的记录。
OUTCOMES = (
    "next_limit_premium_open",
    "next_limit_premium_close",
    "next_limit_red_ratio",
    "next_break_rate_change",
)

#: 制度断点。**常量来自 a_share_rules，本文件不重抄日期**——两份日期迟早分叉。
REGIME_BREAKPOINTS: tuple[tuple[date, str], ...] = (
    (STAR_MARKET_OPEN, "star_open"),
    (CHINEXT_20PCT_FROM, "chinext_20pct"),
    (BSE_OPEN, "bse_open"),
    (SSE_RISK_WARNING_10PCT_FROM, "sse_risk_warning_10pct"),
)
REGIME_BASE = "pre_star"


# ========== 制度分段 ==========

def regime_of(trading_date: Any) -> str | None:
    """交易日 → 制度分段标识。日期不可解析返回 None（该日不进任何统计）。"""
    try:
        day = date.fromisoformat(str(trading_date))
    except (TypeError, ValueError):
        return None
    label = REGIME_BASE
    for breakpoint_day, name in REGIME_BREAKPOINTS:
        if day >= breakpoint_day:
            label = name
    return label


# ========== 打标签（三套口径，逐日 point-in-time） ==========

def _numeric(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def label_five_tier(
    history: Sequence[Mapping[str, Any]], previous_tier: str | None
) -> dict[str, Any]:
    """五档口径。``history`` 末行即当日，函数只读末行与传入的上一档。

    ``sentiment_daily`` 不含连板晋级率，记录未显式携带 ``promotion_rate`` 时按
    ``None`` 传给 ``classify_tier``——走它自己的"晋级率缺失、按高度板保守判定"
    分支，而不是就地编一个代理指标。口径退化已在校准报告里写明。
    """
    row = dict(history[-1])
    height = _numeric(row.get("max_board"))
    if height is None:
        return {"label": None, "reason": "max_board_unavailable"}
    result = market_temperature.classify_tier(
        int(height), _numeric(row.get("promotion_rate")), previous_tier
    )
    return {"label": result["tier"], "raw_label": result["raw_tier"],
            "reason": None, "promotion_rate_available": row.get("promotion_rate") is not None}


def label_market_state(
    history: Sequence[Mapping[str, Any]], tier: str | None, previous_state: str | None
) -> dict[str, Any]:
    """S0-S6 口径。喂五档结果 + 当日涨跌停家数广度证据，其余证据本数据集没有。"""
    if not tier:
        return {"label": None, "reason": "tier_unavailable"}
    row = dict(history[-1])
    result = market_temperature.classify_market_state(
        {"tier": tier, "context_status": "ok"},
        breadth={"limitup_count": row.get("limit_count"),
                 "limitdown_count": row.get("limit_down_count")},
        previous_state=previous_state,
    )
    if not result.get("available"):
        return {"label": None, "reason": "state_machine_unavailable"}
    return {"label": result["dominant_state"],
            "raw_label": result.get("raw_dominant_state"), "reason": None}


def label_sentiment(
    history: Sequence[Mapping[str, Any]], config: Mapping[str, Any] | None
) -> dict[str, Any]:
    """S_t 口径：连续分 + 分档。配置缺失或预热不足 → 不可用，不给 50 分。"""
    if not config:
        return {"label": None, "score": None, "reason": "config_missing"}
    result = sentiment_score.score_at(history, len(history) - 1, config)
    if result.get("status") != "ok":
        return {"label": None, "score": None, "reason": result.get("reason")}
    return {"label": result.get("band"), "score": result.get("score"), "reason": None}


def label_series(
    records: Sequence[Mapping[str, Any]], *, config: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """逐日三套标签。

    **禁未来函数的实现点在这里**：第 index 日只拿到 ``records[:index + 1]`` 这个
    切片，t+1 之后的行不在参数里，误用无从发生。
    """
    labels: list[dict[str, Any]] = []
    previous_tier: str | None = None
    previous_state: str | None = None
    for index in range(len(records)):
        history = records[:index + 1]
        tier = label_five_tier(history, previous_tier)
        state = label_market_state(history, tier.get("label"), previous_state)
        sentiment = label_sentiment(history, config)
        previous_tier = tier.get("label") or previous_tier
        previous_state = state.get("label") or previous_state
        labels.append({
            "trading_date": history[-1].get("trading_date"),
            "five_tier": tier.get("label"),
            "market_state": state.get("label"),
            "sentiment_band": sentiment.get("label"),
            "sentiment_score": sentiment.get("score"),
            "reasons": {"five_tier": tier.get("reason"), "market_state": state.get("reason"),
                        "sentiment_band": sentiment.get("reason")},
        })
    return labels


# ========== 次日结果（被解释变量） ==========

def next_day_outcomes(
    today: Mapping[str, Any], tomorrow: Mapping[str, Any]
) -> dict[str, float | None]:
    """t+1 日的梯队溢价 / 涨停红盘率 / 炸板率变化。缺项为 None，不补 0。"""
    today_break = _numeric(today.get("break_rate"))
    next_break = _numeric(tomorrow.get("break_rate"))
    return {
        "next_limit_premium_open": _numeric(tomorrow.get("limit_premium_open")),
        "next_limit_premium_close": _numeric(tomorrow.get("limit_premium_close")),
        "next_limit_red_ratio": _numeric(tomorrow.get("limit_red_ratio")),
        "next_break_rate_change": (
            round(next_break - today_break, 6)
            if today_break is not None and next_break is not None else None
        ),
    }


def build_observations(
    records: Sequence[Mapping[str, Any]], *, config: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """标签(t) × 结果(t+1) 配对。跨制度的相邻两日不配对。"""
    rows = list(records)
    labels = label_series(rows, config=config)
    observations: list[dict[str, Any]] = []
    for index in range(len(rows) - 1):
        today, tomorrow = rows[index], rows[index + 1]
        regime = regime_of(today.get("trading_date"))
        if regime is None or regime != regime_of(tomorrow.get("trading_date")):
            continue
        observations.append({
            "trading_date": today.get("trading_date"),
            "next_trading_date": tomorrow.get("trading_date"),
            "regime": regime,
            "coverage_status": str(today.get("coverage_status") or "unknown"),
            "next_coverage_status": str(tomorrow.get("coverage_status") or "unknown"),
            "labels": {scheme: labels[index].get(scheme) for scheme in SCHEMES},
            "sentiment_score": labels[index].get("sentiment_score"),
            "outcomes": next_day_outcomes(today, tomorrow),
        })
    return observations


def is_full_coverage(observation: Mapping[str, Any]) -> bool:
    """标签日与结果日**都**是 full 覆盖才算结论集。任一端 partial 即出局。"""
    return (observation.get("coverage_status") == "full"
            and observation.get("next_coverage_status") == "full")


def filter_full_coverage(observations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """结论集。"""
    return [dict(row) for row in observations if is_full_coverage(row)]


# ========== 聚合：E[R|state] ==========

def summarize_cell(
    values: Sequence[float], *, min_samples: int = MIN_SAMPLES
) -> dict[str, Any]:
    """一格统计。空 → unavailable；不足门槛 → UNVERIFIED 且**不输出均值**。"""
    sample = [float(value) for value in values]
    if not sample:
        return {"n": 0, "status": "unavailable", "mean": None, "median": None}
    if len(sample) < int(min_samples):
        return {"n": len(sample), "status": "UNVERIFIED", "mean": None, "median": None,
                "withheld_reason": f"n<{int(min_samples)}"}
    return {"n": len(sample), "status": "ok",
            "mean": round(_mean(sample), 6), "median": round(_median(sample), 6)}


def state_matrix(
    observations: Sequence[Mapping[str, Any]], scheme: str, outcome: str,
    *, min_samples: int = MIN_SAMPLES,
) -> dict[str, dict[str, Any]]:
    """{制度: {状态: 格}}。制度分段互不合并。"""
    buckets: dict[str, dict[str, list[float]]] = {}
    for row in observations:
        label = (row.get("labels") or {}).get(scheme)
        value = (row.get("outcomes") or {}).get(outcome)
        if label is None or value is None:
            continue
        buckets.setdefault(str(row.get("regime")), {}).setdefault(str(label), []).append(float(value))
    return {
        regime: {label: summarize_cell(values, min_samples=min_samples)
                 for label, values in sorted(states.items())}
        for regime, states in sorted(buckets.items())
    }


# ========== 消融：区分度对比 ==========

def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        average = (position + end) / 2.0 + 1.0
        for index in order[position:end + 1]:
            ranks[index] = average
        position = end + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Spearman 秩相关。样本 < 3 或任一侧无变异 → None（不是 0.0）。"""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = _ranks(list(xs)), _ranks(list(ys))
    mx, my = _mean(rx), _mean(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return None
    return round(cov / (vx * vy) ** 0.5, 6)


def _scheme_order(scheme: str, config: Mapping[str, Any] | None) -> tuple[str, ...]:
    if scheme == "five_tier":
        return tuple(market_temperature.TIER_ORDER)
    if scheme == "market_state":
        return tuple(market_temperature.MARKET_STATES)
    bands = list((config or {}).get("bands") or [])
    return tuple(str(item.get("name")) for item in sorted(
        (item for item in bands if isinstance(item, Mapping)),
        key=lambda item: float(item.get("min", 0.0)),
    ))


def discrimination(
    observations: Sequence[Mapping[str, Any]], outcome: str,
    *, config: Mapping[str, Any] | None, min_samples: int = MIN_SAMPLES,
) -> dict[str, Any]:
    """三套口径对同一被解释变量的 Spearman IC 与分组单调性（按制度分段）。"""
    report: dict[str, Any] = {}
    regimes = sorted({str(row.get("regime")) for row in observations})
    for regime in regimes:
        rows = [row for row in observations if str(row.get("regime")) == regime]
        entry: dict[str, Any] = {
            "sentiment_score_continuous": _ic_continuous(rows, outcome, min_samples),
        }
        for scheme in SCHEMES:
            entry[scheme] = _ic_ordinal(rows, scheme, outcome,
                                        _scheme_order(scheme, config), min_samples)
        report[regime] = entry
    return report


def _ic_continuous(
    rows: Sequence[Mapping[str, Any]], outcome: str, min_samples: int
) -> dict[str, Any]:
    pairs = [
        (float(row["sentiment_score"]), float((row.get("outcomes") or {})[outcome]))
        for row in rows
        if row.get("sentiment_score") is not None
        and (row.get("outcomes") or {}).get(outcome) is not None
    ]
    if not pairs:
        return {"n": 0, "status": "unavailable", "ic": None}
    if len(pairs) < int(min_samples):
        return {"n": len(pairs), "status": "UNVERIFIED", "ic": None}
    return {"n": len(pairs), "status": "ok",
            "ic": spearman([p[0] for p in pairs], [p[1] for p in pairs])}


def _ic_ordinal(
    rows: Sequence[Mapping[str, Any]], scheme: str, outcome: str,
    order: Sequence[str], min_samples: int,
) -> dict[str, Any]:
    index_of = {name: position for position, name in enumerate(order)}
    pairs = [
        (float(index_of[str((row.get("labels") or {}).get(scheme))]),
         float((row.get("outcomes") or {})[outcome]))
        for row in rows
        if str((row.get("labels") or {}).get(scheme)) in index_of
        and (row.get("outcomes") or {}).get(outcome) is not None
    ]
    if not pairs:
        return {"n": 0, "status": "unavailable", "ic": None, "monotonic": None}
    matrix = state_matrix(rows, scheme, outcome, min_samples=min_samples)
    cells = list(matrix.values())[0] if matrix else {}
    result = {"n": len(pairs),
              "status": "ok" if len(pairs) >= int(min_samples) else "UNVERIFIED",
              "ic": spearman([p[0] for p in pairs], [p[1] for p in pairs])
              if len(pairs) >= int(min_samples) else None,
              "monotonic": _monotonic(cells, order)}
    return result


def _monotonic(cells: Mapping[str, Mapping[str, Any]], order: Sequence[str]) -> Any:
    """分组单调性。任一分组不是 ``ok``（空或不足门槛）→ None，不给判定。"""
    means = []
    for name in order:
        cell = cells.get(name)
        if cell is None:
            continue
        if cell.get("status") != "ok" or cell.get("mean") is None:
            return None
        means.append(float(cell["mean"]))
    if len(means) < 2:
        return None
    return all(b >= a for a, b in zip(means, means[1:])) or \
        all(b <= a for a, b in zip(means, means[1:]))


# ========== 报告 ==========

def coverage_breakdown(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in records:
        key = str(row.get("coverage_status") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def build_report(
    records: Sequence[Mapping[str, Any]], *, min_samples: int = MIN_SAMPLES,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """完整报告：结论集（仅 full 覆盖）+ partial 子集（显式不可作结论）。"""
    rows = sorted(list(records), key=lambda item: str(item.get("trading_date")))
    cfg = sentiment_score.load_config() if config is None else dict(config)
    observations = build_observations(rows, config=cfg)
    conclusive = filter_full_coverage(observations)
    partial = [dict(row) for row in observations if not is_full_coverage(row)]
    dates = [str(row.get("trading_date")) for row in rows if row.get("trading_date")]
    return {
        "schema": SCHEMA,
        "calibrated": False,
        "min_samples": int(min_samples),
        "trading_day_count": len(rows),
        "first_trading_date": dates[0] if dates else None,
        "last_trading_date": dates[-1] if dates else None,
        "coverage_breakdown": coverage_breakdown(rows),
        "observation_count": len(observations),
        "conclusive": _section(conclusive, cfg, min_samples, conclusive=True),
        "partial_subset": _section(partial, cfg, min_samples, conclusive=False),
        "notes": _notes(cfg),
    }


def _section(
    observations: Sequence[Mapping[str, Any]], config: Mapping[str, Any] | None,
    min_samples: int, *, conclusive: bool,
) -> dict[str, Any]:
    state_pnl = {
        scheme: {outcome: state_matrix(observations, scheme, outcome,
                                       min_samples=min_samples)
                 for outcome in OUTCOMES}
        for scheme in SCHEMES
    }
    # ``conclusive`` 只说覆盖口径够不够格，不代表真有结论：full 覆盖但零样本时
    # 若还留 True，下游一句 ``if section["conclusive"]`` 就会把空矩阵当成已校准
    # 结果放行（仓内「空集恒真」那类假绿）。因此再与「至少一格达到样本门槛」求与。
    has_conclusion = any(
        cell.get("status") == "ok"
        for by_outcome in state_pnl.values()
        for matrix in by_outcome.values()
        for cell in matrix.values()
    )
    return {
        "conclusive": bool(conclusive) and has_conclusion,
        "conclusion_eligible_scope": bool(conclusive),
        "has_conclusion": has_conclusion,
        "coverage_scope": "full" if conclusive else "partial_or_unknown",
        "sample_count": len(observations),
        "regimes": sorted({str(row.get("regime")) for row in observations}),
        "state_pnl": state_pnl,
        "discrimination": {
            outcome: discrimination(observations, outcome, config=config,
                                    min_samples=min_samples)
            for outcome in OUTCOMES
        },
    }


def _notes(config: Mapping[str, Any] | None) -> list[str]:
    notes = [
        "标签只用截至 t 日的记录计算，被解释变量取自 t+1 日；两者严格分离。",
        "sentiment_daily 不含连板晋级率，五档口径走 classify_tier 的缺晋级率保守分支。",
        "partial 覆盖日单独统计且 conclusive=False：子集口径，不可当全市场。",
        "不改任何阈值：本脚本只出证据，校准走独立 PR。",
    ]
    if not config:
        notes.append("config/scoring.yaml 的 sentiment_score 节缺失，S_t 口径整体不可用。")
    return notes


def load_records(path: str | None) -> list[dict[str, Any]]:
    """读 ``sentiment_daily`` 汇总视图。未指定路径即用数据集默认位置。"""
    if not path:
        return sentiment_daily.load_summary()
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, Mapping) and value.get("trading_date"):
                rows.append(dict(value))
    return sorted(rows, key=lambda item: str(item.get("trading_date")))


def _print_summary(report: Mapping[str, Any]) -> None:
    print(f"交易日 {report['trading_day_count']} 天 "
          f"[{report['first_trading_date']} → {report['last_trading_date']}] "
          f"覆盖分布={report['coverage_breakdown']}")
    conclusive = report["conclusive"]
    partial = report["partial_subset"]
    print(f"结论集(full 覆盖) 配对样本={conclusive['sample_count']} "
          f"制度分段={conclusive['regimes'] or '无'}")
    print(f"partial 子集 配对样本={partial['sample_count']}（子集口径，不可作结论）")
    if conclusive["sample_count"] == 0:
        print("零可用样本：三套口径全部 UNVERIFIED，管道已就绪待生产数据。")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="State PnL 分阶段收益归因（P1，只出证据）")
    parser.add_argument("--summary-file", default=None,
                        help="sentiment_daily.jsonl 路径（缺省用数据集默认位置）")
    parser.add_argument("--min-samples", type=int, default=MIN_SAMPLES,
                        help=f"每格样本门槛，低于即 UNVERIFIED（默认 {MIN_SAMPLES}）")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    parser.add_argument("--out", default=None, help="把完整 JSON 写到该路径")
    args = parser.parse_args(argv)

    report = build_report(load_records(args.summary_file), min_samples=args.min_samples)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
