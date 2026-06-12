#!/usr/bin/env python3
"""Runtime-neutral entrypoint used by Hermes, OpenClaw, CI, and local runs."""

from hermes_job_runner import main


if __name__ == "__main__":
    main()
