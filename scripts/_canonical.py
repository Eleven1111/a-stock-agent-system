"""Thin wrappers for canonical skill script entrypoints."""

from __future__ import annotations

import os
import runpy
import sys


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def run(relative_path: str) -> None:
    target = os.path.join(ROOT, relative_path)
    sys.argv[0] = target
    runpy.run_path(target, run_name="__main__")
