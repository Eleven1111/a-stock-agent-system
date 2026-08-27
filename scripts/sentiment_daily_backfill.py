#!/usr/bin/env python3
"""``sentiment_daily`` 历史回填 CLI（升级方案 P0-b）。

只读本地日线缓存（``market/history.sqlite3``，由 market-history-cache 作业维护），
按交易日重算方案 §3.1(a) 的字段并落进 ``$A_STOCK_STATE_HOME/market/sentiment_daily/``。
**不触网**：缓存里没有的交易日就是没有，脚本如实少产出一天，绝不去补抓。

日线能给什么、不能给什么，这里是明确的：

- 可回填：limit/touch/break_rate/premium/red_ratio/adr/max_board/board4plus
  （连板高度由缓存内连续封板天数递推，不依赖任何盘中产物）；
- 恒不可用：``sector_breadth_top``（需板块成分表）与
  ``leader_damage_intraday_drawdown``（需分钟线）。二者写 ``null`` 并进
  ``unavailable_fields``，不用日线代理插值。

另两条已知偏差，写在这里而不是留给读数的人自己发现：
1. 涨跌停幅度按**当前**证券名称判 ST（缓存无历史名称），历史上曾 ST 而现已摘帽的
   个股会按 10% 判定；
2. 覆盖率分母取窗口内缓存见过的证券数，缓存本身不全时按 ``coverage_status=partial``
   标记该日，而不是把半个市场的家数当全市场口径。
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Mapping

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
try:
    from ._repo_bootstrap import ensure_repo_importable  # type: ignore[import-not-found]
except ImportError:
    from _repo_bootstrap import ensure_repo_importable  # type: ignore[no-redef]

ensure_repo_importable(ROOT)
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

import local_market_history as history  # noqa: E402
import sentiment_daily  # noqa: E402
from paths import data_file  # noqa: E402
from state_store import read_json  # noqa: E402

SOURCE = "local_history_cache"


def bootstrap(*, min_days: int, end_date: str) -> dict[str, Any]:
    """Seed the rolling history once, without overwriting forward daily rows.

    Existing forward rows have richer intraday/sector evidence than a daily-bar
    reconstruction.  The bootstrap window therefore ends on the last cached
    trading day strictly before the earliest existing row.
    """
    existing = sentiment_daily.load_summary()
    if len(existing) >= int(min_days):
        return {
            "status": "ok", "skipped": True, "reason": "minimum_history_ready",
            "observed_days": len(existing), "required_days": int(min_days),
        }
    bootstrap_end = end_date
    if existing:
        earliest = min(str(row.get("trading_date") or "") for row in existing)
        earlier = history.trading_dates_between("1990-01-01", earliest)
        earlier = [day for day in earlier if day < earliest]
        if not earlier:
            return {
                "status": "blocked", "reason": "no_history_before_forward_rows",
                "observed_days": len(existing), "required_days": int(min_days),
            }
        bootstrap_end = earlier[-1]
    result = backfill(
        start_date="1990-01-01", end_date=bootstrap_end, limit_days=int(min_days)
    )
    result["bootstrap"] = True
    result["preserved_forward_days"] = len(existing)
    result["required_days"] = int(min_days)
    return result


def load_name_map() -> dict[str, str]:
    """代码 → 证券名称（判 ST 用）。缺失则返回空表，ST 一律按板块常规幅度判定。"""
    payload = read_json(data_file("stock-triage", "exchange_universe.json"), {})
    rows = payload.get("stocks") if isinstance(payload, Mapping) else None
    names: dict[str, str] = {}
    for row in rows or []:
        if isinstance(row, Mapping) and row.get("code"):
            names[str(row["code"]).zfill(6)] = str(row.get("name") or "")
    return names


def _quote_rows(bars: list[dict[str, Any]], names: Mapping[str, str]) -> list[dict[str, Any]]:
    """缓存行 → 归一化行情行。归一化在这里做一次，后续封板判定不再碰 None。"""
    return sentiment_daily.normalize_rows([
        {
            "code": str(bar.get("code") or "").zfill(6),
            "name": names.get(str(bar.get("code") or "").zfill(6), ""),
            "open": bar.get("open"),
            "high": bar.get("high"),
            "close": bar.get("close"),
            "preclose": bar.get("preclose"),
        }
        for bar in bars
    ])


def advance_streaks(
    streaks: Mapping[str, int], rows: list[Mapping[str, Any]]
) -> dict[str, int]:
    """按当日封板情况递推连板高度。未出现在当日行情里的代码沿用旧值（停牌不清零，
    也不 +1）——把停牌当断板会系统性低估最高板。"""
    updated = dict(streaks)
    for row in rows:
        code = str(row.get("code") or "")
        flags = sentiment_daily.limit_flags(row)
        updated[code] = (updated.get(code, 0) + 1) if flags["sealed"] else 0
    return updated


def _ladder(streaks: Mapping[str, int]) -> dict[str, dict[str, int]]:
    return {code: {"height": height} for code, height in streaks.items() if height > 0}


def _leader_code(streaks: Mapping[str, int]) -> str | None:
    ranked = sorted(
        ((height, code) for code, height in streaks.items() if height > 0), reverse=True
    )
    return ranked[0][1] if ranked else None


def backfill(
    *, start_date: str, end_date: str, limit_days: int | None = None
) -> dict[str, Any]:
    """回填窗口内每个**缓存里真实存在**的交易日。返回计数摘要，不返回序列本体。"""
    dates = history.trading_dates_between(start_date, end_date)
    if limit_days is not None:
        dates = dates[-limit_days:]
    if not dates:
        return {"status": "blocked", "reason": "empty_history_cache",
                "start_date": start_date, "end_date": end_date, "written_days": 0}
    names = load_name_map()
    # 分母优先取交易所全量名册：拿缓存自己的证券数当分母，缓存只有 300 只时也会
    # 算出 100% 覆盖率，正好把最需要暴露的缺口盖掉。
    expected = sentiment_daily.universe_expected() or history.distinct_code_count(
        start_date, end_date
    )
    streaks: dict[str, int] = {}
    prev_sealed: list[str] = []
    prev_leader: str | None = None
    written: list[str] = []
    partial: list[str] = []
    for trading_date in dates:
        rows = _quote_rows(history.get_bars_on(trading_date), names)
        streaks = advance_streaks(streaks, rows)
        computed = sentiment_daily.compute_sentiment_metrics(
            rows,
            prev_limit_codes=prev_sealed,
            ladder=_ladder(streaks),
            leader_code=prev_leader,
            sector_breadth_top=None,
        )
        outcome = sentiment_daily.persist_metrics(
            computed,
            trading_date=trading_date,
            snapshot_ref=f"{SOURCE}:{trading_date}",
            source=SOURCE,
            universe_expected=expected or None,
        )
        if outcome.get("status") == "blocked":
            return {"status": "blocked", "reason": outcome.get("reason"),
                    "trading_date": trading_date, "errors": outcome.get("errors"),
                    "written_days": len(written)}
        written.append(trading_date)
        if outcome.get("coverage_status") == "partial":
            partial.append(trading_date)
        prev_sealed = [
            str(row.get("code"))
            for row in rows
            if sentiment_daily.limit_flags(row)["sealed"]
        ]
        prev_leader = _leader_code(streaks)
    return {
        "status": "ok",
        "start_date": written[0],
        "end_date": written[-1],
        "written_days": len(written),
        "partial_coverage_days": len(partial),
        "universe_expected": expected,
        "always_unavailable_fields": [
            "sector_breadth_top", "leader_damage_intraday_drawdown"
        ],
        "output_dir": sentiment_daily.sentiment_daily_dir(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default="1990-01-01")
    parser.add_argument("--end-date", default="2100-01-01")
    parser.add_argument("--limit-days", type=int,
                        help="只回填窗口内最近 N 个交易日")
    parser.add_argument("--bootstrap-min-days", type=int,
                        help="仅当现有序列不足 N 天时，在已有前向记录之前一次性播种 N 天")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)
    result = (
        bootstrap(min_days=args.bootstrap_min_days, end_date=args.end_date)
        if args.bootstrap_min_days is not None
        else backfill(start_date=args.start_date, end_date=args.end_date, limit_days=args.limit_days)
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"[sentiment_daily] status={result.get('status')} "
              f"days={result.get('written_days')} "
              f"partial={result.get('partial_coverage_days')} "
              f"range={result.get('start_date')}..{result.get('end_date')}")
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
