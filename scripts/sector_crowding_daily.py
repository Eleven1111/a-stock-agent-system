#!/usr/bin/env python3
"""
板块日度因子产物 —— RESEARCH ONLY
==================================
用本地日线缓存（``local_market_history``，qfq）+ 当前行业归属（``industry_map``）
重建过去 N 个交易日的板块聚合，单趟读盘产出两份：

- ``sector_crowding_latest.json``       拥挤度分位与状态机档位
- ``sector_price_factors_latest.json``  RS / 超额动量 / RS 斜率 / 广度
- ``sector_fake_breakout_latest.json``  假突破风险（六个可得子项）

两份分开落盘：产物名字必须说清里面是什么，把价格因子塞进拥挤度那份会让下游读到
名不副实的内容。

**零取数**：只读本地 SQLite 与本地缓存，不发起任何网络请求。

**零实盘影响**：产物标 ``evidence_qualification: exploratory_reconstruction`` /
``live_effect: none``。历史分位是用**今天的**行业归属重建过去得到的（归属变更
日志 2026-09 才开始积累），因此不得进 research gate、不得生成订单、不得改权重。

Fail-closed：
- 行业归属缓存不可用 → 整体 ``blocked``，绝不用「其他」把全市场塞成一个板块；
- 缓存交易日不足 → 照样落产物，但分位判 unavailable 并写明缺口；
- 板块当日有效成分不足或覆盖率过低 → 该板块 unavailable，**不是** NORMAL。
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
try:
    from ._repo_bootstrap import ensure_repo_importable  # type: ignore[import-not-found]
except ImportError:
    from _repo_bootstrap import ensure_repo_importable  # type: ignore[no-redef]

ensure_repo_importable(ROOT)
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

import industry_map  # noqa: E402
import local_market_history as history  # noqa: E402
import sector_crowding  # noqa: E402
import sector_fake_breakout  # noqa: E402
import sector_price_factors  # noqa: E402
from paths import data_file  # noqa: E402
from state_store import atomic_write_json  # noqa: E402

ARTIFACT_NAME = "sector_crowding_latest.json"
PRICE_ARTIFACT_NAME = "sector_price_factors_latest.json"
FAKE_BREAKOUT_ARTIFACT_NAME = "sector_fake_breakout_latest.json"


def _artifact_path() -> str:
    return data_file("stock-triage", ARTIFACT_NAME)


def _price_artifact_path() -> str:
    return data_file("stock-triage", PRICE_ARTIFACT_NAME)


def _fake_breakout_artifact_path() -> str:
    return data_file("stock-triage", FAKE_BREAKOUT_ARTIFACT_NAME)


def build_series(
    asof: str,
    membership: dict[str, str],
    *,
    window: int,
    config: dict[str, Any],
    price_config: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """重建 ``asof`` 及之前 ``window`` 个**缓存里真实存在**的交易日的板块聚合。

    交易日来自缓存自身，而不是交易日历 —— 用日历会凭空造出缓存从没见过的日子。
    单趟读盘同时喂拥挤度与价格因子两条链，避免把同一段 SQLite 读两遍。

    返回 ``(拥挤度序列, 价格因子序列)``。
    """
    dates = history.trading_dates_between("1990-01-01", asof)[-(window + 1):]
    crowding_series: list[dict[str, Any]] = []
    day_bars: list[tuple[str, list[dict[str, Any]]]] = []
    for trading_date in dates:
        bars = history.get_bars_on(trading_date)
        if not bars:
            continue
        crowding_series.append(
            sector_crowding.aggregate_sector_day(
                bars, membership, trading_date=trading_date, config=config
            )
        )
        day_bars.append((trading_date, bars))
    price_series = sector_price_factors.build_daily_series(
        day_bars, membership, config=price_config
    )
    return crowding_series, price_series


def run(*, asof: str | None = None, write: bool = True) -> dict[str, Any]:
    trading_date = str(asof or date.today().isoformat())
    config = sector_crowding.load_config()

    membership = industry_map.load_cached(trading_date)
    if not membership:
        status = industry_map.load_cached_status(trading_date)
        return {
            "schema": sector_crowding.SCHEMA,
            "asof": trading_date,
            "status": "blocked",
            "reason": f"行业归属不可用（{status.get('status')}: {status.get('reason')}）",
            "live_effect": "none",
            "sectors": [],
        }

    price_config = sector_price_factors.load_config()
    series, price_series = build_series(
        trading_date,
        membership,
        window=int(config["percentile_window"]),
        config=config,
        price_config=price_config,
    )
    if not series:
        return {
            "schema": sector_crowding.SCHEMA,
            "asof": trading_date,
            "status": "blocked",
            "reason": "本地日线缓存没有任何可用交易日",
            "live_effect": "none",
            "sectors": [],
        }

    registered: dict[str, int] = {}
    for sector in membership.values():
        registered[sector] = registered.get(sector, 0) + 1

    payload = sector_crowding.build_sector_crowding(
        series,
        asof=str(series[-1]["trading_date"]),
        registered_members=registered,
        config=config,
    )
    payload["requested_asof"] = trading_date
    payload["membership_codes"] = len(membership)
    # 归属历史起点决定这份分位什么时候能升级成 canonical，写进产物省得下次再查。
    payload["membership_history"] = {
        key: value
        for key, value in industry_map.history_asof(trading_date).items()
        if key != "industry_by_code"
    }
    # 价格因子与拥挤度共用同一趟读盘，但落成**两份**产物：名字必须说清里面是什么，
    # 把价格因子塞进 sector_crowding_latest.json 会让下游读到名不副实的内容。
    price_payload = sector_price_factors.build_sector_price_factors(
        price_series,
        asof=str(price_series[-1]["trading_date"]) if price_series else trading_date,
        config=price_config,
    )
    price_payload["requested_asof"] = trading_date
    payload["price_factors"] = {
        key: value for key, value in price_payload.items() if key != "sectors"
    }

    fake_payload = _build_fake_breakout(price_series, series, payload, price_payload)
    payload["fake_breakout"] = {
        key: value for key, value in fake_payload.items() if key != "sectors"
    }

    if write:
        atomic_write_json(_artifact_path(), payload)
        atomic_write_json(_price_artifact_path(), price_payload)
        atomic_write_json(_fake_breakout_artifact_path(), fake_payload)
        payload["artifact"] = _artifact_path()
        payload["price_artifact"] = _price_artifact_path()
        payload["fake_breakout_artifact"] = _fake_breakout_artifact_path()
    return payload


def _build_fake_breakout(
    price_series: list[dict[str, Any]],
    crowding_series: list[dict[str, Any]],
    crowding_payload: dict[str, Any],
    price_payload: dict[str, Any],
) -> dict[str, Any]:
    """把已经算好的广度/集中度分位/拥挤分喂进假突破风险，不重算任何一项。"""
    fake_config = sector_fake_breakout.load_config()
    if not price_series:
        return {
            "schema": sector_fake_breakout.SCHEMA,
            "status": sector_fake_breakout.UNAVAILABLE,
            "reason": "价格序列为空",
            "sectors": [],
        }

    breadth_today: dict[str, float] = {}
    breadth_prior: dict[str, float] = {}
    price_settings = sector_price_factors.load_config()
    min_members = int(price_settings["min_members_observed"])
    for sector, entry in (price_series[-1].get("sectors") or {}).items():
        value = sector_price_factors.breadth(entry, min_members=min_members)
        if value is not None:
            breadth_today[sector] = value
    # 「突破以来的广度变化」用突破窗口起点那天的广度做基准，缺则不给（不补零）。
    lookback = min(len(price_series), int(fake_config["breakout_window"]))
    if lookback >= 2:
        for sector, entry in (price_series[-lookback].get("sectors") or {}).items():
            value = sector_price_factors.breadth(entry, min_members=min_members)
            if value is not None:
                breadth_prior[sector] = value

    concentration = {}
    crowding_scores = {}
    for row in crowding_payload.get("sectors") or []:
        if row.get("status") != "ok":
            continue
        sector = row["sector"]
        crowding_scores[sector] = row["score"]
        value = (row.get("components") or {}).get("concentration")
        if value is not None:
            concentration[sector] = value

    amounts: dict[str, list[float]] = {}
    for day in crowding_series:
        for sector, entry in (day.get("sectors") or {}).items():
            value = entry.get("amount")
            if value is not None:
                amounts.setdefault(sector, []).append(float(value))

    payload = sector_fake_breakout.build_sector_fake_breakout(
        price_series,
        asof=str(price_series[-1]["trading_date"]),
        breadth_by_sector=breadth_today,
        breadth_prior_by_sector=breadth_prior,
        concentration_percentiles=concentration,
        crowding_scores=crowding_scores,
        amounts_by_sector=amounts,
        config=fake_config,
    )
    payload["requested_asof"] = price_payload.get("requested_asof")
    return payload


def format_report(payload: dict[str, Any]) -> str:
    if payload.get("status") != "ok":
        return f"[{payload.get('asof')}] 板块拥挤度 {payload.get('status')}：{payload.get('reason')}"
    ranked = sorted(
        (row for row in payload.get("sectors") or [] if row.get("status") == "ok"),
        key=lambda row: -(row.get("score") or 0.0),
    )[:10]
    lines = [
        f"[{payload.get('asof')}] 板块拥挤度（研究口径，{payload.get('evidence_qualification')}）",
        f"  窗口 {payload.get('percentile_window')} 日 / 实得 {payload.get('history_sessions')} 日"
        f"；出分 {payload.get('scored_count')}/{payload.get('sector_count')}",
    ]
    for row in ranked:
        lines.append(
            f"  {row['sector']:<10} {row['score']:>6.1f}  {row['state']:<10}"
            f" 置信={row.get('confidence')} 成分={row.get('member_count')}"
        )
    price = payload.get("price_factors") or {}
    fake = payload.get("fake_breakout") or {}
    lines.append(
        f"  价格因子（未经验证）：{price.get('status')} 出分 {price.get('scored_count')}"
        f"/{price.get('sector_count')} 基准={price.get('market_basis')}"
    )
    lines.append(
        f"  假突破风险（未经验证）：{fake.get('status')} 出分 {fake.get('scored_count')}"
        f"/{fake.get('sector_count')}"
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="板块日度因子产物（RESEARCH ONLY）")
    parser.add_argument("--asof", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true", help="只算不落盘")
    args = parser.parse_args()

    payload = run(asof=args.asof, write=not args.no_write)
    if args.json:
        summary = {key: value for key, value in payload.items() if key != "sectors"}
        summary["sectors"] = [
            row for row in (payload.get("sectors") or []) if row.get("status") == "ok"
        ][:30]
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(format_report(payload))
    return 0 if payload.get("status") in {"ok", "unavailable"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
