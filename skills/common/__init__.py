# A股Agent系统共享模块
"""Shared modules for every in-repo skill.

The modules in this directory import each other by their flat names
(``import state_store``), and ~140 call sites across ``scripts/`` and the skill
directories each used to re-derive and insert this directory into ``sys.path``
before importing them.

This package owns that one path mutation now. Consumers write::

    import skills.common  # noqa: F401  -- puts skills/common on sys.path

instead of hand-rolling their own ``sys.path.insert``.

Why the flat names stay canonical rather than moving to ``skills.common.X``:
loading the same source file under two different module names produces two
distinct class objects, so ``except DataSourceError`` and ``isinstance`` stop
matching across that boundary. A partial migration is therefore strictly worse
than either end state — measured at 50 test failures when only this package's
internals were converted (2026-08-09). Moving the whole import graph at once is
a separate, much larger change; until then one canonical flat namespace is the
safe shape.
"""

from __future__ import annotations

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
