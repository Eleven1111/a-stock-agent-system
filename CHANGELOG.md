# Changelog

All notable changes are documented here. The project follows semantic
versioning for repository contracts; a release tag is created only after the
published CI and release gates pass.

## 1.4.0 - Unreleased

### Fixed

- Added the manual-only `research_gate.py start-shadow` and `promote` CLI
  entry points for adjacent strategy-registry promotions. Each invocation
  requires an actor, reason, signature, and timezone-aware timestamp; the
  existing evidence, OOS, shadow, broker-reconciliation, origin-ceiling, and
  bounded-weight gates remain fail-closed. No automatic promotion or
  paper-trading substitution for broker reconciliation was added.

- Wired the MFI overheat auction gate to the field names `auction_collector`
  actually writes (`book_coverage_status`, `auction_book_status`). It previously
  required `book_quality == "ok"`, a key no producer sets, so every overheated
  candidate was rejected regardless of book quality and the `stale_last_good`
  check never fired. Added a contract test that feeds the collector's own
  quality report into the gate.

### Changed

- Evolved the MFI overheat gate to `mfi-overheat-gate-v2`: one bounded nonlinear
  10–25 point lane risk charge, trusted stable auction book and amount gating,
  plus fail-closed open deterioration controls.
- Hardened point-in-time evidence, execution, settlement, portfolio-risk, and
  research-agent boundaries to fail closed.
- Added reproducible validation, shadow-promotion, statistical, cost/capacity,
  and public-governance controls.
- Added Python 3.10 and 3.13 CI with constrained direct dependencies.

### Migration

- Deployments must share `A_STOCK_STATE_HOME` across Hermes and OpenClaw.
- Research strategies remain non-live until the versioned OOS, shadow, human
  approval, and broker-reconciliation gates are satisfied.
- Existing state is read compatibly; operators should run the state doctor and
  reconciliation checks before enabling new-risk decisions.
