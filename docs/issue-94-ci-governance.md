# Issue #94: CI and main-branch governance

This repository fails closed at the GitHub merge boundary.

## Required merge evidence

- `Python 3.10` and `Python 3.13` are required status checks. Each job runs
  installation, Ruff, pytest, cron-manifest validation, compileall and the
  maintainability budget.
- Required checks must be green on the latest main commit; branch updates
  invalidate stale approvals and require the last push to be approved by
  someone other than its author.
- At least one approving review and resolution of all review threads are
  required. There are no ruleset bypass actors.
- CodeQL scans Python on pull requests, pushes to main, manual dispatch and a
  weekly schedule. Action dependencies are pinned to immutable commits.
- Dependabot security updates, vulnerability alerts, secret scanning and push
  protection remain enabled in repository settings.

The version-controlled desired ruleset is
`.github/rulesets/main.json`. Repository administrators must keep GitHub
ruleset `17245575` synchronized with it.

## Operational limitation

The repository currently has one collaborator. A pull request authored by
that collaborator cannot satisfy its own approval requirement. Invite at least
one independent reviewer before attempting to merge owner-authored changes;
do not weaken the rule to work around that governance dependency.

## Evidence

- First complete green main run after billing recovery:
  <https://github.com/Eleven1111/a-stock-agent-system/actions/runs/29155221845>
- Tracking issue: <https://github.com/Eleven1111/a-stock-agent-system/issues/94>
