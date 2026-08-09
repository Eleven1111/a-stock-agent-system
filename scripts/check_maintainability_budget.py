#!/usr/bin/env python3
"""Repository-wide maintainability debt baseline and no-regression gate."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import pathlib
import subprocess
from collections import Counter, defaultdict
from datetime import date
from typing import Iterable


ROOT = pathlib.Path(__file__).resolve().parents[1]
BASELINE_FILE = ROOT / "config" / "maintainability_baseline.json"
WAIVERS_FILE = ROOT / "config" / "maintainability_waivers.json"
PRODUCTION_PREFIXES = ("skills/", "scripts/")
EXCLUDED_PARTS = {"third_party", ".venv", "__pycache__"}


def _stable_id(path: str, kind: str, symbol: str, ordinal: int = 0) -> str:
    raw = f"{path}|{kind}|{symbol}|{ordinal}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


class DebtVisitor(ast.NodeVisitor):
    def __init__(self, path: str, source_lines: list[str]):
        self.path = path
        self.source_lines = source_lines
        self.scope: list[str] = []
        self.records: list[dict[str, object]] = []
        self.ordinals: defaultdict[tuple[str, str], int] = defaultdict(int)

    def _symbol(self) -> str:
        return ".".join(self.scope) or "<module>"

    def _add(self, kind: str, line: int, *, symbol: str | None = None) -> None:
        name = symbol or self._symbol()
        key = (kind, name)
        ordinal = self.ordinals[key]
        self.ordinals[key] += 1
        self.records.append(
            {
                "id": _stable_id(self.path, kind, name, ordinal),
                "kind": kind,
                "path": self.path,
                "line": line,
                "symbol": name,
            }
        )

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        end = int(getattr(node, "end_lineno", node.lineno))
        if end - node.lineno + 1 > 80:
            self._add("long_function", node.lineno)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        broad = node.type is None
        if isinstance(node.type, ast.Name):
            broad = node.type.id in {"Exception", "BaseException"}
        if broad:
            self._add("broad_exception", node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in {"insert", "append", "extend"}
            and isinstance(func.value, ast.Attribute)
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "sys"
            and func.value.attr == "path"
        ):
            self._add("sys_path_mutation", node.lineno)
        elif isinstance(func, ast.Attribute) and func.attr == "addsitedir":
            # site.addsitedir is import-path surgery too; counting only
            # sys.path.* let it through and understated the real total.
            self._add("sys_path_mutation", node.lineno)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        # `sys.path = [...]` and `sys.path[0:0] = [...]` reach the same place
        # as .insert(); without these the counter is trivially side-steppable.
        for target in node.targets:
            base = target.value if isinstance(target, ast.Subscript) else target
            if (
                isinstance(base, ast.Attribute)
                and base.attr == "path"
                and isinstance(base.value, ast.Name)
                and base.value.id == "sys"
            ):
                self._add("sys_path_mutation", node.lineno)
        self.generic_visit(node)


def analyze_source(path: str, source: str) -> list[dict[str, object]]:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return [
            {
                "id": _stable_id(path, "syntax_error", "<module>"),
                "kind": "syntax_error",
                "path": path,
                "line": exc.lineno or 1,
                "symbol": "<module>",
            }
        ]
    visitor = DebtVisitor(path, source.splitlines())
    visitor.visit(tree)
    return visitor.records


def _production_path(path: str) -> bool:
    parts = pathlib.PurePosixPath(path).parts
    return (
        path.endswith(".py")
        and path.startswith(PRODUCTION_PREFIXES)
        and not any(part in EXCLUDED_PARTS for part in parts)
    )


def scan_worktree(root: pathlib.Path = ROOT) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for prefix in PRODUCTION_PREFIXES:
        for path in sorted((root / prefix).rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            if _production_path(relative):
                records.extend(analyze_source(relative, path.read_text(encoding="utf-8")))
    return sorted(records, key=lambda item: (str(item["path"]), int(item["line"]), str(item["kind"])))


def _git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=False
    )
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result.stdout


def scan_git_ref(ref: str) -> list[dict[str, object]]:
    paths = [
        path
        for path in _git("ls-tree", "-r", "--name-only", ref).splitlines()
        if _production_path(path)
    ]
    records: list[dict[str, object]] = []
    for path in paths:
        source = _git("show", f"{ref}:{path}")
        records.extend(analyze_source(path, source))
    return sorted(records, key=lambda item: (str(item["path"]), int(item["line"]), str(item["kind"])))


def _counts(records: Iterable[dict[str, object]]) -> dict[str, int]:
    return dict(sorted(Counter(str(item["kind"]) for item in records).items()))


def baseline_payload(ref: str) -> dict[str, object]:
    records = scan_git_ref(ref)
    return {
        "schema": "maintainability_baseline_v1",
        "baseline_ref": ref,
        "budget": _counts(records),
        "violations": records,
    }


def changed_files(base_ref: str) -> set[str]:
    tracked = set(_git("diff", "--name-only", base_ref, "--").splitlines())
    untracked = set(_git("ls-files", "--others", "--exclude-standard").splitlines())
    return {path for path in tracked | untracked if _production_path(path)}


def waiver_payload(base_ref: str, *, expires_on: str) -> dict[str, object]:
    changed = changed_files(base_ref)
    records = [item for item in scan_worktree() if item["path"] in changed]
    return {
        "schema": "maintainability_waivers_v1",
        "waivers": [
            {
                "violation_id": item["id"],
                "expires_on": expires_on,
                "reason": "pre-existing debt in a risk-boundary file modified by audit remediation",
            }
            for item in records
        ],
    }


def check_budget(base_ref: str) -> dict[str, object]:
    baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    waiver_doc = json.loads(WAIVERS_FILE.read_text(encoding="utf-8"))
    waivers = {
        str(item["violation_id"]): item
        for item in waiver_doc.get("waivers") or []
        if str(item.get("expires_on") or "") >= date.today().isoformat()
    }
    current = scan_worktree()
    current_counts = _counts(current)
    budget = {str(key): int(value) for key, value in (baseline.get("budget") or {}).items()}
    baseline_ids = {str(item["id"]) for item in baseline.get("violations") or []}
    changed = changed_files(base_ref)
    errors: list[str] = []
    for kind, count in current_counts.items():
        if count > budget.get(kind, 0):
            errors.append(f"{kind} count {count} exceeds budget {budget.get(kind, 0)}")
    for item in current:
        violation_id = str(item["id"])
        if violation_id not in baseline_ids and violation_id not in waivers:
            errors.append(f"new violation {violation_id} {item['path']}:{item['line']}")
        if item["path"] in changed and violation_id not in waivers:
            errors.append(f"modified-file violation lacks waiver {violation_id} {item['path']}:{item['line']}")
    return {
        "schema": "maintainability_check_v1",
        "ok": not errors,
        "baseline_counts": budget,
        "current_counts": current_counts,
        "changed_production_files": sorted(changed),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-ref", default=os.environ.get("MAINTAINABILITY_BASE_REF", "origin/main"))
    parser.add_argument("--emit-baseline", action="store_true")
    parser.add_argument("--emit-waivers", action="store_true")
    parser.add_argument("--expires-on", default="2026-10-31")
    args = parser.parse_args()
    if args.emit_baseline:
        payload = baseline_payload(args.base_ref)
    elif args.emit_waivers:
        payload = waiver_payload(args.base_ref, expires_on=args.expires_on)
    else:
        payload = check_budget(args.base_ref)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
