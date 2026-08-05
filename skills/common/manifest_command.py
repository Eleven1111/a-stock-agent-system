"""Typed command resolution for manifest jobs.

Scheduled jobs used to be shell strings. That gave the dispatcher an execution
surface nobody had bounded: quoting bugs became silent argument corruption, and
``shell=True`` meant a pipe, a redirect or a ``$(...)`` in the manifest would
have been executed rather than rejected.

Enabled jobs now carry ``command_argv`` (the scheduler entry) and ``run.argv``
(the business process). Both are string arrays executed with ``shell=False``.
The legacy string form survives on disabled jobs only, for one migration
release, and is never auto-promoted back into a shell execution path.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence


#: Characters that only mean something to a shell. In an argv element they are
#: either dead weight or a sign the author still thinks a shell will run this.
SHELL_METACHARACTERS = ("|", ">", "<", "$", "`", ";", "&", "\n", "*", "?")

PLACEHOLDER_OPEN = "{"
PLACEHOLDER_CLOSE = "}"

#: Environment keys the runner owns. ``run.env`` exists so a job can carry its
#: own business feature flags (they must travel with the manifest, not with one
#: machine's .env). It is deliberately not a general environment override: these
#: keys decide where state lives, which identity the run claims, and which
#: binary executes — the fail-closed contract that state_integrity, the run
#: lease and artifact routing are all keyed on. A manifest value for them is
#: ignored at runtime and rejected at validation time.
RESERVED_ENV_KEYS = frozenset({
    "PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "HOME",
    "HERMES_HOME",
    "HERMES_JOB_ID",
    "HERMES_RUN_ID",
    "HERMES_BATCH_ID",
    "HERMES_TRADING_DATE",
    "HERMES_CONTEXT_SCOPE",
    "HERMES_CONTEXT_FROM",
    "A_STOCK_STATE_HOME",
    "A_STOCK_STATE_ID",
    "A_STOCK_ENV_FILE",
    "A_STOCK_BACKUP_HOME",
    "A_STOCK_RUNTIME",
    "A_STOCK_JOB_ID",
    "A_STOCK_RUN_ID",
    "A_STOCK_BATCH_ID",
    "A_STOCK_TRADING_DATE",
    "A_STOCK_AGENT_STATE_PATH",
    "A_STOCK_TRACE_ID",
})

#: Conventional POSIX-ish environment name. Anything else is a typo or an
#: attempt to smuggle something past the reserved-key check.
ENV_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*")


class CommandContractError(ValueError):
    """Raised when a manifest command cannot be executed safely."""


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


def has_typed_outer(job: Mapping[str, Any]) -> bool:
    return isinstance(job.get("command_argv"), list)


def has_typed_run(job: Mapping[str, Any]) -> bool:
    return isinstance((job.get("run") or {}).get("argv"), list)


def outer_argv(job: Mapping[str, Any]) -> list[str]:
    """Scheduler entry command as argv.

    Enabled jobs must supply ``command_argv``. The string ``command`` is only
    read for disabled jobs so the migration can be inspected and diffed.
    """
    argv = job.get("command_argv")
    if isinstance(argv, list):
        return [str(item) for item in argv]
    if job.get("enabled"):
        raise CommandContractError(
            f"job {job.get('id')!r} is enabled and must define command_argv"
        )
    return shlex.split(str(job.get("command") or ""))


def business_argv(job: Mapping[str, Any]) -> list[str]:
    """Isolated business process command as argv."""
    run = job.get("run") or {}
    argv = run.get("argv")
    if isinstance(argv, list):
        return [str(item) for item in argv]
    if job.get("enabled"):
        raise CommandContractError(
            f"job {job.get('id')!r} is enabled and must define run.argv"
        )
    command = str(run.get("command") or "")
    if not command.strip():
        raise CommandContractError(f"job {job.get('id')!r} has no run.argv or run.command")
    return shlex.split(command)


def display_command(argv: Sequence[str]) -> str:
    """Human-readable, shell-quoted rendering. Never fed back to a shell."""
    return " ".join(shlex.quote(str(item)) for item in argv)


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def argv_errors(argv: Any, *, label: str) -> list[str]:
    """Structural problems with an argv array, as human-readable strings."""
    errors: list[str] = []
    if not isinstance(argv, list) or not argv:
        return [f"{label} must be a non-empty array of strings"]
    for index, item in enumerate(argv):
        if not isinstance(item, str):
            errors.append(f"{label}[{index}] must be a string")
            continue
        if not item.strip() and index == 0:
            errors.append(f"{label}[0] must be a non-empty executable")
        for token in SHELL_METACHARACTERS:
            if token in item:
                errors.append(
                    f"{label}[{index}] contains shell metacharacter {token!r}: {item!r}"
                )
                break
    return errors


def env_errors(run: Any, *, label: str = "run.env") -> list[str]:
    """Structural problems with a job's ``run.env`` block."""
    raw = (run or {}).get("env") if isinstance(run, Mapping) else None
    if raw is None:
        return []
    if not isinstance(raw, Mapping):
        return [f"{label} must be an object of NAME=value strings"]
    errors: list[str] = []
    for key, value in raw.items():
        name = str(key)
        if not ENV_KEY_RE.fullmatch(name):
            errors.append(f"{label} key must be UPPER_SNAKE_CASE: {name!r}")
            continue
        if name in RESERVED_ENV_KEYS:
            errors.append(f"{label} must not override runner-owned key: {name}")
            continue
        if not isinstance(value, (str, int, float, bool)):
            errors.append(f"{label}[{name}] must be a scalar value")
    return errors


def env_overrides(run: Any) -> Dict[str, str]:
    """Per-job environment from the manifest, minus anything runner-owned.

    Filtering rather than raising keeps a bad manifest from turning into a job
    that never runs: the runner-owned value is the correct one anyway, and
    ``validate_cron_manifest.py`` fails the same input at gate time.
    """
    raw = (run or {}).get("env") if isinstance(run, Mapping) else None
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if ENV_KEY_RE.fullmatch(str(key)) and str(key) not in RESERVED_ENV_KEYS
    }


def undeclared_placeholders(argv: Sequence[str], declared: Iterable[str] = ()) -> list[str]:
    """Template variables in argv that the job has not explicitly declared."""
    allowed = set(declared)
    found: list[str] = []
    for item in argv:
        text = str(item)
        start = text.find(PLACEHOLDER_OPEN)
        while start != -1:
            end = text.find(PLACEHOLDER_CLOSE, start)
            if end == -1:
                break
            name = text[start + 1:end]
            if name and name not in allowed and name not in found:
                found.append(name)
            start = text.find(PLACEHOLDER_OPEN, end)
    return found


# --------------------------------------------------------------------------
# substitution and execution
# --------------------------------------------------------------------------


def substitute_argv(
    argv: Sequence[str],
    variables: Mapping[str, str],
    *,
    allow_fragments: Iterable[str] = (),
) -> list[str]:
    """Replace whole argv values, or declared fragments, with variable values.

    Whole-value substitution (``"{code}"`` → ``"600519"``) can never change the
    argument count, so it cannot smuggle an extra flag into the command. A
    fragment substitution can, so it is refused unless the key was declared.
    """
    fragments = set(allow_fragments)
    result: list[str] = []
    for item in argv:
        text = str(item)
        if len(text) > 2 and text.startswith(PLACEHOLDER_OPEN) and text.endswith(PLACEHOLDER_CLOSE):
            key = text[1:-1]
            if key in variables:
                result.append(str(variables[key]))
                continue
        for key, value in variables.items():
            token = f"{PLACEHOLDER_OPEN}{key}{PLACEHOLDER_CLOSE}"
            if token in text:
                if key not in fragments:
                    raise CommandContractError(
                        f"variable {key!r} may only replace a whole argument, not a fragment of {text!r}"
                    )
                text = text.replace(token, str(value))
        result.append(text)
    return result


def resolve_executable(
    argv: Sequence[str],
    *,
    env: Optional[Mapping[str, str]] = None,
    python: Optional[str] = None,
) -> list[str]:
    """Resolve argv[0] to a concrete path, failing closed when it is missing.

    A bare ``python`` in the manifest used to depend on whichever PATH the
    scheduler happened to inherit. Resolving it here means a missing interpreter
    is a clear error at launch instead of a mystery ``command not found`` in a
    job log.
    """
    if not argv:
        raise CommandContractError("empty argv")
    values = dict(env if env is not None else os.environ)
    head = str(argv[0])
    rest = [str(item) for item in argv[1:]]

    if head in {"python", "python3"} and python:
        return [python, *rest]
    if os.path.sep in head:
        candidate = os.path.abspath(head)
        if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
            raise CommandContractError(f"executable not found or not executable: {head}")
        return [candidate, *rest]
    resolved = shutil.which(head, path=values.get("PATH"))
    if not resolved:
        raise CommandContractError(f"executable not found on PATH: {head}")
    return [resolved, *rest]


def resolve_cwd(root: str, raw: Any) -> str:
    """Resolve a job cwd inside the repository, failing closed on escape."""
    root_abs = os.path.abspath(root)
    candidate = os.path.abspath(os.path.join(root_abs, str(raw or ".")))
    if candidate != root_abs and not candidate.startswith(root_abs + os.sep):
        raise CommandContractError(f"job cwd escapes the repository: {raw!r}")
    if not os.path.isdir(candidate):
        raise CommandContractError(f"job cwd does not exist: {candidate}")
    return candidate
