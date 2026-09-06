"""Reconciliation contract between the manifest and an installed OpenClaw host.

These tests never touch a real deployment: the installed side is always a
fixture, so the plan is exercised offline on machines that have no OpenClaw at
all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.generate_openclaw_cron import (
    MANAGED_JOB_PREFIX,
    apply_reconcile_plan,
    build_openclaw_commands,
    build_reconcile_plan,
    command_from_spec,
    compare_installed_job,
    desired_job_spec,
)

ROOT = Path(__file__).resolve().parents[1]
DISABLE_TEMPLATE = "{openclaw} cron disable {job_id}"


def _job(job_id: str, *, enabled: bool = True, deliver: str = "local") -> dict:
    return {
        "id": job_id,
        "name": job_id,
        "schedule": "0 9 * * 1-5",
        "timezone": "Asia/Shanghai",
        "enabled": enabled,
        "deliver": deliver,
        "context_from": [],
        "run": {"timeout_seconds": 30},
    }


def _plan(manifest_jobs, installed, **kwargs):
    return build_reconcile_plan(
        {"jobs": manifest_jobs},
        installed_jobs=installed,
        repo_dir="/repo",
        python="/repo/.venv/bin/python",
        state_home="/state",
        **kwargs,
    )


def _installed(job_id: str, **overrides) -> dict:
    spec = desired_job_spec(
        job_id,
        _job(job_id),
        jobs={job_id: _job(job_id)},
        repo_dir="/repo",
        python="/repo/.venv/bin/python",
        grace_seconds=60,
        command_env=["A_STOCK_STATE_HOME=/state"],
        delivery_channel="discord",
        delivery_to=None,
        delivery_account="default",
    )
    record = {
        "id": f"cron-{job_id}",
        "name": f"{MANAGED_JOB_PREFIX}{job_id}",
        "cron": spec["schedule"],
        "tz": spec["timezone"],
        "session": spec["session"],
        "commandArgv": spec["command_argv"],
        "commandCwd": spec["command_cwd"],
        "timeoutSeconds": spec["timeout_seconds"],
        "outputMaxBytes": spec["output_max_bytes"],
        "commandEnv": spec["command_env"],
    }
    record.update(overrides)
    return record


def test_a_manifest_disabled_job_that_is_still_installed_gets_a_disable_action():
    plan = _plan([_job("gone", enabled=False)], [_installed("gone")])
    action = plan["actions"][0]

    assert action["action"] == "disable"
    assert action["reason"] == "manifest_disabled_but_installed"
    assert action["installed_ids"] == ["cron-gone"]
    # Without a verified CLI verb the plan refuses to invent one.
    assert action["command"] is None
    assert action["command_status"] == "unverified_cli_verb"
    assert plan["applicable"] is False


def test_a_supplied_disable_template_resolves_the_command():
    plan = _plan(
        [_job("gone", enabled=False)],
        [_installed("gone")],
        disable_command_template=DISABLE_TEMPLATE,
    )
    action = plan["actions"][0]

    assert action["command"] == "openclaw cron disable cron-gone"
    assert action["command_status"] == "resolved"
    assert plan["applicable"] is True


def test_a_manifest_disabled_job_that_is_not_installed_needs_no_action():
    plan = _plan([_job("gone", enabled=False)], [])

    assert plan["actions"][0]["action"] == "skipped"
    assert plan["actions"][0]["command"] is None
    assert plan["applicable"] is True


def test_an_in_sync_job_is_unchanged_and_emits_no_command():
    plan = _plan([_job("target")], [_installed("target")])
    action = plan["actions"][0]

    assert action["action"] == "unchanged"
    assert action["command"] is None
    assert action["drifted_fields"] == []
    assert action["unverifiable_fields"] == []
    assert plan["summary"] == {"unchanged": 1}


def test_reapplying_the_same_plan_is_a_no_op():
    manifest = [_job("target")]
    first = _plan(manifest, [])
    assert first["actions"][0]["action"] == "create"

    # Simulate the host after the create landed, then re-plan.
    second = _plan(manifest, [_installed("target")])
    assert second["actions"][0]["action"] == "unchanged"
    assert [item["command"] for item in second["actions"]] == [None]


def test_parameter_drift_is_named_field_by_field():
    plan = _plan(
        [_job("target")],
        [_installed("target", timeoutSeconds=9, commandCwd="/somewhere/else")],
    )
    action = plan["actions"][0]

    assert action["action"] == "update"
    assert action["drifted_fields"] == ["command_cwd", "timeout_seconds"]
    assert action["comparison"]["timeout_seconds"]["installed"] == 9
    assert action["comparison"]["schedule"]["state"] == "match"
    assert action["command"].startswith("openclaw cron edit cron-target")


def test_a_field_the_host_does_not_report_is_unknown_not_drift():
    stripped = _installed("target")
    del stripped["outputMaxBytes"]
    del stripped["commandEnv"]

    action = _plan([_job("target")], [stripped])["actions"][0]

    assert action["action"] == "unchanged"
    assert action["unverifiable_fields"] == ["command_env", "output_max_bytes"]
    assert action["drifted_fields"] == []


def test_duplicate_installed_names_stay_a_conflict_rather_than_a_guess():
    plan = _plan(
        [_job("target")],
        [_installed("target"), _installed("target", id="cron-other")],
    )
    action = plan["actions"][0]

    assert action["action"] == "conflict"
    assert action["reason"] == "duplicate_installed_name"
    assert action["command"] is None
    assert sorted(action["installed_ids"]) == ["cron-other", "cron-target"]
    assert plan["applicable"] is False


def test_unknown_managed_jobs_are_reported_but_never_scheduled_for_deletion():
    plan = _plan([_job("target")], [_installed("target"), _installed("retired")])

    assert plan["orphaned_managed_jobs"] == ["retired"]
    assert [item["logical_id"] for item in plan["actions"]] == ["target"]
    assert not any(
        "delete" in (item["command"] or "") for item in plan["actions"]
    )


def test_jobs_owned_by_other_applications_are_invisible_to_the_plan():
    plan = _plan(
        [_job("target")],
        [_installed("target"), {"id": "cron-x", "name": "Someone else: nightly"}],
    )

    assert plan["orphaned_managed_jobs"] == []
    assert plan["summary"] == {"unchanged": 1}


def test_comparison_treats_argv_order_as_significant():
    spec = desired_job_spec(
        "target", _job("target"), jobs={"target": _job("target")},
        repo_dir="/repo", python="/py", grace_seconds=60, command_env=[],
        delivery_channel="discord", delivery_to=None, delivery_account="default",
    )
    reversed_argv = {"commandArgv": list(reversed(spec["command_argv"]))}

    assert compare_installed_job(spec, reversed_argv)["command_argv"]["state"] == "drift"
    assert compare_installed_job(
        spec, {"commandArgv": list(spec["command_argv"])}
    )["command_argv"]["state"] == "match"


def test_generated_command_and_plan_agree_on_the_desired_parameters():
    manifest = {"jobs": [_job("target")]}
    direct = build_openclaw_commands(
        manifest, repo_dir="/repo", python="/repo/.venv/bin/python", state_home="/state"
    )[0]
    spec = desired_job_spec(
        "target", _job("target"), jobs={"target": _job("target")},
        repo_dir="/repo", python="/repo/.venv/bin/python", grace_seconds=60,
        command_env=["A_STOCK_STATE_HOME=/state"], delivery_channel="discord",
        delivery_to=None, delivery_account="default",
    )

    assert direct == command_from_spec(spec, openclaw="openclaw", installed_id=None)


def test_every_enabled_manifest_job_reaches_the_dag_entry_point():
    manifest = json.loads((ROOT / "cron" / "hermes-cron-manifest.json").read_text())
    commands = build_openclaw_commands(
        manifest, repo_dir=str(ROOT), python="/venv/bin/python",
        delivery_to="user:test-recipient", state_home="/state",
    )
    enabled = [job for job in manifest["jobs"] if job.get("enabled", True)]

    assert len(commands) == len(enabled)
    for command in commands:
        argv = json.loads(command.split("--command-argv ")[1].split("' --command-cwd")[0].strip("'"))
        assert argv[1].endswith("scripts/run_agent_dag.py")
        assert "--runtime" in argv and argv[argv.index("--runtime") + 1] == "openclaw"


def _origin_job(job_id: str) -> dict:
    job = _job(job_id)
    job["deliver"] = "origin"
    return job


def test_a_plan_is_still_produced_when_no_delivery_target_is_configured():
    # 16 of the 64 enabled manifest jobs deliver to origin. Refusing to describe
    # the other 48 because of them makes the read-only diagnostic unusable on the
    # very machine that needs it, which is where the target is deliberately absent.
    plan = _plan([_origin_job("announce"), _job("quiet")], [], delivery_to=None)

    blocked = next(item for item in plan["actions"] if item["logical_id"] == "announce")
    assert blocked["action"] == "blocked"
    assert blocked["reason"] == "delivery_target_missing"
    assert blocked["command"] is None

    described = next(item for item in plan["actions"] if item["logical_id"] == "quiet")
    assert described["action"] == "create"
    assert described["command"]
    # An incomplete plan can never be applied.
    assert plan["applicable"] is False


def test_a_blocked_job_leaks_no_recipient_and_no_configuration_wording():
    plan = _plan([_origin_job("announce")], [], delivery_to=None)
    serialised = json.dumps(plan, ensure_ascii=False)

    assert "--to" not in serialised
    assert "A_STOCK_DELIVERY_TO" not in serialised


def test_a_configured_delivery_target_produces_a_normal_action():
    plan = _plan([_origin_job("announce")], [], delivery_to="user:verified-recipient")
    action = plan["actions"][0]

    assert action["action"] == "create"
    assert "--announce" in action["command"]
    assert plan["applicable"] is True


def test_an_unknown_deliver_policy_is_classified_rather_than_crashing_the_plan():
    broken = _job("weird")
    broken["deliver"] = "orgin"
    plan = _plan([broken, _job("quiet")], [], delivery_to=None)

    assert plan["summary"] == {"blocked": 1, "create": 1}
    assert plan["actions"][1]["reason"] == "deliver_policy_unknown"


def test_the_apply_path_still_fails_closed_without_a_target(tmp_path):
    with pytest.raises(ValueError, match="origin delivery target is required"):
        build_openclaw_commands(
            {"jobs": [_origin_job("announce")]},
            repo_dir=str(tmp_path), python="/venv/bin/python", state_home="/state",
        )


def test_the_repo_manifest_plans_end_to_end_without_a_delivery_target():
    manifest = json.loads((ROOT / "cron" / "hermes-cron-manifest.json").read_text())
    plan = build_reconcile_plan(
        manifest, installed_jobs=[], repo_dir=str(ROOT),
        python="/venv/bin/python", state_home="/state", delivery_to=None,
    )

    assert plan["summary"]["create"] + plan["summary"]["blocked"] == 64
    assert plan["summary"]["blocked"] == 16
    assert plan["applicable"] is False


def test_apply_executes_the_disable_the_plan_computed():
    # #342 shipped the disable action but the apply path rebuilt every enabled
    # job from the manifest and skipped disabled ones outright, so the command
    # was computed and then never run. Assert the consumer, not the field.
    plan = _plan(
        [_job("live"), _job("gone", enabled=False)],
        [_installed("live"), _installed("gone")],
        disable_command_template=DISABLE_TEMPLATE,
    )
    executed: list[str] = []

    result = apply_reconcile_plan(plan, runner=executed.extend)

    assert "openclaw cron disable cron-gone" in executed
    assert result["applied_by_action"] == {"disable": 1}


def test_apply_touches_only_what_the_plan_named():
    plan = _plan(
        [_job("drifted"), _job("in_sync")],
        [_installed("drifted", timeoutSeconds=9), _installed("in_sync")],
    )
    executed: list[str] = []

    result = apply_reconcile_plan(plan, runner=executed.extend)

    assert len(executed) == 1
    assert "cron-drifted" in executed[0]
    assert "in_sync" not in executed[0]
    assert result["applied"] == 1


def test_apply_refuses_an_inapplicable_plan_and_runs_nothing():
    plan = _plan([_job("gone", enabled=False)], [_installed("gone")])  # no template
    executed: list[str] = []

    with pytest.raises(ValueError, match="reconcile_plan_not_applicable"):
        apply_reconcile_plan(plan, runner=executed.extend)
    assert executed == []


def test_apply_refuses_a_plan_blocked_on_a_missing_delivery_target():
    plan = _plan([_origin_job("announce")], [], delivery_to=None)
    executed: list[str] = []

    with pytest.raises(ValueError, match="delivery_target_missing"):
        apply_reconcile_plan(plan, runner=executed.extend)
    assert executed == []


def test_a_host_that_reports_nothing_comparable_is_written_not_assumed_in_sync():
    # "in sync" would be an assumption; the plan converges by writing instead.
    action = _plan([_job("target")], [{"id": "cron-target", "name": _installed("target")["name"]}])["actions"][0]

    assert action["action"] == "update"
    assert action["reason"] == "unverifiable_installed_state"
    assert action["command"].startswith("openclaw cron edit cron-target")


def test_applying_twice_is_a_no_op_on_a_host_that_reports_its_state():
    manifest = [_job("target")]
    executed: list[str] = []
    apply_reconcile_plan(_plan(manifest, []), runner=executed.extend)
    assert len(executed) == 1

    executed.clear()
    apply_reconcile_plan(_plan(manifest, [_installed("target")]), runner=executed.extend)
    assert executed == []
