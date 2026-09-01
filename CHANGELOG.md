# Changelog

All notable changes are documented here. The project follows semantic
versioning for repository contracts; a release tag is created only after the
published CI and release gates pass.

## 1.4.0 - Unreleased

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
