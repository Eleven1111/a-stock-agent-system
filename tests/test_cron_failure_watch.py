"""竞价链路失败汇总 watchdog — artifact 层可见性。

na 拓扑下没有任何消费者读作业退出码，watchdog 读 artifact 是唯一的失败可见性
通道，因此这里断言的是"文案怎么分类"，而不只是"有没有输出"。
"""

import runtime_context
from state_store import atomic_write_json

from scripts.cron_failure_watch import AUCTION_CHAIN_JOBS, main, render_text, scan


TRADING_DATE = "2026-08-05"


def _write_artifact(job_id, *, status="ok", trading_date=TRADING_DATE, run_id=None):
    run_id = run_id or f"{job_id}-0001"
    atomic_write_json(
        runtime_context.artifact_path(job_id, run_id),
        {
            "schema": runtime_context.ARTIFACT_SCHEMA,
            "job_id": job_id,
            "run_id": run_id,
            "trading_date": trading_date,
            "status": status,
            "finished_at": f"{trading_date}T09:26:11+08:00",
        },
    )


def _seed_green_chain(monkeypatch, tmp_path, *, skip=()):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    for job_id in AUCTION_CHAIN_JOBS:
        if job_id in skip:
            continue
        _write_artifact(job_id)


def test_green_chain_stays_silent(tmp_path, monkeypatch, capsys):
    _seed_green_chain(monkeypatch, tmp_path)

    report = scan(trading_date=TRADING_DATE)

    assert report["status"] == "ok"
    assert render_text(report) == ""
    assert main(["--trading-date", TRADING_DATE]) == 0
    assert capsys.readouterr().out == ""


def test_missing_artifact_reads_as_a_missed_trigger_not_a_failed_run(
    tmp_path, monkeypatch, capsys
):
    """Mac 睡眠错过 launchd 心跳在本机是常态，运维动作与"跑了但失败"完全不同。"""
    _seed_green_chain(monkeypatch, tmp_path, skip=("candidate-preopen",))

    report = scan(trading_date=TRADING_DATE)

    assert report["status"] == "alert"
    assert [item["job_id"] for item in report["missing"]] == ["candidate-preopen"]
    assert report["failed"] == []

    assert main(["--trading-date", TRADING_DATE]) == 0
    text = capsys.readouterr().out
    assert "candidate-preopen" in text
    assert "未触发" in text
    assert "执行失败" not in text


def test_failed_run_is_reported_apart_from_missing(tmp_path, monkeypatch, capsys):
    _seed_green_chain(monkeypatch, tmp_path, skip=("auction-finalize",))
    _write_artifact("auction-finalize", status="timeout")

    report = scan(trading_date=TRADING_DATE)

    assert report["status"] == "alert"
    assert report["missing"] == []
    assert [item["job_id"] for item in report["failed"]] == ["auction-finalize"]

    assert main(["--trading-date", TRADING_DATE]) == 0
    text = capsys.readouterr().out
    assert "执行失败" in text
    assert "timeout" in text
    assert "未触发" not in text


def test_yesterday_artifact_does_not_cover_today(tmp_path, monkeypatch):
    """同交易日语义：昨天跑成功不能替今天背书。"""
    _seed_green_chain(monkeypatch, tmp_path, skip=("auction-snapshot",))
    _write_artifact("auction-snapshot", trading_date="2026-08-04")

    report = scan(trading_date=TRADING_DATE)

    assert [item["job_id"] for item in report["missing"]] == ["auction-snapshot"]


def test_duplicate_skipped_is_not_a_failure(tmp_path, monkeypatch):
    """auction-snapshot 每分钟触发，末次 attempt 常是 duplicate_skipped，不得误报。"""
    _seed_green_chain(monkeypatch, tmp_path, skip=("auction-snapshot",))
    _write_artifact("auction-snapshot", status="duplicate_skipped")

    assert scan(trading_date=TRADING_DATE)["status"] == "ok"


def test_json_mode_reports_every_checked_job(tmp_path, monkeypatch, capsys):
    _seed_green_chain(monkeypatch, tmp_path)

    assert main(["--trading-date", TRADING_DATE, "--json"]) == 0

    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "a_stock_cron_failure_watch_v1"
    assert [item["job_id"] for item in payload["checked"]] == list(AUCTION_CHAIN_JOBS)
