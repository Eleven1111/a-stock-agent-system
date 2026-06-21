#!/usr/bin/env python3
"""Repository-root wrapper for the portfolio research input builder."""

import os
import sys


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
if COMMON not in sys.path:
    sys.path.insert(0, COMMON)

from portfolio_research_history import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
