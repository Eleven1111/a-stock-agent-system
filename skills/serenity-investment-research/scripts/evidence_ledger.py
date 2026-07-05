#!/usr/bin/env python3
"""Create, update, and validate Serenity research evidence ledgers."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALID_GRADES = {"S", "A", "B", "C", "D"}
VALID_CLAIM_TYPES = {
    "fact",
    "source-backed inference",
    "third-party summary",
    "researcher inference",
    "red_flag",
}
VALID_CONFIDENCE = {"high", "medium", "low"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def next_id(entries: list[dict[str, Any]]) -> str:
    max_num = 0
    for entry in entries:
        raw = str(entry.get("id", ""))
        if raw.startswith("E") and raw[1:].isdigit():
            max_num = max(max_num, int(raw[1:]))
    return f"E{max_num + 1:03d}"


def validate_ledger(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not data.get("target"):
        errors.append("ledger.target is required")
    if not data.get("research_type"):
        errors.append("ledger.research_type is required")
    entries = data.get("entries")
    if not isinstance(entries, list):
        errors.append("ledger.entries must be a list")
        return errors

    seen_ids: set[str] = set()
    for idx, entry in enumerate(entries, start=1):
        prefix = f"entry[{idx}]"
        entry_id = entry.get("id")
        if not entry_id:
            errors.append(f"{prefix}.id is required")
        elif entry_id in seen_ids:
            errors.append(f"{prefix}.id duplicates {entry_id}")
        else:
            seen_ids.add(entry_id)

        for field in ("claim", "claim_type", "source_title", "date", "grade", "supports", "confidence"):
            if entry.get(field) in (None, "", []):
                errors.append(f"{prefix}.{field} is required")

        if entry.get("grade") and entry["grade"] not in VALID_GRADES:
            errors.append(f"{prefix}.grade must be one of {sorted(VALID_GRADES)}")
        if entry.get("claim_type") and entry["claim_type"] not in VALID_CLAIM_TYPES:
            errors.append(f"{prefix}.claim_type must be one of {sorted(VALID_CLAIM_TYPES)}")
        if entry.get("confidence") and entry["confidence"] not in VALID_CONFIDENCE:
            errors.append(f"{prefix}.confidence must be one of {sorted(VALID_CONFIDENCE)}")
        if entry.get("supports") and not isinstance(entry["supports"], list):
            errors.append(f"{prefix}.supports must be a list")
        if not entry.get("url") and not entry.get("local_path"):
            errors.append(f"{prefix} needs url or local_path")

    return errors


def cmd_init(args: argparse.Namespace) -> int:
    out = Path(args.out)
    data = {
        "target": args.target,
        "research_type": args.research_type,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "entries": [],
    }
    write_json(out, data)
    print(f"initialized {out}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    path = Path(args.ledger)
    data = read_json(path)
    entries = data.setdefault("entries", [])
    if not isinstance(entries, list):
        raise SystemExit("ledger.entries must be a list")

    entry = {
        "id": args.id or next_id(entries),
        "claim": args.claim,
        "claim_type": args.claim_type,
        "source_title": args.source_title,
        "source_type": args.source_type,
        "url": args.url,
        "local_path": args.local_path,
        "date": args.date,
        "grade": args.grade,
        "excerpt": args.excerpt,
        "supports": [s for s in args.supports.split(",") if s] if args.supports else [],
        "confidence": args.confidence,
        "notes": args.notes,
    }
    entries.append(entry)
    data["updated_at"] = now_iso()
    errors = validate_ledger(data)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 2
    write_json(path, data)
    print(f"added {entry['id']} to {path}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    path = Path(args.ledger)
    data = read_json(path)
    errors = validate_ledger(data)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 2
    print(f"ok: {path} ({len(data.get('entries', []))} entries)")
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    data = read_json(Path(args.ledger))
    entries = data.get("entries", [])
    by_grade: dict[str, int] = {}
    by_tag: dict[str, int] = {}
    for entry in entries:
        by_grade[entry.get("grade", "?")] = by_grade.get(entry.get("grade", "?"), 0) + 1
        for tag in entry.get("supports", []) or []:
            by_tag[tag] = by_tag.get(tag, 0) + 1
    print(json.dumps({"entries": len(entries), "by_grade": by_grade, "by_tag": by_tag}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create a new evidence ledger")
    p_init.add_argument("--target", required=True)
    p_init.add_argument("--research-type", required=True)
    p_init.add_argument("--out", required=True)
    p_init.set_defaults(func=cmd_init)

    p_add = sub.add_parser("add", help="add one evidence entry")
    p_add.add_argument("--ledger", required=True)
    p_add.add_argument("--id")
    p_add.add_argument("--claim", required=True)
    p_add.add_argument("--claim-type", default="fact", choices=sorted(VALID_CLAIM_TYPES))
    p_add.add_argument("--source-title", required=True)
    p_add.add_argument("--source-type", default="")
    p_add.add_argument("--url", default="")
    p_add.add_argument("--local-path", default="")
    p_add.add_argument("--date", required=True)
    p_add.add_argument("--grade", required=True, choices=sorted(VALID_GRADES))
    p_add.add_argument("--excerpt", default="")
    p_add.add_argument("--supports", default="", help="comma-separated tags")
    p_add.add_argument("--confidence", default="medium", choices=sorted(VALID_CONFIDENCE))
    p_add.add_argument("--notes", default="")
    p_add.set_defaults(func=cmd_add)

    p_validate = sub.add_parser("validate", help="validate a ledger")
    p_validate.add_argument("ledger")
    p_validate.set_defaults(func=cmd_validate)

    p_summary = sub.add_parser("summary", help="summarize a ledger")
    p_summary.add_argument("ledger")
    p_summary.set_defaults(func=cmd_summary)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
