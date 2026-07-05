#!/usr/bin/env python3
"""Lint Serenity investment reports for evidence and structure."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

REQUIRED_PHRASES = ["不构成任何投资建议"]
REQUIRED_SECTIONS = ["一句话结论", "评分", "风险", "反面", "资料来源"]
HIGH_RISK_WORDS = ["一定上涨", "必然上涨", "稳赚", "无风险", "保底", "确定翻倍"]

DEFAULT_MIN_SOURCES = 25

# Heading patterns for the industry-chain deep-scan gates. Both Chinese and
# English wording are accepted so bilingual reports pass.
DOWNGRADED_PATTERNS = [
    r"被降级的热门方向",
    r"降级.{0,6}方向",
    r"downgrad\w*\s+hot",
    r"overhyped",
    r"prove\s+this\s+theme",
]
TIER_PATTERNS = [
    r"产业链层级",
    r"层级排序",
    r"价值链层级",
    r"chokepoint\s+ranking",
    r"chokepoint\s*排名",
    r"value[- ]?chain\s+tier",
    r"tier\s+ranking",
]
COMPANY_RANK_PATTERNS = [
    r"公司排序",
    r"标的池",
    r"标的排序",
    r"公司排名",
    r"company\s+ranking",
    r"watchlist",
]
RED_FLAG_PATTERNS = [
    r"红旗",
    r"red[\s-]?flag",
]


def load_evidence(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def _first_match_index(text: str, patterns: list[str]) -> int | None:
    best: int | None = None
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match and (best is None or match.start() < best):
            best = match.start()
    return best


def _has_any(text: str, patterns: list[str]) -> bool:
    return _first_match_index(text, patterns) is not None


def _resolve_report_type(report_type: str | None, evidence: dict[str, Any] | None) -> str:
    if report_type and report_type != "auto":
        return report_type
    if evidence is not None:
        inferred = evidence.get("research_type")
        if inferred:
            return str(inferred)
    return "generic"


def lint_report(
    text: str,
    evidence: dict[str, Any] | None,
    *,
    report_type: str | None = None,
    min_sources: int = DEFAULT_MIN_SOURCES,
) -> dict[str, Any]:
    findings = []
    for phrase in REQUIRED_PHRASES:
        if phrase not in text:
            findings.append({"severity": "high", "message": f"Missing required phrase: {phrase}"})
    for section in REQUIRED_SECTIONS:
        if section not in text:
            findings.append({"severity": "high", "message": f"Missing required section keyword: {section}"})
    for word in HIGH_RISK_WORDS:
        if word in text:
            findings.append({"severity": "high", "message": f"Unsafe investment wording: {word}"})

    source_like = len(re.findall(r"https?://|\|.*[SABCD].*\|", text))
    if source_like < 3:
        findings.append({"severity": "medium", "message": "Report appears to have too few citations/source rows"})

    resolved_type = _resolve_report_type(report_type, evidence)

    if evidence is not None:
        entries = evidence.get("entries", [])
        if len(entries) < 3:
            findings.append({"severity": "medium", "message": "Evidence ledger has fewer than 3 entries"})
        strong = [e for e in entries if e.get("grade") in {"S", "A"}]
        if len(strong) < 2:
            findings.append({"severity": "high", "message": "Evidence ledger has fewer than 2 S/A entries"})
        ids = [e.get("id") for e in entries if e.get("id")]
        cited_ids = [eid for eid in ids if eid in text]
        if ids and not cited_ids:
            findings.append({"severity": "medium", "message": "Report does not cite evidence IDs from ledger"})

        # Red-flag disclosure gate applies to any report type: if the ledger
        # records red_flag entries, the report must disclose them.
        red_flags = [e for e in entries if e.get("claim_type") == "red_flag"]
        if red_flags and not _has_any(text, RED_FLAG_PATTERNS):
            findings.append(
                {
                    "severity": "high",
                    "message": (
                        f"Evidence ledger has {len(red_flags)} red_flag entries "
                        "but report has no red-flag (红旗清单) disclosure section"
                    ),
                }
            )

    # Industry-chain deep-scan hard gates. single_stock / comparison / generic
    # reports are exempt from source-count, downgraded-direction, and ordering.
    if resolved_type == "industry_chain":
        _lint_industry_chain(text, evidence, min_sources, findings)

    status = "pass"
    if any(f["severity"] == "high" for f in findings):
        status = "fail"
    elif findings:
        status = "warn"
    return {"status": status, "findings": findings, "report_type": resolved_type}


def _lint_industry_chain(
    text: str,
    evidence: dict[str, Any] | None,
    min_sources: int,
    findings: list[dict[str, Any]],
) -> None:
    # (1) minimum source count
    if evidence is not None:
        n_sources = len(evidence.get("entries", []))
        if n_sources < min_sources:
            findings.append(
                {
                    "severity": "high",
                    "message": (
                        f"Industry-chain deep scan needs >= {min_sources} evidence sources, "
                        f"found {n_sources}"
                    ),
                }
            )

    # (2) downgraded hot-direction chapter (anti-consensus check)
    if not _has_any(text, DOWNGRADED_PATTERNS):
        findings.append(
            {
                "severity": "high",
                "message": "Missing 被降级的热门方向 (downgraded hot direction) chapter",
            }
        )

    # (4) tier ranking must precede company ranking
    tier_idx = _first_match_index(text, TIER_PATTERNS)
    company_idx = _first_match_index(text, COMPANY_RANK_PATTERNS)
    if tier_idx is None:
        findings.append(
            {
                "severity": "high",
                "message": "Missing 产业链层级排序 (value-chain tier ranking) section",
            }
        )
    elif company_idx is not None and tier_idx > company_idx:
        findings.append(
            {
                "severity": "high",
                "message": (
                    "Tier ranking (层级排序) must appear before company ranking (公司排序)"
                ),
            }
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report")
    parser.add_argument("--evidence")
    parser.add_argument("--out")
    parser.add_argument(
        "--report-type",
        default="auto",
        choices=["auto", "single_stock", "industry_chain", "comparison", "generic"],
        help="Report type; 'auto' infers from evidence.research_type.",
    )
    parser.add_argument(
        "--min-sources",
        type=int,
        default=DEFAULT_MIN_SOURCES,
        help="Minimum evidence sources for an industry_chain deep scan.",
    )
    args = parser.parse_args()

    text = Path(args.report).read_text(encoding="utf-8", errors="replace")
    evidence = load_evidence(args.evidence)
    result = lint_report(
        text,
        evidence,
        report_type=args.report_type,
        min_sources=args.min_sources,
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["status"] != "fail" else 2


if __name__ == "__main__":
    raise SystemExit(main())
