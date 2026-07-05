"""Shared resolution of cron manifest job roles.

A manifest job may declare a ``role`` that disambiguates scheduler and DAG
behaviour:

- ``scheduled``: generated into the scheduler and usable as a DAG dependency.
- ``dependency_only``: not scheduled, but still runs as a DAG dependency or an
  explicit CLI target.
- ``off``: not scheduled and refused as a dependency/target (fail-closed).

When ``role`` is absent it is derived from ``enabled`` so legacy manifests keep
their current behaviour (``enabled: true`` -> scheduled, ``enabled: false`` ->
dependency_only).
"""

from __future__ import annotations

from typing import Any, Mapping

VALID_ROLES = ("scheduled", "dependency_only", "off")


def resolve_job_role(job: Mapping[str, Any]) -> str:
    """Return the effective role for ``job``, deriving from ``enabled``."""
    role = job.get("role")
    if role in VALID_ROLES:
        return role
    return "scheduled" if job.get("enabled", True) else "dependency_only"


def is_scheduled(job: Mapping[str, Any]) -> bool:
    """True when the scheduler should generate an entry for ``job``."""
    return resolve_job_role(job) == "scheduled"
