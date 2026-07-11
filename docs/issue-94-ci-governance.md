# Issue #94: CI and main-branch governance

This repository fails closed at the GitHub merge boundary.

## Required merge evidence

- `Python 3.10` and `Python 3.13` are required status checks. Each job runs
  installation, Ruff, pytest, cron-manifest validation, compileall and the
  maintainability budget.
- Required checks must be green against the latest main commit.
- Pull requests are mandatory and all review threads must be resolved, but an
  approving review is not mandatory for this single-maintainer repository.
  There are no ruleset bypass actors.
- CodeQL scans Python on pull requests, pushes to main, manual dispatch and a
  weekly schedule. Action dependencies are pinned to immutable commits.
- Dependabot security updates, vulnerability alerts, secret scanning and push
  protection remain enabled in repository settings.

The version-controlled desired ruleset is
`.github/rulesets/main.json`. Repository administrators must keep GitHub
ruleset `17245575` synchronized with it.

## Review policy

The repository currently has one collaborator. Mandatory independent approval
would make every owner-authored pull request impossible to merge. Review is
therefore optional, while PR-only changes, strict required checks, resolved
review threads and the no-bypass policy remain mandatory.

## Evidence

- First complete green main run after billing recovery:
  <https://github.com/Eleven1111/a-stock-agent-system/actions/runs/29155221845>
- Tracking issue: <https://github.com/Eleven1111/a-stock-agent-system/issues/94>
