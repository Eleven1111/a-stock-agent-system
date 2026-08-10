"""Re-exec direct script entrypoints with the repository on PYTHONPATH."""

from __future__ import annotations

import os
import sys


def ensure_repo_importable(repo_root: str) -> None:
    """Restart once with *repo_root* on PYTHONPATH when it is not importable.

    Direct ``python scripts/x.py`` execution exposes only ``scripts/`` on the
    import path. Re-exec keeps setup at the process boundary instead of
    mutating ``sys.path`` independently in every business entrypoint.
    """
    root = os.path.abspath(repo_root)
    if root in sys.path:
        return
    env = dict(os.environ)
    existing = [item for item in env.get("PYTHONPATH", "").split(os.pathsep) if item]
    env["PYTHONPATH"] = os.pathsep.join([root, *[item for item in existing if item != root]])
    os.execve(sys.executable, [sys.executable, *sys.argv], env)

