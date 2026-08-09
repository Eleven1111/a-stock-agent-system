#!/usr/bin/env python3
"""Repository-root wrapper for the portfolio research input builder."""

import os


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

from portfolio_research_history import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
