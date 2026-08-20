"""What survives a job timeout.

``subprocess.run(timeout=...)`` SIGKILLs the direct child and nothing else, so
anything the job spawned keeps running. `market-history-cache` and
`snapshot-gc` were being SIGKILLed on *every* run (issue #245), and nobody had
ever checked what they left behind.

These tests drive real process trees. A mock cannot tell you whether a pid is
still alive, which is the only question being asked here.
"""

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from scripts import hermes_job_runner as runner


pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="process-group cleanup is a POSIX mechanism"
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_gone(pid: int, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


SPAWNS_A_GRANDCHILD = textwrap.dedent(
    """
    import subprocess, sys, time
    grandchild = subprocess.Popen([
        sys.executable, "-c",
        "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(120)",
        sys.argv[1],
    ])
    sys.stdout.write("spawned\\n")
    sys.stdout.flush()
    time.sleep(120)
    """
)


def _grandchild_pid(pidfile, *, timeout=5.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return int(pidfile.read_text())
        except (OSError, ValueError):
            time.sleep(0.05)
    raise AssertionError("grandchild never reported its pid")


class TestTimeoutKillsTheWholeTree:
    def test_a_grandchild_does_not_outlive_the_timed_out_job(self, tmp_path):
        pidfile = tmp_path / "grandchild.pid"
        started = time.monotonic()

        result = runner.run_isolated(
            [sys.executable, "-c", SPAWNS_A_GRANDCHILD, str(pidfile)],
            cwd=str(tmp_path), env=dict(os.environ), timeout=2,
        )
        elapsed = time.monotonic() - started

        assert result.timed_out is True
        # Signalling only the direct child leaves the grandchild holding the
        # output pipes, so the drain blocks until the orphan exits on its own —
        # the job "returns" two minutes after its 2s timeout. Bounding the wall
        # clock is what distinguishes killing the tree from killing the child.
        assert elapsed < 2 + runner.TERM_GRACE_SECONDS + 5, (
            f"run_isolated took {elapsed:.1f}s for a 2s timeout — it stalled on "
            "an orphan still holding the pipe"
        )
        pid = _grandchild_pid(pidfile)
        assert _wait_gone(pid), (
            f"grandchild {pid} outlived the job it belonged to — this is the leak "
            "market-history-cache and snapshot-gc produced on every run"
        )

    def test_the_timeout_contract_the_runner_depends_on_is_unchanged(self, tmp_path):
        pidfile = tmp_path / "grandchild.pid"

        result = runner.run_isolated(
            [sys.executable, "-c", SPAWNS_A_GRANDCHILD, str(pidfile)],
            cwd=str(tmp_path), env=dict(os.environ), timeout=2,
        )

        assert result.returncode == 124
        assert "spawned" in result.stdout, "partial output must survive the kill"
        assert isinstance(result.stdout, str) and isinstance(result.stderr, str)
        _wait_gone(_grandchild_pid(pidfile))


class TestGracefulFirst:
    def test_a_job_that_handles_sigterm_gets_to_shut_itself_down(self, tmp_path):
        """SIGKILL mid-write is how orphaned .lock files are made.

        The job is asked to stop before it is killed, so anything holding a
        database handle can close it.
        """
        marker = tmp_path / "clean_exit"
        script = textwrap.dedent(
            f"""
            import signal, sys, time
            def _bye(signum, frame):
                open({str(marker)!r}, "w").write("closed cleanly")
                sys.exit(0)
            signal.signal(signal.SIGTERM, _bye)
            sys.stdout.write("ready\\n"); sys.stdout.flush()
            time.sleep(120)
            """
        )

        result = runner.run_isolated(
            [sys.executable, "-c", script],
            cwd=str(tmp_path), env=dict(os.environ), timeout=2,
        )

        assert result.timed_out is True
        assert marker.read_text() == "closed cleanly"

    def test_a_job_that_ignores_sigterm_is_still_killed(self, tmp_path):
        script = textwrap.dedent(
            """
            import signal, sys, time
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            sys.stdout.write("ready\\n"); sys.stdout.flush()
            time.sleep(120)
            """
        )
        started = time.monotonic()

        result = runner.run_isolated(
            [sys.executable, "-c", script],
            cwd=str(tmp_path), env=dict(os.environ), timeout=2,
        )
        elapsed = time.monotonic() - started

        assert result.timed_out is True
        assert elapsed < 2 + runner.TERM_GRACE_SECONDS + 5


class TestHappyPathUnchanged:
    def test_a_job_that_finishes_returns_its_own_result(self, tmp_path):
        result = runner.run_isolated(
            [sys.executable, "-c", "import sys; print('done'); sys.exit(3)"],
            cwd=str(tmp_path), env=dict(os.environ), timeout=30,
        )

        assert result.timed_out is False
        assert result.returncode == 3
        assert result.stdout.strip() == "done"

    def test_a_job_that_exits_during_the_grace_window_does_not_crash_the_runner(self, tmp_path):
        """The process group can vanish between the timeout and the kill."""
        result = runner.run_isolated(
            [sys.executable, "-c", "import time; time.sleep(2.05)"],
            cwd=str(tmp_path), env=dict(os.environ), timeout=2,
        )

        assert result.timed_out is True
        assert result.returncode == 124


def test_the_job_runs_in_its_own_process_group(tmp_path):
    """The group is what makes the whole tree addressable at kill time."""
    result = runner.run_isolated(
        [sys.executable, "-c", "import os; print(os.getpid() == os.getpgid(0))"],
        cwd=str(tmp_path), env=dict(os.environ), timeout=30,
    )

    assert result.stdout.strip() == "True"


def test_baseline_subprocess_run_really_does_leak(tmp_path):
    """Pins the reason run_isolated exists.

    If a future Python cleans the tree up on its own this fails, and the extra
    machinery can go. Until then, this is the behaviour being worked around.
    """
    pidfile = tmp_path / "grandchild.pid"
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(
            [sys.executable, "-c", SPAWNS_A_GRANDCHILD, str(pidfile)],
            cwd=str(tmp_path), capture_output=True, text=True, timeout=2,
        )

    pid = _grandchild_pid(pidfile)
    leaked = _alive(pid)
    if leaked:
        os.killpg(os.getpgid(pid), signal.SIGKILL) if os.getpgid(pid) != os.getpgid(0) else os.kill(pid, signal.SIGKILL)
    assert leaked, "stdlib no longer leaks descendants; run_isolated may be redundant"
