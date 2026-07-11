# Contributing

Changes must preserve the fail-closed A-share decision contract in
`AGENTS.md`. In particular, unknown or stale evidence cannot become neutral,
research-only strategies cannot influence live ranking, and execution plans
must respect T+1.

## Development

Use Python 3.10 or 3.13 and install the supported direct-dependency set:

```bash
python -m pip install -c constraints.txt -e ".[dev]"
```

Before opening a pull request, run:

```bash
pytest -q
python -m ruff check .
python scripts/validate_cron_manifest.py
python -m compileall -q skills scripts tests
python scripts/check_maintainability_budget.py --base-ref origin/main
git diff --check
```

Add regression tests before changing unprotected behavior. Never commit
credentials, runtime state, holdings, signal ledgers, or private research
artifacts. Commit messages should follow the repository Lore trailer protocol.

GitHub Actions are pinned to immutable commit SHAs. Dependabot proposes action
and Python dependency updates; reviewers must verify the upstream release/tag,
run the full matrix, and update the explanatory version comment together with
the SHA.
