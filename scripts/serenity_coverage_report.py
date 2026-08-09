#!/usr/bin/env python3
"""Serenity 深研覆盖率报告：候选池 top10 的 fresh 深研覆盖率。

§6c 验收指标的直接答案——"serenity 有没有起作用"。读 candidate_pool_latest
（候选池，取 top N，默认 top10）和 deep_research_cache（每只候选的深研缓存
状态），统计 fresh（非 stale、非 missing）深研覆盖率。纯只读、确定性、无网络
调用，可独立运行，也可挂进 performance-weekly 之类的周报链路。
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

from deep_research_cache import read_deep_research  # noqa: E402
from paths import data_file  # noqa: E402
from state_store import read_json  # noqa: E402

SCHEMA = "serenity_coverage_report_v1"
DEFAULT_TOP_N = 10


def _norm_code(value: Any) -> str:
    code = str(value or "").strip()
    return code.zfill(6) if code.isdigit() and code else code


def load_top_candidates(top_n: int = DEFAULT_TOP_N) -> list[dict[str, Any]]:
    """Top-N candidate_pool_latest entries, same readiness gate as
    research_dispatch.scan_candidate_trigger (pool.status == "ready")."""
    pool = read_json(data_file("stock-triage", "candidate_pool_latest.json"), {})
    if not isinstance(pool, dict) or pool.get("status") != "ready":
        return []
    return list((pool.get("candidates") or [])[:top_n])


def build_report(
    *,
    top_n: int = DEFAULT_TOP_N,
    candidates: list[dict[str, Any]] | None = None,
    cache_lookup: Any = None,
) -> dict[str, Any]:
    lookup = cache_lookup or read_deep_research
    entries = candidates if candidates is not None else load_top_candidates(top_n)
    if not entries:
        return {
            "schema": SCHEMA,
            "status": "no_candidate_pool",
            "top_n": top_n,
            "total": 0,
            "fresh": 0,
            "coverage_pct": None,
            "candidates": [],
        }

    rows = []
    fresh_count = 0
    for candidate in entries:
        code = _norm_code((candidate or {}).get("code"))
        if not code:
            continue
        cache = lookup(code)
        is_fresh = bool(cache) and not cache.get("stale")
        if is_fresh:
            fresh_count += 1
        rows.append({
            "code": code,
            "name": (candidate or {}).get("name"),
            "found": bool(cache),
            "stale": bool(cache.get("stale")) if cache else None,
            "fresh": is_fresh,
            "asof": cache.get("asof") if cache else None,
            "deep_score": cache.get("deep_score") if cache else None,
        })

    total = len(rows)
    coverage_pct = round(fresh_count / total * 100, 1) if total else None
    return {
        "schema": SCHEMA,
        "status": "ok",
        "top_n": top_n,
        "total": total,
        "fresh": fresh_count,
        "coverage_pct": coverage_pct,
        "candidates": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--out", help="Write JSON report to this path")
    args = parser.parse_args()

    report = build_report(top_n=args.top_n)

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
