# Security Policy

## Supported version

Security fixes are applied to the current `main` branch and the latest tagged
release. Older snapshots are not maintained.

## Reporting a vulnerability

Do not open a public issue containing credentials, private portfolio data, or
an exploitable vulnerability. Use GitHub's private vulnerability reporting or
Security Advisory flow for this repository. Include the affected revision,
impact, a minimal reproduction, and any suggested mitigation.

This project is decision support only. A security report must never include
real brokerage credentials or unredacted production state.

## Secret response

If a credential may have entered Git history, revoke or rotate it first.
Deleting it from the current tree is insufficient; history cleanup is a
separate, coordinated operation that invalidates existing clones and commit
references.
