"""cron 调度分发器 — 表达式匹配 / 到期作业筛选 / 同分钟去重。

launchd 每 60 秒唤醒一次 dispatcher，因此正确性取决于两件事：
表达式匹配必须精确到分钟，且同一分钟内重复唤醒不得重复触发。
"""

import json

import pytest

from scripts import cron_dispatch as cd


def _dt(s):
    from datetime import datetime
    return datetime.fromisoformat(s)


def test_parse_field_supports_star_number_range_list_step():
    assert cd.parse_field("*", 0, 6) == set(range(7))
    assert cd.parse_field("3", 0, 6) == {3}
    assert cd.parse_field("1-4", 0, 6) == {1, 2, 3, 4}
    assert cd.parse_field("1,3,5", 0, 6) == {1, 3, 5}
    assert cd.parse_field("*/15", 0, 59) == {0, 15, 30, 45}
    assert cd.parse_field("15-23", 0, 59) == set(range(15, 24))


def test_parse_field_rejects_out_of_range_and_garbage():
    for bad in ("99", "-1", "1-99", "abc", "", "*/0"):
        with pytest.raises(ValueError):
            cd.parse_field(bad, 0, 59)


def test_cron_matches_real_manifest_schedules():
    # 全市场动态候选发现：每工作日 15:07
    assert cd.cron_matches("7 15 * * 1-5", _dt("2026-07-20T15:07:00"))
    assert not cd.cron_matches("7 15 * * 1-5", _dt("2026-07-20T15:08:00"))
    assert not cd.cron_matches("7 15 * * 1-5", _dt("2026-07-19T15:07:00"))  # 周日
    # 集合竞价快照：工作日 09:15-09:23 每分钟
    for minute in range(15, 24):
        assert cd.cron_matches("15-23 9 * * 1-5", _dt(f"2026-07-20T09:{minute}:00"))
    assert not cd.cron_matches("15-23 9 * * 1-5", _dt("2026-07-20T09:24:00"))
    # 盘中催化：工作日 9-11,13-14 点的 3 分与 33 分
    assert cd.cron_matches("3,33 9-11,13-14 * * 1-5", _dt("2026-07-20T13:33:00"))
    assert not cd.cron_matches("3,33 9-11,13-14 * * 1-5", _dt("2026-07-20T12:33:00"))


def test_cron_matches_treats_sunday_as_both_0_and_7():
    assert cd.cron_matches("0 9 * * 0", _dt("2026-07-19T09:00:00"))
    assert cd.cron_matches("0 9 * * 7", _dt("2026-07-19T09:00:00"))


def test_due_jobs_skips_disabled_and_non_matching():
    manifest = {"jobs": [
        {"id": "a", "schedule": "7 15 * * 1-5", "enabled": True},
        {"id": "b", "schedule": "7 15 * * 1-5", "enabled": False},
        {"id": "c", "schedule": "8 15 * * 1-5", "enabled": True},
    ]}
    assert [j["id"] for j in cd.due_jobs(manifest, _dt("2026-07-20T15:07:00"))] == ["a"]


def test_due_jobs_skips_malformed_schedule_without_killing_the_batch():
    """一个作业的表达式写坏，不能连累同一分钟其他作业。"""
    manifest = {"jobs": [
        {"id": "bad", "schedule": "not a cron", "enabled": True},
        {"id": "good", "schedule": "7 15 * * 1-5", "enabled": True},
    ]}
    assert [j["id"] for j in cd.due_jobs(manifest, _dt("2026-07-20T15:07:00"))] == ["good"]


def test_same_minute_is_claimed_only_once(tmp_path):
    """launchd 每 60s 唤醒，时钟抖动可能让同一分钟被唤醒两次。"""
    state = tmp_path / "dispatch_state.json"
    assert cd.claim(str(state), "candidate-discovery", _dt("2026-07-20T15:07:00")) is True
    assert cd.claim(str(state), "candidate-discovery", _dt("2026-07-20T15:07:41")) is False
    # 下一分钟是新的触发点
    assert cd.claim(str(state), "candidate-discovery", _dt("2026-07-20T15:08:00")) is True
    # 不同作业互不影响
    assert cd.claim(str(state), "other-job", _dt("2026-07-20T15:08:00")) is True


def test_claim_survives_corrupt_state_file(tmp_path):
    """状态文件损坏时必须继续调度，而不是让所有作业静默停摆。"""
    state = tmp_path / "dispatch_state.json"
    state.write_text("{ not json", encoding="utf-8")
    assert cd.claim(str(state), "job", _dt("2026-07-20T15:07:00")) is True
    assert json.loads(state.read_text(encoding="utf-8"))["job"] == "2026-07-20T15:07"


def test_claim_prunes_stale_entries(tmp_path):
    """状态文件不能无限增长。"""
    state = tmp_path / "dispatch_state.json"
    payload = {f"old-{i}": "2026-01-01T00:00" for i in range(50)}
    state.write_text(json.dumps(payload), encoding="utf-8")
    cd.claim(str(state), "fresh", _dt("2026-07-20T15:07:00"))
    kept = json.loads(state.read_text(encoding="utf-8"))
    assert "fresh" in kept
    assert len(kept) < 50


def test_job_env_puts_repo_venv_first_on_path():
    """manifest 命令用裸 python；/bin/sh 的极简 PATH 解析不到会 command not found。"""
    import os

    env = cd.job_env()
    parts = env["PATH"].split(os.pathsep)
    assert parts[0] == os.path.join(cd.ROOT, ".venv", "bin")
    # 原有 PATH 必须保留在后面：venv 里没有的工具（git 等）仍要能找到
    assert parts[1:], "原有 PATH 不能被丢弃"
    assert env["PYTHONPATH"] == cd.COMMON


def test_launched_job_resolves_bare_python(tmp_path):
    """端到端：裸 python 命令必须能在派生环境里跑起来。"""
    import time

    log = tmp_path / "jobs.log"
    pid = cd.launch(
        {"id": "probe", "command": "python -c \"print('PY_OK')\"", "cwd": "."},
        log_path=str(log),
    )
    assert pid is not None
    for _ in range(50):
        if log.exists() and "PY_OK" in log.read_text(encoding="utf-8"):
            return
        time.sleep(0.1)
    raise AssertionError(f"bare python did not resolve: {log.read_text(encoding='utf-8') if log.exists() else '<no log>'}")
