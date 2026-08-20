"""诊断包的测试。

重点在脱敏：报告要经人手传递（截图、粘贴、上传），洗漏一个 key 就是一次泄露
事故，而不是「难读一点」。其余部分是纯聚合，按仓库规范（rules/testing.md）
脚本类工具跑真实数据即可，这里只覆盖容易静默出错的边界。
"""

import json
import sqlite3

from scripts import daily_diagnostics as dd


class TestRedaction:
    def test_common_credential_shapes_are_scrubbed(self):
        raw = "\n".join([
            "openai sk-abcdef0123456789ABCDEF",
            "github ghp_ABCDEFGHIJKLMNOP0123456789",
            "slack xoxb-1234567890-abcdefghij",
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload",
            "A_STOCK_FEISHU_CHAT_ID=oc_9f8e7d6c5b4a3210",
            "api_key: 7c4a8d09ca3762af61e59520943dc26494f8941b",
        ])

        cleaned = dd.redact(raw)

        for secret in (
            "sk-abcdef0123456789ABCDEF",
            "ghp_ABCDEFGHIJKLMNOP0123456789",
            "xoxb-1234567890-abcdefghij",
            "eyJhbGciOiJIUzI1NiJ9.payload",
            "oc_9f8e7d6c5b4a3210",
            "7c4a8d09ca3762af61e59520943dc26494f8941b",
        ):
            assert secret not in cleaned, f"未脱敏: {secret}"
        assert "<REDACTED>" in cleaned

    def test_keeps_the_key_name_so_the_line_stays_diagnosable(self):
        """洗掉值、保留键名——否则读报告的人不知道是哪个凭据没配。"""
        cleaned = dd.redact("A_STOCK_FEISHU_CHAT_ID=oc_9f8e7d6c5b4a3210")

        assert "A_STOCK_FEISHU_CHAT_ID" in cleaned
        assert "oc_9f8e7d6c5b4a3210" not in cleaned

    def test_ordinary_diagnostics_text_survives(self):
        raw = "TIMEOUT after 600s\nstatus=timeout returncode=124 duration=600.143"

        assert dd.redact(raw) == raw

    def test_empty_input_is_safe(self):
        assert dd.redact("") == ""


class TestHermesAggregation:
    def _trace(self, tmp_path, events):
        home = tmp_path / "state"
        cron = home / "cron"
        cron.mkdir(parents=True)
        with open(cron / "execution_trace.jsonl", "w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return str(home)

    def test_timeout_is_reported_as_unhealthy(self, tmp_path):
        home = self._trace(tmp_path, [
            {"job_id": "j1", "event_type": "dispatch.claimed", "emitted_at": "2026-08-06T08:30:00"},
            {"job_id": "j1", "event_type": "job.started", "emitted_at": "2026-08-06T08:30:01"},
            {"job_id": "j1", "event_type": "job.finished", "status": "timeout",
             "duration_seconds": 600.1, "emitted_at": "2026-08-06T08:40:01"},
        ])

        rows, meta = dd.collect_hermes(home, "2026-08-06")

        assert meta["events"] == 3
        assert rows[0]["unhealthy"] == ["timeout"]
        assert rows[0]["max_duration"] == 600.1

    def test_duplicate_skipped_is_not_an_alert(self, tmp_path):
        """auction-snapshot 每分钟触发，末次常是 duplicate_skipped —— 那是设计内行为。"""
        home = self._trace(tmp_path, [
            {"job_id": "j1", "event_type": "dispatch.claimed"},
            {"job_id": "j1", "event_type": "job.started"},
            {"job_id": "j1", "event_type": "job.finished", "status": "duplicate_skipped"},
        ])

        rows, _ = dd.collect_hermes(home, "")

        assert rows[0]["unhealthy"] == []
        assert rows[0]["never_started"] is False

    def test_claimed_without_start_is_flagged_without_guessing_why(self, tmp_path):
        """依赖短路（PR #162 的正常行为）和真的没跑起来，现象一样、动作不同，只如实标注。"""
        home = self._trace(tmp_path, [{"job_id": "j1", "event_type": "dispatch.claimed"}])

        rows, _ = dd.collect_hermes(home, "")

        assert rows[0]["never_started"] is True
        assert rows[0]["unhealthy"] == []

    def test_missing_trace_file_does_not_crash(self, tmp_path):
        """诊断工具必须能在被诊断的系统坏掉时照常工作。"""
        rows, meta = dd.collect_hermes(str(tmp_path / "nope"), "2026-08-06")

        assert rows == []
        assert meta["events"] == 0


class TestOpenclawLedger:
    def test_reads_run_logs_and_never_writes(self, tmp_path):
        db = tmp_path / "openclaw.sqlite"
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE cron_run_logs "
                "(job_id TEXT, status TEXT, started_at TEXT, error TEXT, duration_ms INT)"
            )
            conn.execute(
                "INSERT INTO cron_run_logs VALUES "
                "('a3605e53','error','2026-08-06T15:30:00','行情工具缺失',1200)"
            )
            conn.execute("CREATE TABLE cron_jobs (job_id TEXT, name TEXT)")
            conn.execute("INSERT INTO cron_jobs VALUES ('a3605e53','A-stock: review')")

        data = dd.collect_openclaw(str(db), "2026-08-06")

        assert data["available"] is True
        assert len(data["runs"]) == 1
        assert data["runs"][0]["status"] == "error"
        assert len(data["jobs"]) == 1

    def test_database_is_opened_read_only(self, tmp_path, monkeypatch):
        """诊断工具绝不能改动被诊断的系统 —— 这是它敢在生产上跑的前提。

        只读没有别的可观测表现（本函数本来也不写），所以直接断言连接契约；
        否则一次误改就是在故障现场破坏证据。
        """
        db = tmp_path / "openclaw.sqlite"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE cron_jobs (job_id TEXT)")
        seen = {}
        real_connect = sqlite3.connect

        def spy(target, *args, **kwargs):
            seen["target"] = target
            seen["uri"] = kwargs.get("uri")
            return real_connect(target, *args, **kwargs)

        monkeypatch.setattr(dd.sqlite3, "connect", spy)
        dd.collect_openclaw(str(db), "2026-08-06")

        assert seen["uri"] is True, "必须以 URI 形式打开，否则 mode=ro 会被当成文件名"
        assert "mode=ro" in seen["target"]

    def test_absent_database_degrades_instead_of_raising(self, tmp_path):
        data = dd.collect_openclaw(str(tmp_path / "missing.sqlite"), "2026-08-06")

        assert data["available"] is False
        assert "error" in data
        assert dd.section_openclaw(data)  # 仍能产出一段说明而非崩溃


class TestArchiveRetention:
    def _seed(self, tmp_path, names):
        out = tmp_path / "diagnostics"
        out.mkdir()
        for name in names:
            (out / name).write_text("x", encoding="utf-8")
        return out

    def test_only_reports_past_the_window_are_removed(self, tmp_path):
        # 保留窗口含边界：today - 30 天 = 2026-07-07，该日当天保留，更早的删。
        out = self._seed(tmp_path, [
            "2026-05-01.md",   # 远超窗口
            "2026-07-06.md",   # 越界一天
            "2026-07-07.md",   # 恰好在边界上，保留
            "2026-08-05.md",
        ])

        removed = dd.prune_reports(str(out), 30, "2026-08-06")

        assert sorted(removed) == ["2026-05-01.md", "2026-07-06.md"]
        assert (out / "2026-07-07.md").exists()
        assert (out / "2026-08-05.md").exists()

    def test_hand_saved_files_are_never_touched(self, tmp_path):
        """只认 YYYY-MM-DD.md，手工另存的事故存档不能被定时任务删掉。"""
        out = self._seed(tmp_path, [
            "2026-05-01.md",
            "2026-05-01-incident.md",
            "notes.md",
            "2026-05-01.md.bak",
        ])

        removed = dd.prune_reports(str(out), 30, "2026-08-06")

        assert removed == ["2026-05-01.md"]
        for keeper in ("2026-05-01-incident.md", "notes.md", "2026-05-01.md.bak"):
            assert (out / keeper).exists()

    def test_retention_zero_disables_pruning(self, tmp_path):
        out = self._seed(tmp_path, ["2020-01-01.md"])

        assert dd.prune_reports(str(out), 0, "2026-08-06") == []
        assert (out / "2020-01-01.md").exists()

    def test_missing_directory_is_not_an_error(self, tmp_path):
        assert dd.prune_reports(str(tmp_path / "nope"), 30, "2026-08-06") == []

    def test_archive_writes_dated_file_and_summarises_on_stdout(self, tmp_path, capsys):
        out = tmp_path / "diagnostics"
        rc = dd.main([
            "--date", "2026-08-06",
            "--state-home", str(tmp_path / "state"),
            "--openclaw-db", str(tmp_path / "no.sqlite"),
            "--openclaw-log-dir", str(tmp_path / "nolog"),
            "--archive", "--out-dir", str(out),
        ])

        assert rc == 0
        assert (out / "2026-08-06.md").exists()
        printed = capsys.readouterr().out
        # 调度器拿到的是一行摘要，不是整份报告——artifact 有 max_output_chars 上限。
        assert printed.count("\n") == 1
        assert "诊断报告 2026-08-06" in printed
        assert "# A股系统每日运行诊断" not in printed


class TestReport:
    def test_report_is_self_contained_and_starts_with_the_fingerprint(self, tmp_path):
        report = dd.build_report(
            "2026-08-06",
            str(tmp_path / "state"),
            str(tmp_path / "no.sqlite"),
            str(tmp_path / "nolog"),
        )

        assert report.startswith("# A股系统每日运行诊断 · 2026-08-06")
        # 指纹必须在所有数字之前：分不清哪台机器/哪个版本，后面全是误读。
        assert report.index("## 1. 环境指纹") < report.index("## 2. Hermes")
        for heading in ("## 3. OpenClaw", "## 4. 作业注册漂移", "## 5. 证据摘录"):
            assert heading in report


class TestDriftCharacterization:
    """`section_drift` 的四条分支逐字定桩。

    抽取结构化的 `collect_drift` 之前先把渲染结果钉死：日报是人读的，
    换行与措辞变了没人会立刻发现，但排障时的可读性就是它的全部价值。
    """

    @staticmethod
    def _manifest(tmp_path, jobs):
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
        return str(path)

    def test_unreadable_manifest(self, tmp_path):
        path = str(tmp_path / "missing.json")
        assert dd.section_drift(path, {"available": True, "jobs": []}) == [
            "## 4. 作业注册漂移",
            "",
            f"未能读取 manifest `{path}`。",
            "",
        ]

    def test_openclaw_registry_unavailable(self, tmp_path):
        path = self._manifest(tmp_path, [{"id": "a", "enabled": True}])
        assert dd.section_drift(path, {"available": False}) == [
            "## 4. 作业注册漂移",
            "",
            "manifest enabled 作业 **1** 个。",
            "",
            "未能读取 OpenClaw 注册表，跳过比对。",
            "",
        ]

    def test_no_drift(self, tmp_path):
        path = self._manifest(tmp_path, [
            {"id": "a", "enabled": True},
            {"id": "b", "enabled": False},
        ])
        openclaw = {"available": True, "jobs": [{"job_id": "A-stock: a"}]}
        assert dd.section_drift(path, openclaw) == [
            "## 4. 作业注册漂移",
            "",
            "manifest enabled 作业 **1** 个。",
            "OpenClaw 注册 **1** 个。",
            "",
            "两边一致，无漂移。",
            "",
        ]

    def test_drift_in_both_directions(self, tmp_path):
        path = self._manifest(tmp_path, [
            {"id": "a", "enabled": True},
            {"id": "z", "enabled": True},
        ])
        openclaw = {"available": True, "jobs": [{"name": "A-stock: a"}, {"name": "ghost"}]}
        assert dd.section_drift(path, openclaw) == [
            "## 4. 作业注册漂移",
            "",
            "manifest enabled 作业 **2** 个。",
            "OpenClaw 注册 **2** 个。",
            "",
            "**manifest 里 enabled 但 OpenClaw 未注册**（改了没同步注册？）：",
            "",
            "- `z`",
            "",
            "**OpenClaw 注册了但不在 manifest enabled 列表**"
            "（非仓库作业，或 manifest 已下线）：",
            "",
            "- `ghost`",
            "",
        ]


class TestCollectDrift:
    """结构化结论：开盘前体检按它判 red/green，所以状态字段必须明确。"""

    @staticmethod
    def _manifest(tmp_path, jobs):
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
        return str(path)

    def test_drift_is_reported_with_both_directions(self, tmp_path):
        path = self._manifest(tmp_path, [
            {"id": "a", "enabled": True},
            {"id": "z", "enabled": True},
        ])
        drift = dd.collect_drift(
            path, {"available": True, "jobs": [{"name": "A-stock: a"}, {"name": "ghost"}]}
        )

        assert drift["status"] == "drift"
        assert drift["enabled_count"] == 2
        assert drift["registered_count"] == 2
        assert drift["missing"] == ["z"]
        assert drift["extra"] == ["ghost"]
        assert drift["unavailable_at"] is None

    def test_clean_registration_is_ok(self, tmp_path):
        path = self._manifest(tmp_path, [{"id": "a", "enabled": True}])
        drift = dd.collect_drift(path, {"available": True, "jobs": [{"job_id": "a"}]})

        assert drift["status"] == "ok"
        assert drift["missing"] == drift["extra"] == []

    def test_unavailable_says_which_side_could_not_be_read(self, tmp_path):
        missing_manifest = dd.collect_drift(
            str(tmp_path / "nope.json"), {"available": True, "jobs": [{"job_id": "a"}]}
        )
        assert missing_manifest["status"] == "unavailable"
        assert missing_manifest["unavailable_at"] == "manifest"

        path = self._manifest(tmp_path, [{"id": "a", "enabled": True}])
        no_registry = dd.collect_drift(path, {"available": False})
        assert no_registry["status"] == "unavailable"
        assert no_registry["unavailable_at"] == "openclaw"
        # 读不到注册表 != 没有漂移，不能让体检误判成绿
        assert no_registry["missing"] == []


class TestSeverityFromManifest:
    """严重度不该靠拍脑袋写死一张作业清单 —— manifest 里本来就写着谁被谁依赖。"""

    @staticmethod
    def _manifest(tmp_path, jobs):
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
        return str(path)

    def test_required_dependents_ignores_optional_and_disabled(self, tmp_path):
        path = self._manifest(tmp_path, [
            {"id": "up", "enabled": True, "context_from": []},
            {"id": "hard", "enabled": True, "context_from": ["up"]},
            {
                "id": "soft", "enabled": True, "context_from": ["up"],
                "dependency_policy": {"optional_jobs": ["up"]},
            },
            {"id": "off", "enabled": False, "context_from": ["up"]},
        ])

        assert dd.required_dependents(path) == {"up": ["hard"]}

    def test_a_job_with_hard_dependents_is_p0(self):
        assert dd._job_severity("up", {"up": ["hard"]}) == "P0"
        assert dd._job_severity("leaf", {"up": ["hard"]}) == "P1"


class TestCollectFindings:
    @staticmethod
    def _rows(**overrides):
        row = {
            "job_id": "auction-snapshot", "claimed": 3, "started": 3, "finished": 3,
            "statuses": ["timeout"], "unhealthy": ["timeout"],
            "max_duration": 180.0, "never_started": False,
        }
        row.update(overrides)
        return [row]

    def test_finding_keys_are_stable_across_days(self):
        """key 里不能带日期/run_id/耗时，否则聚合会把同一个问题算成天天都是新的。"""
        first = dd.collect_findings(
            rows=self._rows(max_duration=180.0), meta={"delivery": {}},
            openclaw={}, drift={}, gateway={}, dependents={},
        )
        second = dd.collect_findings(
            rows=self._rows(max_duration=42.0, claimed=9), meta={"delivery": {}},
            openclaw={}, drift={}, gateway={}, dependents={},
        )

        assert [item["key"] for item in first] == [item["key"] for item in second]
        assert first[0]["key"] == "job_unhealthy:auction-snapshot:timeout"

    def test_chain_job_failure_is_p0_and_carries_its_downstream(self):
        findings = dd.collect_findings(
            rows=self._rows(), meta={"delivery": {}}, openclaw={}, drift={}, gateway={},
            dependents={"auction-snapshot": ["auction-finalize"]},
        )

        assert findings[0]["severity"] == "P0"
        assert findings[0]["downstream"] == ["auction-finalize"]

    def test_unregistered_enabled_job_is_p0(self):
        findings = dd.collect_findings(
            rows=[], meta={"delivery": {}}, openclaw={},
            drift={"missing": ["ghost-job"], "extra": ["stray"]}, gateway={}, dependents={},
        )
        by_kind = {item["kind"]: item for item in findings}

        assert by_kind["registration_missing"]["severity"] == "P0"
        assert by_kind["registration_extra"]["severity"] == "P2"

    def test_undelivered_alerts_are_reported(self):
        findings = dd.collect_findings(
            rows=[], meta={"delivery": {("delivery.failed", "not_configured"): 4}},
            openclaw={}, drift={}, gateway={}, dependents={},
        )

        assert findings[0]["key"] == "delivery_failed:not_configured"
        assert findings[0]["count"] == 4


def _daily(date, findings, observed):
    return {
        "schema": "a_stock_diagnostics_daily_v1",
        "date": date,
        "observed_subjects": observed,
        "findings": findings,
    }


def _finding(key, subject="auction-snapshot", severity="P0"):
    return {
        "key": key, "kind": "job_unhealthy", "subject": subject,
        "severity": severity, "detail": key, "count": 1,
    }


class TestRollup:
    def test_new_recurring_and_resolved_are_separated(self):
        reports = [
            _daily("2026-06-01", [_finding("a"), _finding("gone")], ["auction-snapshot"]),
            _daily("2026-06-02", [_finding("a"), _finding("b")], ["auction-snapshot"]),
        ]

        rollup = dd.build_rollup(reports, days=2)

        assert [item["key"] for item in rollup["recurring"]] == ["a"]
        assert [item["key"] for item in rollup["new"]] == ["b"]
        assert [item["key"] for item in rollup["resolved"]] == ["gone"]
        assert rollup["status"] == "ok"

    def test_first_seen_last_seen_and_occurrences_are_tracked(self):
        reports = [
            _daily("2026-06-01", [_finding("a")], ["auction-snapshot"]),
            _daily("2026-06-02", [], ["auction-snapshot"]),
            _daily("2026-06-03", [_finding("a")], ["auction-snapshot"]),
        ]

        entry = dd.build_rollup(reports, days=3)["recurring"][0]

        assert entry["first_seen"] == "2026-06-01"
        assert entry["last_seen"] == "2026-06-03"
        assert entry["occurrences"] == 2

    def test_a_finding_that_vanished_because_the_job_never_ran_is_not_resolved(self):
        """「消失」不等于「修好」—— 这是「已验证修复」四个字的全部重量。

        作业当天压根没跑，它的异常自然不会再出现；把这当成修复，就是用空集
        证明通过。
        """
        reports = [
            _daily("2026-06-01", [_finding("a")], ["auction-snapshot"]),
            _daily("2026-06-02", [], []),   # 当天没观测到任何作业
        ]

        rollup = dd.build_rollup(reports, days=2)

        assert rollup["resolved"] == []
        assert [item["key"] for item in rollup["unverified"]] == ["a"]
        assert "未被观测到" in rollup["unverified"][0]["unverified_reason"]
        assert rollup["unverified"][0]["fix_status"] == "unverified"

    def test_short_window_is_reported_as_partial_not_silently_ok(self):
        """5 个交易日的标准不能被 1 份报告糊过去。"""
        rollup = dd.build_rollup(
            [_daily("2026-06-01", [_finding("a")], ["auction-snapshot"])], days=5
        )

        assert rollup["status"] == "partial"
        assert "只有 1 份" in rollup["reason"]

    def test_no_reports_at_all_is_insufficient_data(self):
        rollup = dd.build_rollup([], days=5)

        assert rollup["status"] == "insufficient_data"
        assert rollup["counts"] if "counts" in rollup else True
        assert rollup["new"] == rollup["recurring"] == []

    def test_severity_counts_exclude_resolved(self):
        reports = [
            _daily("2026-06-01", [_finding("a"), _finding("gone")], ["auction-snapshot"]),
            _daily("2026-06-02", [_finding("a")], ["auction-snapshot"]),
        ]

        assert dd.build_rollup(reports, days=2)["severity_counts"]["P0"] == 1


class TestArchiveWritesBothFormats:
    def test_archive_emits_markdown_and_structured_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dd, "build_report", lambda *a, **k: "# 报告\n\n**异常 0 个**\n")
        monkeypatch.setattr(
            dd, "build_structured_report",
            lambda *a, **k: {
                "schema": "a_stock_diagnostics_daily_v1", "date": "2026-06-23",
                "severity_counts": {"P0": 1, "P1": 0, "P2": 2}, "findings": [],
            },
        )
        out_dir = tmp_path / "diagnostics"

        code = dd.main([
            "--archive", "--date", "2026-06-23",
            "--state-home", str(tmp_path), "--out-dir", str(out_dir),
        ])

        assert code == 0
        assert (out_dir / "2026-06-23.md").exists()
        payload = json.loads((out_dir / "2026-06-23.json").read_text(encoding="utf-8"))
        assert payload["schema"] == "a_stock_diagnostics_daily_v1"

    def test_pruning_covers_the_json_twin(self, tmp_path):
        out_dir = tmp_path / "diagnostics"
        out_dir.mkdir()
        for name in ("2026-05-01.md", "2026-05-01.json", "2026-06-23.md", "2026-06-23.json"):
            (out_dir / name).write_text("x", encoding="utf-8")
        (out_dir / "2026-05-01-incident.md").write_text("keep", encoding="utf-8")

        removed = dd.prune_reports(str(out_dir), 30, "2026-06-23")

        assert sorted(removed) == ["2026-05-01.json", "2026-05-01.md"]
        # 手工另存的存档不按日期命名，绝不能被顺手删掉
        assert (out_dir / "2026-05-01-incident.md").exists()


def test_archive_collects_once_for_both_outputs(tmp_path, monkeypatch):
    """归档要出两份产物，但 6.5MB 的 trace 与 sqlite 只能读一遍。

    这个作业是 60s 的 short 档，分别采集会把预算直接翻倍。
    """
    calls = []

    def _collect_all(day, state_home, openclaw_db, log_dir):
        calls.append(day)
        return {
            "day": day, "state_home": state_home, "log_dir": log_dir,
            "manifest_path": "m", "rows": [], "meta": {"events": 0, "delivery": {}},
            "openclaw": {"available": False, "path": "x", "runs": [], "jobs": []},
            "drift": {"status": "unavailable"}, "gateway": {"status": "unavailable", "counts": {}},
            "dependents": {},
        }

    monkeypatch.setattr(dd, "collect_all", _collect_all)
    out_dir = tmp_path / "diagnostics"

    dd.main([
        "--archive", "--date", "2026-06-23",
        "--state-home", str(tmp_path), "--out-dir", str(out_dir),
    ])

    assert calls == ["2026-06-23"]
    assert (out_dir / "2026-06-23.md").exists()
    assert (out_dir / "2026-06-23.json").exists()
