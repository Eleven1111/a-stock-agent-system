"""Cron Manifest 校验测试"""

import json
import os
import tempfile
from scripts.validate_cron_manifest import (
    TIMEOUT_RATIONALE_TIERS,
    TIMEOUT_TIERS,
    validate,
)


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MANIFEST_PATH = os.path.join(ROOT, "cron", "hermes-cron-manifest.json")


def _entry_command(job):
    """Render command_argv back to a readable string for assertions."""
    return " ".join(job.get("command_argv") or [])


def _run_command(job):
    return " ".join((job.get("run") or {}).get("argv") or [])

VALID_JOB = {
    "id": "test-job",
    "name": "Test",
    "schedule": "0 9 * * 1-5",
    "timezone": "Asia/Shanghai",
    "command_argv": ["python", "scripts/run_agent_dag.py", "test-job", "--emit-target"],
    "cwd": ".",
    "enabled": True,
    "silent_when_no_signal": True,
    "expected_output": "text",
    "external": True,
    "execution_mode": "isolated_subprocess",
    "context_scope": "cron",
    "deliver": "origin",
    "max_output_chars": 2000,
    "context_from": [],
    "artifact_path_template": "{cron_output_dir}/{job_id}/{run_id}.json",
    "allowed_state_writes": ["$A_STOCK_STATE_HOME/cron/output/test-job/"],
    "run": {
        "argv": ["python", "skills/stock-triage/scripts/context_digest.py", "--json"],
        "cwd": ".",
        "timeout_seconds": 10,
        "timeout_tier": "short",
    },
}


def _validate_job(job):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as handle:
        json.dump({"jobs": [job]}, handle)
        path = handle.name
    try:
        return validate(path)
    finally:
        os.unlink(path)


def test_valid_manifest():
    manifest = {"jobs": [VALID_JOB]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is True
    finally:
        os.unlink(path)


def _manifest_job(job_id):
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        manifest = json.load(f)
    return next(job for job in manifest["jobs"] if job["id"] == job_id)


def test_same_window_auction_and_open_pushes_are_merged():
    auction_finalize = _manifest_job("auction-finalize")
    auction_brief = _manifest_job("auction-intelligence-brief")
    open_confirmation = _manifest_job("open-confirmation")
    open_brief = _manifest_job("open-intelligence-brief")

    assert auction_finalize["deliver"] == "local"
    assert auction_brief["deliver"] == "origin"
    assert open_confirmation["deliver"] == "local"
    assert open_brief["deliver"] == "origin"


def test_strategy_shadow_daily_is_isolated_and_after_close():
    job = _manifest_job("strategy-shadow-daily")
    assert job["schedule"] == "40 23 * * 1-5"
    assert job["enabled"] is True
    assert job["deliver"] == "local"
    assert job["context_from"] == [
        "strategy-evidence-daily", "preleader-pretable-build", "cycle-state-shadow"
    ]
    assert _entry_command(job) == (
        "python scripts/run_agent_dag.py strategy-shadow-daily --emit-target"
    )
    assert _run_command(job) == "python scripts/strategy_shadow_runner.py --json"
    writes = " ".join(job["allowed_state_writes"])
    assert "strategy_shadow" in writes


def test_strategy_forward_settlement_runs_after_shadow_and_cannot_write_live_state():
    job = _manifest_job("strategy-forward-settlement-daily")
    assert job["schedule"] == "50 23 * * 1-5"
    assert job["context_from"] == ["strategy-shadow-daily", "market-history-cache"]
    assert _run_command(job) == "python scripts/strategy_forward_settlement.py --json"
    writes = " ".join(job["allowed_state_writes"])
    assert "strategy_forward" in writes
    assert "signal_ledger" not in writes
    assert "strategy_registry" not in writes
    assert "portfolio" not in writes
    assert job["external_note"].find("next-session open") >= 0
    assert "portfolio" not in writes
    assert "signal_ledger" not in writes


def test_strategy_evidence_daily_is_bounded_and_isolated():
    job = _manifest_job("strategy-evidence-daily")
    assert job["schedule"] == "20 23 * * 1-5"
    assert job["context_from"] == [
        "closing-triage", "market-history-cache", "cycle-state-shadow"
    ]
    assert _entry_command(job) == (
        "python scripts/run_agent_dag.py strategy-evidence-daily --emit-target"
    )
    assert _run_command(job) == "python scripts/strategy_evidence_daily.py --json"
    writes = " ".join(job["allowed_state_writes"])
    assert "strategy_evidence" in writes
    assert "portfolio" not in writes
    assert "signal_ledger" not in writes


def test_strategy_promotion_nightly_is_local_bounded_and_after_forward_settlement():
    job = _manifest_job("strategy-promotion-nightly")
    assert job["schedule"] == "55 23 * * 1-5"
    assert job["external"] is False
    assert job["deliver"] == "local"
    assert job["silent_when_no_signal"] is True
    assert job["context_from"] == ["strategy-forward-settlement-daily"]
    assert _entry_command(job) == "python scripts/strategy_promotion_runner.py --json"
    assert _run_command(job) == _entry_command(job)
    writes = " ".join(job["allowed_state_writes"])
    assert "strategy_registry.json" in writes
    assert "portfolio" not in writes
    assert "signal_ledger" not in writes
    assert "不设置 runtime_allowed" in job["external_note"]


def test_sentiment_backfill_bootstraps_s6_without_network_or_overwriting_forward_rows():
    job = _manifest_job("sentiment-daily-backfill")
    assert job["schedule"] == "50 22 * * 1-5"
    assert job["external"] is False
    assert job["context_from"] == ["market-history-cache"]
    assert _run_command(job) == (
        "python scripts/sentiment_daily_backfill.py --bootstrap-min-days 180 --json"
    )
    assert "sentiment_daily" in " ".join(job["allowed_state_writes"])


def test_paper_trading_jobs_are_research_only_and_dag_ordered():
    open_job = _manifest_job("paper-trading-open")
    monitor_job = _manifest_job("paper-trading-monitor")
    close_job = _manifest_job("paper-trading-close")

    assert open_job["context_from"] == ["open-confirmation"]
    assert monitor_job["context_from"] == ["paper-trading-open"]
    assert close_job["context_from"] == ["paper-trading-monitor"]
    for job in (open_job, monitor_job, close_job):
        assert "paper_trading_runner.py" in _run_command(job)
        assert "signal_ledger.jsonl" in " ".join(job["allowed_state_writes"])
    # research-only 由 runner 的 live_order_sent=False 保证，与推不推送无关。
    # close 是唯一推送的一份（#174 待办 1，对齐 portfolio-check）；open 每天 1 次、
    # monitor 每天 16 次，推了就是刷屏，保持不推。
    assert open_job["deliver"] == "local"
    assert monitor_job["deliver"] == "local"
    assert close_job["deliver"] == "origin"


def test_tail_close_jobs_are_disabled_research_only_and_dag_ordered():
    prepare = _manifest_job("tail-close-prepare")
    decision = _manifest_job("tail-close-decision")
    after_hours = _manifest_job("tail-close-after-hours-shadow")
    after_hours_reconcile = _manifest_job("tail-close-after-hours-reconcile")
    reconcile = _manifest_job("tail-close-reconcile")

    assert prepare["schedule"] == "35 14 * * 1-5"
    assert decision["schedule"] == "50 14 * * 1-5"
    assert decision["context_from"] == ["tail-close-prepare"]
    assert reconcile["context_from"] == ["tail-close-decision"]
    assert after_hours["context_from"] == []
    assert after_hours_reconcile["schedule"] == "31 15 * * 1-5"
    assert after_hours_reconcile["context_from"] == [
        "tail-close-after-hours-shadow"
    ]
    for job in (
        prepare,
        decision,
        after_hours,
        after_hours_reconcile,
        reconcile,
    ):
        assert job["enabled"] is False
        assert job["deliver"] == "local"
        assert job["silent_when_no_signal"] is True
        assert _entry_command(job) == (
            f"python scripts/run_agent_dag.py {job['id']} --emit-target"
        )
        assert "tail_close_signal.py" in _run_command(job)


def test_pure_notification_jobs_deliver_local_and_skip_agent_context():
    # 飞书推送已停用（2026-08-13），纯通知类 job 只落盘不推送，仍不经过 agent context。
    for job_id in (
        "capital-flow",
        "event-calendar",
        "official-policy-watch",
        "news-monitor",
        "news-monitor-weekend",
        "news-monitor-intraday",
    ):
        assert _manifest_job(job_id)["deliver"] == "local"


def test_high_frequency_idle_prone_jobs_opt_into_adaptive_backoff():
    for job_id in ("official-policy-watch", "news-monitor", "news-monitor-weekend", "news-monitor-intraday"):
        assert _manifest_job(job_id)["adaptive_backoff"] is True


def test_candidate_jobs_carry_the_input_snapshot_switch_in_the_manifest():
    """The switch must travel with the repo, not with one machine's .env."""
    for job_id in ("candidate-preopen", "candidate-discovery"):
        assert _manifest_job(job_id)["run"]["env"] == {
            "A_STOCK_SKIP_INPUT_SNAPSHOT": "1"
        }


def test_auction_finalize_lets_a_failed_snapshot_through_so_it_can_degrade():
    """依赖门挡住 finalize 等于全链静默；放行后由 finalize 自己判空走降级报告。"""
    policy = _manifest_job("auction-finalize")["dependency_policy"]

    assert set(policy["accepted_statuses"]) == {"ok", "timeout", "failed"}
    assert policy["optional_jobs"] == ["auction-market-snapshot"]


def test_auction_finalize_declared_tolerance_actually_reaches_the_dag():
    """上一条只断言了 manifest 字段 —— 字段写对不等于 DAG 会照做。

    2026-08-18 的级联就是这么漏过去的：`accepted_statuses` 声明了接受
    timeout/failed，而 run_agent_dag 的短路根本不读这个字段，于是 finalize
    在快照超时那天压根没启动。这条用真实 manifest 直接问 DAG 的判定函数。
    """
    from scripts.run_agent_dag import consumers_tolerating

    with open(MANIFEST_PATH, encoding="utf-8") as handle:
        jobs = {job["id"]: job for job in json.load(handle)["jobs"]}
    batch = ["auction-snapshot", "auction-market-snapshot", "auction-finalize"]

    for status in ("timeout", "failed"):
        assert "auction-finalize" in consumers_tolerating(
            "auction-snapshot", status, jobs=jobs, batch_jobs=batch
        ), f"auction-snapshot {status} 时 finalize 必须仍被放行"

    # 反向：open-confirmation 没有声明容忍，finalize 失败必须仍然挡住它。
    assert consumers_tolerating(
        "auction-finalize",
        "failed",
        jobs=jobs,
        batch_jobs=["auction-finalize", "open-confirmation"],
    ) == []


def test_auction_chain_watchdog_is_registered_and_survives_a_broken_chain():
    """watchdog 必须在链路挂掉时照跑，所以它不能有任何上游依赖。"""
    job = _manifest_job("auction-chain-watch")

    assert job["enabled"] is True
    assert job["context_from"] == []
    assert "dependency_policy" not in job
    assert job["deliver"] == "local"
    assert job["silent_when_no_signal"] is True
    assert "scripts/cron_failure_watch.py" in _run_command(job)
    # 09:35 竞价链应已收口，10:35 复查一次做双保险
    assert job["schedule"] == "35 9,10 * * 1-5"


def test_all_cron_jobs_have_zero_feishu_chat_egress():
    with open(MANIFEST_PATH, encoding="utf-8") as handle:
        jobs = json.load(handle)["jobs"]

    assert [job["id"] for job in jobs if job.get("deliver") == "feishu_direct"] == []


def test_run_env_cannot_override_runner_owned_keys():
    for env in (
        {"A_STOCK_STATE_HOME": "/tmp/elsewhere"},
        {"PATH": "/tmp/bin"},
        {"A_STOCK_RUN_ID": "forged"},
        {"lowercase_key": "1"},
        {"A_STOCK_FLAG": ["not", "scalar"]},
    ):
        job = json.loads(json.dumps(VALID_JOB))
        job["run"]["env"] = env
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"jobs": [job]}, f)
            path = f.name
        try:
            assert validate(path) is False, env
        finally:
            os.unlink(path)


def test_run_env_accepts_a_plain_business_flag():
    job = json.loads(json.dumps(VALID_JOB))
    job["run"]["env"] = {"A_STOCK_SKIP_INPUT_SNAPSHOT": "1"}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"jobs": [job]}, f)
        path = f.name
    try:
        assert validate(path) is True
    finally:
        os.unlink(path)


def test_every_job_declares_a_timeout_tier():
    """分级约定的目的是让「调大超时」跨档时必须显式改档位，而不是手滑。"""
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        manifest = json.load(f)

    for job in manifest["jobs"]:
        run = job["run"]
        tier = run.get("timeout_tier")
        assert tier in TIMEOUT_TIERS, f"{job['id']} tier={tier}"
        low, high = TIMEOUT_TIERS[tier]
        assert low <= run["timeout_seconds"] <= high, job["id"]
        if tier in TIMEOUT_RATIONALE_TIERS:
            assert run.get("timeout_rationale", "").strip(), job["id"]


def test_manifest_restates_the_tier_bands_without_drift():
    """manifest 里的档位表是给改超时的人看的，漂移了就是骗人。"""
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        declared = json.load(f)["timeout_tier_bands"]

    assert {k: tuple(v) for k, v in declared.items()
            if not k.startswith("_")} == TIMEOUT_TIERS

    drifted = json.loads(json.dumps(VALID_JOB))
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"jobs": [drifted], "timeout_tier_bands": {"short": [10, 999]}}, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_missing_timeout_tier_is_rejected():
    job = json.loads(json.dumps(VALID_JOB))
    del job["run"]["timeout_tier"]

    assert _validate_job(job) is False


def test_unknown_timeout_tier_is_rejected():
    job = json.loads(json.dumps(VALID_JOB))
    job["run"]["timeout_tier"] = "instant"

    assert _validate_job(job) is False


def test_timeout_outside_the_declared_tier_band_is_rejected():
    """把 short 作业的超时从 10s 调到 600s，必须撞上档位边界。"""
    job = json.loads(json.dumps(VALID_JOB))
    job["run"]["timeout_seconds"] = 600

    assert _validate_job(job) is False

    job["run"]["timeout_tier"] = "long"
    job["run"]["timeout_rationale"] = "全市场抓取，实测冷跑数分钟"
    assert _validate_job(job) is True


def test_long_tier_requires_a_written_rationale():
    job = json.loads(json.dumps(VALID_JOB))
    job["run"]["timeout_seconds"] = 1800
    job["run"]["timeout_tier"] = "long"

    assert _validate_job(job) is False

    job["run"]["timeout_rationale"] = "   "
    assert _validate_job(job) is False


def test_timeout_above_the_top_tier_ceiling_is_rejected():
    job = json.loads(json.dumps(VALID_JOB))
    job["run"]["timeout_seconds"] = 7200
    job["run"]["timeout_tier"] = "long"
    job["run"]["timeout_rationale"] = "很久"

    assert _validate_job(job) is False


def test_missing_field():
    manifest = {"jobs": [{"id": "bad", "name": "Bad"}]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_duplicate_ids():
    j1 = dict(VALID_JOB)
    j2 = dict(VALID_JOB)
    manifest = {"jobs": [j1, j2]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_placeholders_are_rejected_without_vars():
    j = dict(VALID_JOB)
    j["command_argv"] = ["python", "scripts/run_agent_dag.py", "test-job", "--emit-target", "--var", "code={code}"]
    manifest = {"jobs": [j]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_placeholders_are_rejected_even_with_vars():
    j = dict(VALID_JOB)
    j["command_argv"] = ["python", "scripts/run_agent_dag.py", "test-job", "--var", "code={code}"]
    j["template_vars"] = ["code"]
    manifest = {"jobs": [j]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_run_command_placeholders_are_rejected():
    """run.argv 也必须自包含，不能依赖 Gateway 动态注入。"""
    j = dict(VALID_JOB)
    j["run"] = dict(VALID_JOB["run"])
    j["run"]["argv"] = ["python", "script.py", "{code}"]
    manifest = {"jobs": [j]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_template_vars_are_rejected_without_placeholders():
    j = dict(VALID_JOB)
    j["template_vars"] = ["code", "name"]
    manifest = {"jobs": [j]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_direct_business_command_rejected():
    """Hermes command 必须先进 runner，不能直接跑业务脚本污染主上下文。"""
    j = dict(VALID_JOB)
    j["command_argv"] = ["python", "skills/stock-triage/scripts/intraday_monitor.py", "--json"]
    manifest = {"jobs": [j]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_business_state_writes_cannot_use_hermes_install_home():
    j = dict(VALID_JOB)
    j["allowed_state_writes"] = ["$HERMES_HOME/cron/output/test-job/"]
    manifest = {"jobs": [j]}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_top_level_duplicate_runtime_script_rejected():
    """run.argv 必须指向 canonical skills 路径，不能用顶层重复脚本。"""
    j = dict(VALID_JOB)
    j["run"] = dict(VALID_JOB["run"])
    j["run"]["argv"] = ["python", "scripts/intraday_monitor.py", "--json"]
    manifest = {"jobs": [j]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_missing_isolation_contract_rejected():
    j = dict(VALID_JOB)
    del j["context_scope"]
    manifest = {"jobs": [j]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_invalid_dependency_policy_rejected():
    j = dict(VALID_JOB)
    j["context_from"] = ["upstream"]
    j["dependency_policy"] = {
        "required": True,
        "max_age_minutes": 0,
        "trading_date": "tomorrow",
    }
    manifest = {"jobs": [j]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_unknown_dependency_rejected():
    j = dict(VALID_JOB)
    j["context_from"] = ["does-not-exist"]
    manifest = {"jobs": [j]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_dependency_cycle_rejected():
    first = dict(VALID_JOB)
    first["id"] = "first"
    first["command_argv"] = ["python", "scripts/run_agent_dag.py", "first", "--emit-target"]
    first["context_from"] = ["second"]
    second = dict(VALID_JOB)
    second["id"] = "second"
    second["command_argv"] = ["python", "scripts/run_agent_dag.py", "second", "--emit-target"]
    second["context_from"] = ["first"]
    manifest = {"jobs": [first, second]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_repo_manifest_keeps_runtime_isolation_contract():
    manifest_path = os.path.join(ROOT, "cron", "hermes-cron-manifest.json")
    assert validate(manifest_path) is True

    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    jobs = {job["id"]: job for job in manifest["jobs"]}

    for required in [
        "provider-health",
        "hot-money-context",
        "social-attention-preopen",
        "social-attention-midday",
        "social-attention-close",
        "hot-money-context-backfill",
        "candidate-preopen",
        "candidate-discovery",
            "auction-snapshot",
            "auction-market-snapshot",
            "auction-finalize",
            "open-confirmation",
            "preopen-intelligence-brief",
            "auction-intelligence-brief",
            "open-intelligence-brief",
            "hot-money-morning-checkpoint",
            "hot-money-afternoon-checkpoint",
            "closing-triage",
            "news-monitor-intraday",
            "news-l1-scan",
            "news-monitor-weekend",
            "official-policy-watch",
            "market-pulse-1314",
            "market-pulse-1500",
            "stock-intelligence-refresh",
            "serenity-refresh-plan",
    ]:
        assert required in jobs
        assert _entry_command(jobs[required]) == (
            f"python scripts/run_agent_dag.py {required} --emit-target"
        )
        assert jobs[required]["context_scope"] == "cron"

    assert "--codes" not in _run_command(jobs["auction-snapshot"])
    assert "--codes" not in _run_command(jobs["auction-finalize"])
    assert "--codes" not in _run_command(jobs["open-confirmation"])
    assert jobs["candidate-discovery"]["context_from"][0] == "hot-money-context"
    assert "social-attention-close" in jobs["candidate-discovery"]["context_from"]
    assert jobs["candidate-discovery"]["dependency_policy"]["optional_jobs"] == [
        "social-attention-close",
    ]
    assert jobs["candidate-preopen"]["schedule"] == "30 8 * * 1-5"
    assert _run_command(jobs["candidate-preopen"]).endswith(
        "candidate_discovery.py --bootstrap-if-missing --no-settle --json"
    )
    assert jobs["hot-money-context-backfill"]["schedule"] == "20 8 * * 1-5"
    assert _run_command(jobs["hot-money-context"]).endswith("--cache-only")
    assert jobs["social-attention-preopen"]["schedule"] == "42 8 * * 1-5"
    assert jobs["social-attention-midday"]["schedule"] == "37 11 * * 1-5"
    assert jobs["social-attention-close"]["schedule"] == "4 15 * * 1-5"
    assert jobs["news-monitor-intraday"]["schedule"] == (
        "2,17,32,47 9-11,13-14 * * 1-5"
    )
    assert jobs["news-l1-scan"]["schedule"] == "4,34 8-22 * * 1-5"
    assert _run_command(jobs["news-monitor-intraday"]).endswith("--mode intraday --json")
    assert jobs["news-l1-scan"]["trading_day_policy"] == "calendar_day"
    assert jobs["news-l1-scan"]["context_from"] == []
    assert _run_command(jobs["news-l1-scan"]) == "python scripts/news_l1_scan.py --silent --json"
    assert jobs["news-monitor-weekend"]["trading_day_policy"] == "calendar_day"
    assert jobs["news-monitor-weekend"]["context_from"] == []
    assert jobs["news-monitor-weekend"]["schedule"] == "0 9,12,18,22 * * 0,6"
    assert any("catalyst_context.json" in path for path in jobs["news-monitor"]["allowed_state_writes"])
    assert any("catalyst_context.json" in path for path in jobs["news-monitor-intraday"]["allowed_state_writes"])
    assert jobs["official-policy-watch"]["trading_day_policy"] == "calendar_day"
    assert jobs["official-policy-watch"]["schedule"] == "3,33 8-22 * * 1-5"
    assert _run_command(jobs["official-policy-watch"]) == (
        "python skills/policy-intent-decoder/scripts/watch_official_policy.py --json"
    )
    assert jobs["official-policy-watch"]["silent_when_no_signal"] is True
    assert any(
        "policy-intent-decoder/data" in path
        for path in jobs["official-policy-watch"]["allowed_state_writes"]
    )
    # intraday-alert is an origin-push job: it emits human-readable text, not --json.
    assert _run_command(jobs["intraday-alert"]) == "python skills/stock-triage/scripts/intraday_monitor.py"
    assert jobs["intraday-alert"]["run"]["timeout_seconds"] >= 120
    assert jobs["news-monitor"]["run"]["timeout_seconds"] >= 180
    for job_id, profile in (
        ("market-pulse-1314", "midday"),
        ("market-pulse-1500", "close"),
    ):
        command = _run_command(jobs[job_id])
        # market-pulse is an origin-push job: it emits the human-readable summary, not --json.
        assert command == f"python scripts/market_pulse_digest.py --profile {profile} --max-chars 200"
        assert jobs[job_id]["run"]["timeout_seconds"] == 120
        assert "prompt" not in command
        assert "web_fetch" not in command
        assert jobs[job_id]["max_output_chars"] <= 1200
    for job_id in (
        "social-attention-preopen",
        "social-attention-midday",
        "social-attention-close",
    ):
        assert jobs[job_id]["enabled"] is True
        assert _run_command(jobs[job_id]).endswith("--json")
        assert jobs[job_id]["deliver"] == "local"
        assert any(
            "social_attention.json" in path
            for path in jobs[job_id]["allowed_state_writes"]
        )
    assert jobs["candidate-preopen"]["context_from"] == [
        "hot-money-context-backfill",
        "social-attention-preopen",
    ]
    assert jobs["candidate-preopen"]["dependency_policy"].get("optional_jobs") == [
        "social-attention-preopen"
    ]
    assert jobs["candidate-preopen"]["dependency_policy"]["trading_date"] == (
        "same_trading_date"
    )
    assert jobs["candidate-preopen"]["dependency_policy"]["max_age_minutes"] == 90
    assert jobs["auction-snapshot"]["context_from"] == ["candidate-preopen"]
    assert jobs["auction-snapshot"]["dependency_policy"].get("optional_jobs") == [
        "candidate-preopen"
    ]
    from scripts.run_agent_dag import execution_order

    assert execution_order(jobs, ["candidate-preopen"]) == [
        "hot-money-context-backfill",
        "candidate-preopen",
    ]
    # candidate-preopen 转为 optional 之后不再被拉进竞价快照的同批展开。
    # 这不是能力损失：candidate-preopen 是 1200s 的 long 档作业，而
    # auction-snapshot 自己只有 180s，旧的「同批引导」在竞价窗口里必然超时，
    # 引导从来没成功过。改由隔夜观察池降级（pool_stale → research_only）接手。
    assert execution_order(jobs, ["auction-snapshot"]) == ["auction-snapshot"]
    assert jobs["open-confirmation"]["context_from"] == ["auction-finalize"]
    assert jobs["candidate-preopen"]["deliver"] == "local"
    # Same-window execution artifacts are now local; the following brief jobs
    # remain the single origin-push surface for each window.
    assert jobs["auction-finalize"]["deliver"] == "local"
    assert jobs["auction-intelligence-brief"]["deliver"] == "origin"
    assert jobs["open-confirmation"]["deliver"] == "local"
    assert jobs["open-intelligence-brief"]["deliver"] == "origin"
    assert jobs["auction-market-snapshot"]["schedule"] == "24 9 * * 1-5"
    assert "--bounded-universe" in _run_command(jobs["auction-market-snapshot"])
    assert "--full-universe" not in _run_command(jobs["auction-market-snapshot"])
    for job_id, schedule, stage, deliver in (
        ("preopen-intelligence-brief", "50 8 * * 1-5", "preopen", "origin"),
        ("open-intelligence-brief", "36 9 * * 1-5", "open", "origin"),
    ):
        assert jobs[job_id]["schedule"] == schedule
        assert jobs[job_id]["deliver"] == deliver
        assert jobs[job_id]["context_from"] == []
        assert _run_command(jobs[job_id]).endswith(f"--stage {stage}")
    auction_brief = jobs["auction-intelligence-brief"]
    assert auction_brief["schedule"] == "27 9 * * 1-5"
    assert auction_brief["deliver"] == "origin"
    assert auction_brief["context_from"] == [
        "auction-finalize",
        "auction-market-snapshot",
    ]
    assert auction_brief["dependency_policy"] == {
        "trading_date": "same_trading_date",
        "max_age_minutes": 15,
        "optional_jobs": ["auction-market-snapshot"],
    }
    assert _run_command(auction_brief).endswith("--stage auction")
    assert jobs["hot-money-morning-checkpoint"]["context_from"] == [
        "auction-finalize",
        "open-confirmation",
    ]
    assert jobs["hot-money-morning-checkpoint"]["dependency_policy"]["optional_jobs"] == [
        "open-confirmation"
    ]
    assert jobs["hot-money-morning-checkpoint"]["schedule"] == "50 9 * * 1-5"
    assert jobs["hot-money-afternoon-checkpoint"]["context_from"] == ["open-confirmation"]
    assert jobs["hot-money-afternoon-checkpoint"]["schedule"] == "15 13 * * 1-5"
    for job_id, profile in (
        ("hot-money-morning-checkpoint", "morning_confirm"),
        ("hot-money-afternoon-checkpoint", "afternoon_reflow"),
    ):
        assert _run_command(jobs[job_id]) == (
            f"python skills/daban-stock-picker/scripts/hot_money_checkpoint.py "
            f"--profile {profile} --json"
        )
        assert jobs[job_id]["run"]["timeout_seconds"] == 300
        assert any(
            "hot_money_checkpoint" in path
            for path in jobs[job_id]["allowed_state_writes"]
        )
    assert jobs["serenity-refresh-plan"]["context_from"] == [
        "closing-triage",
        "stock-intelligence-refresh",
    ]
    assert jobs["serenity-refresh-plan"]["dependency_policy"]["optional_jobs"] == [
        "stock-intelligence-refresh",
    ]
    assert jobs["serenity-refresh-plan"]["deliver"] == "local"
    assert jobs["auction-finalize"]["schedule"] == "26 9 * * 1-5"
    assert any("monitor_registry.json" in path for path in jobs["auction-finalize"]["allowed_state_writes"])
    assert any("signal_ledger.jsonl" in path for path in jobs["auction-finalize"]["allowed_state_writes"])
    assert any("recommendations.json" in path for path in jobs["open-confirmation"]["allowed_state_writes"])
    assert any("signal_ledger.jsonl" in path for path in jobs["open-confirmation"]["allowed_state_writes"])
    assert any(
        "portfolio_research_snapshots" in path
        for path in jobs["open-confirmation"]["allowed_state_writes"]
    )
    assert any("signal_ledger.jsonl" in path for path in jobs["intraday-alert"]["allowed_state_writes"])
    assert set(jobs["closing-triage"]["context_from"]) >= {"portfolio-check"}
    assert jobs["performance-weekly"]["dependency_policy"]["trading_date"] == "same_trading_date"
    assert jobs["performance-weekly"]["context_from"] == ["performance-daily"]
    assert _run_command(jobs["performance-weekly"]).endswith("--json --gate")
    assert any("strategy_registry.json" in path for path in jobs["performance-weekly"]["allowed_state_writes"])
    assert _run_command(jobs["performance-daily"]).endswith("--json")
    assert _run_command(jobs["ledger-projector"]).startswith("python scripts/agent_state_projector.py")
    assert jobs["snapshot-gc"]["context_from"] == []
    assert jobs["snapshot-gc"]["deliver"] == "local"
    assert _run_command(jobs["snapshot-gc"]).endswith("--apply --json")
    assert jobs["provider-health"]["deliver"] == "local"
    assert _run_command(jobs["provider-health"]) == "python scripts/provider_doctor.py --json"
    assert manifest["default_trading_day_policy"] == "required"
    for job_id in ("institution-weekly", "event-calendar", "performance-weekly", "official-policy-watch", "news-l1-scan", "news-monitor-weekend"):
        assert jobs[job_id]["trading_day_policy"] == "calendar_day"
    assert any(
        "scan_cursors/institution-weekly/" in path
        for path in jobs["institution-weekly"]["allowed_state_writes"]
    )
    assert any(
        "scan_cursors/event-calendar/" in path
        for path in jobs["event-calendar"]["allowed_state_writes"]
    )
    assert "pulse_engine" not in manifest.get("external_dependencies", {})
    assert "builderpulse" not in manifest.get("external_dependencies", {})


def test_auction_snapshot_survives_a_dead_candidate_preopen(tmp_path, monkeypatch):
    """candidate-preopen 是整条竞价链的单点：它一挂，两个快照作业都无产出。

    降级契约是「用隔夜观察池继续抓今天的真实竞价，但整链标 research_only」，
    前提是依赖门先放行。这里用真实 manifest 的策略直接问依赖门 —— 断言 manifest
    字段证明不了运行时会照做（2026-08-18 的教训）。
    """
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("A_STOCK_STATE_ID", raising=False)
    from runtime_context import evaluate_dependencies

    for job_id in ("auction-snapshot", "auction-market-snapshot"):
        job = _manifest_job(job_id)
        # 当日一个 artifact 都没有 —— candidate-preopen 压根没跑成
        gate = evaluate_dependencies(
            job["context_from"],
            trading_date="2026-06-12",
            batch_id="a-share-20260612",
            policy=job["dependency_policy"],
        )
        assert gate["passed"] is True, f"{job_id} 仍被死掉的 candidate-preopen 挡住"
        preopen = next(
            item for item in gate["dependencies"] if item["job_id"] == "candidate-preopen"
        )
        assert preopen["gate_status"] == "optional_failed"
        assert preopen["reasons"] == ["missing"]


def test_candidate_preopen_keeps_its_own_trigger_so_optional_is_not_a_silent_disable():
    """把依赖标 optional 等于把它排除出同批展开。

    它必须自己有 cron 触发点，否则「降级」会变成「悄悄停用」。
    """
    job = _manifest_job("candidate-preopen")

    assert job["enabled"] is True
    assert job["schedule"] == "30 8 * * 1-5"
