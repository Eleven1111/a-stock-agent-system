from datetime import datetime, timezone

import adaptive_schedule


NOW = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)


def test_first_tick_is_always_due(tmp_path):
    path = tmp_path / "adaptive_schedule.json"

    decision = adaptive_schedule.should_run("news-monitor", now=NOW, path=path)

    assert decision["run"] is True
    assert decision["would_skip"] is False
    assert decision["miss_streak"] == 0
    assert decision["interval"] == 1


def test_backoff_skips_ticks_until_interval_reached(tmp_path):
    path = tmp_path / "adaptive_schedule.json"
    # Build up a streak of 3 misses so interval becomes 2.
    for _ in range(3):
        adaptive_schedule.should_run("official-policy-watch", now=NOW, path=path)
        adaptive_schedule.record_outcome(
            "official-policy-watch", ran=True, has_signal=False, path=path
        )

    tick_a = adaptive_schedule.should_run("official-policy-watch", now=NOW, path=path)
    assert tick_a["interval"] == 2
    assert tick_a["ticks_since_run"] == 1
    assert tick_a["run"] is False
    assert tick_a["would_skip"] is True

    # Simulate enforce mode: no run happened, streak untouched.
    adaptive_schedule.record_outcome(
        "official-policy-watch", ran=False, has_signal=None, path=path
    )

    tick_b = adaptive_schedule.should_run("official-policy-watch", now=NOW, path=path)
    assert tick_b["ticks_since_run"] == 2
    assert tick_b["run"] is True


def test_a_real_signal_immediately_resets_backoff_to_full_frequency(tmp_path):
    path = tmp_path / "adaptive_schedule.json"
    for _ in range(21):
        adaptive_schedule.should_run("official-policy-watch", now=NOW, path=path)
        adaptive_schedule.record_outcome(
            "official-policy-watch", ran=True, has_signal=False, path=path
        )

    backed_off = adaptive_schedule.should_run("official-policy-watch", now=NOW, path=path)
    assert backed_off["interval"] == 8

    adaptive_schedule.record_outcome(
        "official-policy-watch", ran=True, has_signal=True, path=path
    )

    reset_tick = adaptive_schedule.should_run("official-policy-watch", now=NOW, path=path)
    assert reset_tick["miss_streak"] == 0
    assert reset_tick["interval"] == 1
    assert reset_tick["run"] is True


def test_skipped_ticks_do_not_extend_the_miss_streak(tmp_path):
    path = tmp_path / "adaptive_schedule.json"
    adaptive_schedule.should_run("news-monitor", now=NOW, path=path)
    adaptive_schedule.record_outcome("news-monitor", ran=True, has_signal=False, path=path)

    before = adaptive_schedule.should_run("news-monitor", now=NOW, path=path)
    adaptive_schedule.record_outcome("news-monitor", ran=False, has_signal=None, path=path)
    after = adaptive_schedule.should_run("news-monitor", now=NOW, path=path)

    assert before["miss_streak"] == after["miss_streak"] == 1


def test_jobs_are_tracked_independently(tmp_path):
    path = tmp_path / "adaptive_schedule.json"
    for _ in range(5):
        adaptive_schedule.should_run("news-monitor", now=NOW, path=path)
        adaptive_schedule.record_outcome("news-monitor", ran=True, has_signal=False, path=path)

    other = adaptive_schedule.should_run("news-monitor-intraday", now=NOW, path=path)

    assert other["miss_streak"] == 0
    assert other["interval"] == 1


def test_record_outcome_ignores_unresolved_has_signal(tmp_path):
    path = tmp_path / "adaptive_schedule.json"
    adaptive_schedule.should_run("news-monitor", now=NOW, path=path)

    adaptive_schedule.record_outcome("news-monitor", ran=True, has_signal=None, path=path)

    decision = adaptive_schedule.should_run("news-monitor", now=NOW, path=path)
    assert decision["miss_streak"] == 0
    assert decision["ticks_since_run"] == 2
