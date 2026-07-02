# Private Content Removal Report - 2026-07-02

Scope: public repository `Eleven1111/a-stock-agent-system`.

This report records the tracked private/runtime files removed from the public
index in branch `chore/private-content-removal`. It intentionally does not
repeat account details, holdings, secrets, or private report body text.

## Files Removed From The Git Index

The following files were removed with `git rm --cached` and remain available
locally where applicable:

| Path group | Count | Note |
| --- | ---: | --- |
| `reports/system-issues-2026-06-23.md` | 1 | Moved locally to ignored `docs_private/system-issues-2026-06-23.md`. |
| `agent_state/` | 3 | Runtime state and lock files. |
| `state_identity.json*` | 3 | Local state identity files and lock/backup files. |
| `market/snapshots/` | 976 | Runtime market snapshot cache. |
| `cron/output/` | 869 | Runtime cron artifacts and locks. |
| **Total** | **1852** | Full list is reproducible with `git diff --cached --name-only --diff-filter=D`. |

Ignored going forward:

```gitignore
docs_private/
reports/
agent_state/
state_identity.json*
market/snapshots/
cron/output/
```

## Sensitive Scan Summary

Commands were run only against files still tracked by git after the index
removal.

| Scan | Result |
| --- | --- |
| Actual cash amount patterns (`2万`, `20000`, `portfolio_size`) | No `2万` remains in tracked files. `20000` and `portfolio_size` remain only in code/test constants and generic config-migration tests; the public docs cash example was replaced with placeholders. |
| Real holdings code plus cost/price patterns | Remaining hits are synthetic examples, test fixtures, reference snippets, or provider examples. No runtime state file or private report remains tracked. |
| API key patterns (`sk-`, cloud key prefixes, concrete key assignments) | No concrete key value found. Remaining matches are variable/function names such as `api_key = _next_serper_key()`. |
| `serpapi` / `serper` | Remaining hits are provider names, environment variable names, or docs describing optional provider configuration; no key value found. |
| Personal path `/Users/na/` | No remaining tracked hits after sanitizing `docs/architecture-review-2026-06-22.md`. |

## Docs Directory Review

Seven tracked files under `docs/` were checked individually:

| File | Result |
| --- | --- |
| `docs/architecture-hardening.md` | Generic architecture terms only; no live holdings, account amount, secret, or personal path. |
| `docs/architecture-review-2026-06-22.md` | Personal local paths were removed. Remaining matches are generic cost/research terms. |
| `docs/eastmoney-resilience.md` | No sensitive scan hits. |
| `docs/hot-money-selection-protocol.md` | No sensitive scan hits. |
| `docs/portfolio-research-protocol.md` | Cash reconciliation example was changed to placeholders. Remaining numeric hits are dates. |
| `docs/stock-intelligence-integration.md` | No account, holdings, secret, or personal path; contains only a public commit hash reference. |
| `docs/trading-lifecycle.md` | Generic lifecycle terms and public stock-code examples only; no live holdings, account amount, secret, or personal path. |

## Sensitive History Still Present

`git log --all --oneline -- reports/`:

```text
903d8e2 fix: yfinance 三连修 — NO_PROXY 限流 + 单ticker MultiIndex + 跨run禁用
```

The introduction commit for `reports/system-issues-2026-06-23.md` is:

```text
903d8e2 2026-06-24 fix: yfinance 三连修 — NO_PROXY 限流 + 单ticker MultiIndex + 跨run禁用
```

This branch removes the file from the current tree only. The sensitive content
still exists in git history until a history rewrite is performed and force
pushed.

## Recommended History Cleaning Plan

Do not run this in the normal working tree without coordination. Recommended
sequence is to use a fresh mirror clone, rewrite history, rotate any exposed
secrets if found, and force-push after notifying collaborators.

```bash
git clone --mirror git@github.com:Eleven1111/a-stock-agent-system.git
cd a-stock-agent-system.git

git filter-repo --force \
  --path reports/system-issues-2026-06-23.md \
  --path agent_state/ \
  --path state_identity.json \
  --path state_identity.json.bak \
  --path state_identity.json.lock \
  --path market/snapshots/ \
  --path cron/output/ \
  --invert-paths

git push --force --all
git push --force --tags
```

Not executed in this branch per task constraint.

## Verification

```text
pytest -q
755 passed in 10.62s
```

```text
python scripts/validate_cron_manifest.py
OK: 39 jobs (0 local, 39 external)
```

```text
python -m ruff check .
/opt/homebrew/opt/python@3.12/bin/python3.12: No module named ruff
```

```text
ruff check .
zsh:1: command not found: ruff
```

```text
git diff --check
<no output>
```
