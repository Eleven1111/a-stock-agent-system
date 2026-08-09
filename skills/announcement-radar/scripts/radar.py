#!/usr/bin/env python3
"""公告召回雷达 —— 抓取 → 分类 → 评分 → 三桶召回 → artifact + lite 简报。

    python skills/announcement-radar/scripts/radar.py --date 2026-08-01
    python skills/announcement-radar/scripts/radar.py --date today --emit-brief

产出：
    $A_STOCK_STATE_HOME/cron/output/announcement-radar/<date>.json   全量召回集
    stdout                                                           lite 简报

**不产出 Top-N 名单。** 分数只用于三桶阈值判定，理由见 ``recall.py``。
全市场单日约 50 次接口请求，受 http_client 的 2.5s/源 节流约束，
正常耗时 2~4 分钟 —— 不能套用其他作业的 15s 超时。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

import recall  # noqa: E402
from classify import Classifier  # noqa: E402
from score import Scorer  # noqa: E402

import industry_map  # noqa: E402
import paths  # noqa: E402
from cninfo_client import fetch_day  # noqa: E402

CN_TZ = ZoneInfo("Asia/Shanghai")
JOB_NAME = "announcement-radar"


def resolve_day(value: str) -> str:
    if value in {"today", ""}:
        return datetime.now(tz=CN_TZ).date().isoformat()
    datetime.strptime(value, "%Y-%m-%d")  # fail loud on a malformed date
    return value


def enrich(rows: list[dict[str, Any]], day: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """分类 + 评分。返回 (rows, stats)。"""
    classifier = Classifier()
    scorer = Scorer()
    industry_by_code = industry_map.load_cached(day)

    unclassified = 0
    industry_hits = 0
    out: list[dict[str, Any]] = []

    for row in rows:
        result = classifier.classify(row.get("title", ""))
        unclassified += int(result["unclassified"])

        code = str(row.get("code") or "").zfill(6)
        industry = industry_by_code.get(code, "")
        group = scorer.industry_group(industry)
        industry_hits += int(scorer.knows_industry(industry))

        scored = scorer.score(
            result["l2"],
            result["stage"],
            group,
            row.get("title", ""),
        )
        out.append({
            **row,
            "l1": result["l1"],
            "l2": result["l2"],
            "matched_kw": result["matched_kw"],
            "stage": result["stage"],
            "industry_group": group,
            **scored,
        })

    total = len(rows) or 1
    stats = {
        "fetched": len(rows),
        "classified_rate": (total - unclassified) / total,
        "unclassified": unclassified,
        "skipped_periodic": sum(1 for r in out if r.get("skipped")),
        # 行业映射缓存过期/缺失会静默退化为全 "其他"，这里显式暴露识别率，
        # 不让降级无声发生（见 memory: adata-industry-map）。
        # 度量的是「行业名被认出来」，不是「归入非其他组」——金融行业本就归其他。
        "industry_mapped_rate": industry_hits / total,
        "scorer_mode": scorer.mode,
    }
    return out, stats


def artifact_path(day: str) -> str:
    directory = os.path.join(paths.cron_output_dir(), JOB_NAME)
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, f"{day}.json")


def write_artifact(path: str, payload: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def run(day: str, *, dry_run: bool = False) -> tuple[str, dict[str, Any]]:
    rules = recall.load_rules()

    raw = fetch_day(day)
    rows, stats = enrich(raw, day)
    result = recall.select(rows, rules)

    warnings = recall.check_guardrails(
        fetched=stats["fetched"],
        classified_rate=stats["classified_rate"],
        selected=len(result["rows"]),
        rules=rules,
    )

    payload = {
        "job": JOB_NAME,
        "date": day,
        "generated_at": datetime.now(tz=CN_TZ).isoformat(timespec="seconds"),
        "stats": {**stats, "selected": len(result["rows"]), "companies": result["companies"]},
        "counts": result["counts"],
        "warnings": warnings,
        "rows": result["rows"],
        "disclaimer": (
            "分流结果，非排序名单、非投资建议。"
            "分数仅用于桶阈值判定；Top-N 排序无唯一解，不得据此选股。"
        ),
    }

    path = ""
    if not dry_run:
        path = artifact_path(day)
        write_artifact(path, payload)

    return path, payload


def main() -> int:
    parser = argparse.ArgumentParser(description="公告召回雷达")
    parser.add_argument("--date", default="today", help="YYYY-MM-DD 或 today")
    parser.add_argument("--dry-run", action="store_true", help="不落 artifact")
    parser.add_argument(
        "--emit-brief",
        action="store_true",
        help="打印 lite 简报（默认只打印统计行）",
    )
    args = parser.parse_args()

    day = resolve_day(args.date)
    try:
        path, payload = run(day, dry_run=args.dry_run)
    except recall.RecallGuardrailError as exc:
        print(f"[announcement-radar] 护栏拦截：{exc}", file=sys.stderr)
        return 2

    stats = payload["stats"]
    print(
        f"[announcement-radar] {day} 抓取 {stats['fetched']} 条，"
        f"命中率 {stats['classified_rate']:.1%}，"
        f"行业映射 {stats['industry_mapped_rate']:.1%}，"
        f"召回 {stats['selected']} 条 / {stats['companies']} 家"
    )
    for warning in payload["warnings"]:
        print(f"[announcement-radar] warn: {warning}", file=sys.stderr)

    if args.emit_brief:
        rules = recall.load_rules()
        print()
        print(recall.build_brief(
            {"rows": payload["rows"], "counts": payload["counts"],
             "companies": stats["companies"]},
            rules,
            day=day,
        ))
    if path:
        print(f"[announcement-radar] artifact -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
