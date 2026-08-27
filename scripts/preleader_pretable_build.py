#!/usr/bin/env python3
"""构建 S4 先于龙头套利的 D-1 盘前表（NON-LIVE 研究层）。

本脚本是 ``preleader_arbitrage.build_pretable`` 的**唯一生产接线点**。策略的成败
点在模块 docstring 里写得很清楚：盘前表必须是真的 D-1 晚间产物，盘中不许现算。
因此这里只做三件事——取 D-1 龙头与同属性成分股、补齐建表所需的排除证据、把结果
落成不可变产物——判定逻辑一律留在 ``preleader_arbitrage`` 里，本脚本不复制阈值。

证据缺失的处理（缺证据 ≠ 干净）
------------------------------
建表要排除两类成分股：重大利空、流动性不足。这两项证据都可能取不到，而**取不到
和"确认没有"是两回事**：

* 20 日均成交额来自本地日线缓存（``market-history-cache`` 作业的产物）。缓存整体
  不可用时，若照常建表，每一只成分股都会被记成 ``insufficient_liquidity`` ——
  一张"所有人都不合格"的表和一张"没建成的表"在下游看起来完全一样，但前者会被当
  成有效盘前表使用。所以这里直接标 ``degraded`` 并写明缺口，不出表。
* 公告扫描按 code 逐只取。**单只取数失败的不进成分股池**，并记进
  ``announcement_scan_failed``；把取数失败当成"没有利空"会把未知风险洗成干净。

产物 ``status`` 只有 ``ok`` 的表才允许被 ``strategy_shadow_runner`` 消费。

红线：本产物只服务 NON-LIVE 研究层，不得进入实盘排序、评分或仓位。
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

import announcement_risk  # noqa: E402
import local_market_history  # noqa: E402
import preleader_arbitrage  # noqa: E402
import recommendation_quality  # noqa: E402
from paths import data_file  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402

SCHEMA = "preleader_pretable_artifact_v1"
TURNOVER_LOOKBACK_DAYS = 20
# 成分股池超过此规模就不建表：公告扫描是逐只网络取数，静默截断会让被截掉的那些票
# 以"没有利空"的身份留在表里——那正是本文件开头要防的洗白。
# 400 这个数来自实测而非拍脑袋：2026-08-27 本机 10 只耗时 47.9s（scan_many 固定
# 5 并发）＝ 4.79s/只，400 只约 1916s，落在 cron 作业 2400s 超时之内。改并发或换
# 公告源时，这个上限和 manifest 里的 timeout_seconds 要一起重算。
DEFAULT_MAX_SCAN_CODES = 400


def _today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _naked_code(value: Any) -> str:
    code = str(value or "").strip().lower()
    return code.removeprefix("sh").removeprefix("sz").zfill(6) if code else ""


def _sector(row: Mapping[str, Any]) -> str:
    return str(row.get("sector") or row.get("industry") or row.get("theme") or "").strip()


def _candidates(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        rows = payload.get("candidates") or payload.get("research_candidates") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    return [row for row in rows if isinstance(row, Mapping)]


def average_turnover(codes: Sequence[str], as_of: str) -> dict[str, float]:
    """按 code 取截至 ``as_of`` 的 20 日均成交额；覆盖不足的 code 直接不出现。

    只用 ``trading_date <= as_of`` 的 bar——盘前表不能含 D0 数据。窗口不满
    ``TURNOVER_LOOKBACK_DAYS`` 的不给均值：半个窗口算出来的均值和满窗口的不可比，
    用它去卡同一条阈值等于对不同股票用不同标准。
    """
    if not codes:
        return {}
    bars = local_market_history.get_daily_bars(
        list(codes), as_of, TURNOVER_LOOKBACK_DAYS
    )
    grouped: dict[str, list[float]] = {}
    for bar in bars:
        amount = bar.get("amount")
        if amount is None:
            continue
        try:
            value = float(amount)
        except (TypeError, ValueError):
            continue
        if value != value:  # NaN
            continue
        grouped.setdefault(_naked_code(bar.get("code")), []).append(value)
    return {
        code: sum(values) / len(values)
        for code, values in grouped.items()
        if len(values) >= TURNOVER_LOOKBACK_DAYS
    }


def scan_material_bad_news(
    codes: Sequence[str], as_of: str
) -> tuple[dict[str, bool], list[str]]:
    """逐只扫公告，返回 (code → 是否重大利空, 取数失败的 code)。

    "重大利空"取 ``recommendation_quality`` 已有的两类硬信号：证伪交易逻辑的澄清
    （``thesis_invalidation_hits``）与硬风险（``hard_risk_hits``）。本脚本不自造
    第二套关键词表——包括定期披露件（非经营性资金占用汇总表等）的豁免，那条护栏已
    收敛进 ``recommendation_quality.PERIODIC_DISCLOSURE_TITLE_RE``，本模块不再留
    第二份同类逻辑，避免两处漂移。
    """
    if not codes:
        return {}, []
    scanned = announcement_risk.scan_many(list(codes))
    flags: dict[str, bool] = {}
    failed: list[str] = []
    for code, announcements in scanned.items():
        key = _naked_code(code)
        if announcements is None:
            failed.append(key)
            continue
        risks = recommendation_quality.scan_announcement_risks(announcements, as_of)
        flags[key] = bool(
            risks.get("thesis_invalidation_hits") or risks.get("hard_risk_hits")
        )
    return flags, sorted(failed)


def _degraded(as_of: str, gaps: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "schema": SCHEMA, "as_of": as_of, "status": "degraded",
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "evidence_gaps": gaps, "pretable": None,
        "research_only": True, "execution_eligible": False, **extra,
    }


def build(
    input_path: str, *, as_of: str | None = None,
    max_scan_codes: int = DEFAULT_MAX_SCAN_CODES,
) -> dict[str, Any]:
    target = as_of or _today()
    with open(os.path.abspath(os.path.expanduser(input_path)), encoding="utf-8") as handle:
        payload = json.load(handle)
    source_asof = str((payload or {}).get("asof") or "")[:10] if isinstance(payload, Mapping) else ""
    if source_asof != target:
        raise ValueError(f"input asof mismatch: expected {target}, got {source_asof or 'missing'}")

    rows = _candidates(payload)
    leaders = [
        {"code": _naked_code(row.get("code")), "attribute": _sector(row), "date": target}
        for row in rows
        if row.get("leader_role") == "sector_leader" and _sector(row) and _naked_code(row.get("code"))
    ]
    leader_attributes = {leader["attribute"] for leader in leaders}
    leader_codes = {leader["code"] for leader in leaders}
    pool = [
        row for row in rows
        if _sector(row) in leader_attributes and _naked_code(row.get("code")) not in leader_codes
    ]
    pool_codes = sorted({_naked_code(row.get("code")) for row in pool if _naked_code(row.get("code"))})

    if len(pool_codes) > max_scan_codes:
        return _degraded(
            target, ["member_pool_exceeds_announcement_scan_budget"],
            member_pool_size=len(pool_codes), max_scan_codes=max_scan_codes,
        )

    turnover = average_turnover(pool_codes, target)
    if pool_codes and not turnover:
        # 一只都没覆盖到 ≠ 全市场都不流动，是缓存没建起来。照常出表会得到一张
        # "所有成分股都流动性不足"的空壳表，而下游无法与真表区分。
        return _degraded(
            target, ["avg_turnover_20d_source_unavailable"],
            member_pool_size=len(pool_codes),
            history_cache=local_market_history.cache_stats(),
        )

    liquid_codes = [code for code in pool_codes if code in turnover]
    bad_news, scan_failed = scan_material_bad_news(liquid_codes, target)
    failed_set = set(scan_failed)

    members = [
        {
            "code": _naked_code(row.get("code")),
            "name": row.get("name"),
            "attribute": _sector(row),
            "date": target,
            "is_st": bool(row.get("is_st")),
            "avg_turnover_20d": turnover.get(_naked_code(row.get("code"))),
            "material_bad_news": bad_news.get(_naked_code(row.get("code")), False),
        }
        for row in pool
        if _naked_code(row.get("code")) not in failed_set
    ]
    pretable = preleader_arbitrage.build_pretable(leaders, members, as_of=target)
    return {
        "schema": SCHEMA, "as_of": target, "status": "ok",
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "evidence_gaps": [],
        "input_path": os.path.abspath(os.path.expanduser(input_path)),
        "leader_count": len(leaders),
        "member_pool_size": len(pool_codes),
        "liquidity_covered": len(liquid_codes),
        "announcement_scan_failed": scan_failed,
        "pretable": pretable,
        "research_only": True, "execution_eligible": False,
    }


def output_path(as_of: str) -> str:
    return data_file("stock-triage", os.path.join("preleader_pretable", f"{as_of}.json"))


def run(input_path: str, *, as_of: str | None = None, **kwargs: Any) -> dict[str, Any]:
    result = build(input_path, as_of=as_of, **kwargs)
    path = output_path(result["as_of"])
    existing = read_json(path, None)
    if isinstance(existing, Mapping) and existing.get("status") == "ok" and result["status"] != "ok":
        # 已有可用盘前表时，一次退化的重跑不许把它盖掉。
        return dict(existing)
    atomic_write_json(path, result)
    return result


def previous_trading_asof(asof: str) -> str | None:
    """找**严格早于** ``asof`` 的最近一张盘前表日期；没有则 None。

    按产物目录里的实际日期回溯，而不是按日历减一天：节假日与停摆日都不会有表，
    减一天会得到一个不存在的日期，然后把"作业停摆"误报成"表缺失"。
    """
    directory = Path(output_path(asof)).parent
    if not directory.is_dir():
        return None
    dates = sorted(
        path.stem for path in directory.glob("*.json")
        if len(path.stem) == 10 and path.stem < str(asof)[:10]
    )
    return dates[-1] if dates else None


def load_pretable(as_of: str) -> tuple[Mapping[str, Any] | None, str]:
    """读 ``as_of`` 当日的盘前表；只有 ``status == "ok"`` 才交出表体。

    返回 ``(pretable, reason)``——取不到时 ``pretable`` 为 None 且 reason 说明原因，
    调用方据此报 unavailable，而不是拿一张退化的表当有效表用。
    """
    payload = read_json(output_path(as_of), None)
    if not isinstance(payload, Mapping):
        return None, "pretable_artifact_missing"
    if payload.get("status") != "ok":
        gaps = ",".join(payload.get("evidence_gaps") or []) or "unknown"
        return None, f"pretable_degraded:{gaps}"
    pretable = payload.get("pretable")
    if not isinstance(pretable, Mapping):
        return None, "pretable_body_missing"
    return pretable, "ok"


def main() -> None:
    parser = argparse.ArgumentParser(description="S4 先于龙头套利 D-1 盘前表构建器")
    parser.add_argument("--input", default=data_file("stock-triage", "candidate_pool_latest.json"))
    parser.add_argument("--asof", default=None)
    parser.add_argument("--max-scan-codes", type=int, default=DEFAULT_MAX_SCAN_CODES)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = run(args.input, as_of=args.asof, max_scan_codes=args.max_scan_codes)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        entries = len(((result.get("pretable") or {}).get("entries")) or [])
        print(f"preleader-pretable {result['as_of']}: {result['status']}, {entries} entries")


if __name__ == "__main__":
    main()
