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
