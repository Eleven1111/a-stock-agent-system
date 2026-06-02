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


def load_evidence(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def lint_report(text: str, evidence: dict[str, Any] | None) -> dict[str, Any]:
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

    status = "pass"
    if any(f["severity"] == "high" for f in findings):
        status = "fail"
    elif findings:
        status = "warn"
    return {"status": status, "findings": findings}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report")
    parser.add_argument("--evidence")
    parser.add_argument("--out")
    args = parser.parse_args()

    text = Path(args.report).read_text(encoding="utf-8", errors="replace")
    evidence = load_evidence(args.evidence)
    result = lint_report(text, evidence)
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["status"] != "fail" else 2


if __name__ == "__main__":
    raise SystemExit(main())
