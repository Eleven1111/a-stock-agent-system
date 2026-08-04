#!/usr/bin/env python3
"""Compile a research proposal into a policy-gated paper execution artifact."""

from __future__ import annotations

import argparse
import json
import os
import site
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
site.addsitedir(os.path.join(ROOT, "skills", "common"))

import research_bus  # noqa: E402
from research_execution_plan import compile_execution_plan, persist_execution_plan  # noqa: E402
from state_store import read_json  # noqa: E402


def _trusted_approval_path(path: str) -> Path:
    """Resolve an approval only from the dedicated trusted state root.

    Hashed approval JSON outside this root remains untrusted.  Symlinks are
    rejected even when their target happens to remain inside the root so the
    production authorization source cannot change after path validation.
    """
    state_home = str(os.environ.get("A_STOCK_STATE_HOME") or "").strip()
    if not state_home:
        raise ValueError("A_STOCK_STATE_HOME is required for approval artifacts")
    trusted_root = (
        Path(state_home).expanduser() / "approvals" / "research-committee"
    ).resolve()
    lexical = Path(os.path.abspath(os.path.expanduser(path)))
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise ValueError("approval artifact is not a readable real file") from exc
    if lexical != resolved:
        raise ValueError("approval artifact symlink is not allowed")
    try:
        resolved.relative_to(trusted_root)
    except ValueError as exc:
        raise ValueError("approval artifact is outside trusted approval root") from exc
    if not resolved.is_file():
        raise ValueError("approval artifact is not a regular file")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description="compile research proposal into gated plan")
    parser.add_argument("--task", required=True)
    parser.add_argument("--market-context", required=True)
    parser.add_argument("--portfolio", required=True)
    parser.add_argument(
        "--synthesis-artifact",
        "--synthesis",
        dest="synthesis_artifact",
        help="canonical research_synthesis_v1 JSON artifact",
    )
    parser.add_argument(
        "--approval-artifact",
        "--approval",
        dest="approval_artifact",
        help="independent research_proposal_approval_v1 JSON artifact",
    )
    parser.add_argument(
        "--now",
        help="timezone-aware compile timestamp (for deterministic replay/tests)",
    )
    parser.add_argument("--output")
    args = parser.parse_args()
    task = research_bus.find_task(args.task)
    if not task:
        raise SystemExit("task not found")
    proposal = read_json(
        os.path.join(research_bus.proposals_dir("pending"), f"{args.task}.json"),
        None,
    )
    if not isinstance(proposal, dict):
        proposal = read_json(
            os.path.join(research_bus.proposals_dir("approved"), f"{args.task}.json"),
            None,
        )
    if not isinstance(proposal, dict):
        raise SystemExit("research proposal not found")
    market = json.loads(Path(args.market_context).read_text(encoding="utf-8"))
    portfolio = json.loads(Path(args.portfolio).read_text(encoding="utf-8"))
    synthesis_path = args.synthesis_artifact
    if not synthesis_path:
        candidate_ref = str(proposal.get("synthesis_ref") or "")
        if candidate_ref and os.path.isfile(candidate_ref):
            synthesis_path = candidate_ref
    synthesis = (
        json.loads(Path(synthesis_path).read_text(encoding="utf-8"))
        if synthesis_path
        else None
    )
    approval = (
        json.loads(
            _trusted_approval_path(args.approval_artifact).read_text(
                encoding="utf-8",
            )
        )
        if args.approval_artifact
        else None
    )
    if synthesis is None and approval is None:
        raise SystemExit(
            "provide --synthesis-artifact or --approval-artifact"
        )
    result = compile_execution_plan(
        proposal,
        market_context=market,
        portfolio=portfolio,
        synthesis_artifact=synthesis,
        approval_artifact=approval,
        now=args.now,
    )
    if args.output:
        persist_execution_plan(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
