"""开盘前体检的测试。

重点只有一条：**体检不能把「不知道」报成绿**。2026-08-18 那次就是「看起来
没有告警」被当成了「没有故障」，而真相是告警根本没有出口。所以这里覆盖的
全是「读不到 / 检查项自己炸了 / 未知状态」这些边界，而不是happy path。
"""

import json

import pytest

from scripts import preopen_preflight as pf


class TestSeverityRollup:
    def test_worst_takes_the_most_severe(self):
        assert pf._worst(["ok", "ok"]) == "ok"
        assert pf._worst(["ok", "warn"]) == "warn"
        assert pf._worst(["warn", "red", "ok"]) == "red"

    def test_unknown_status_is_treated_as_red(self):
        """未知状态不能获得静默通道 —— 拼错一个字符串不该变成绿灯。"""
        assert pf._worst(["ok", "mystery"]) == "red"


class TestGuard:
    def test_a_crashing_check_becomes_red_not_a_crashed_report(self):
        """体检工具自己崩掉等于没有体检，所以单项失败必须被隔离。"""
        def _boom():
            raise RuntimeError("probe exploded")

        result = pf._guard("x", "示例", _boom)

        assert result["status"] == "red"
        assert "probe exploded" in result["reason"]
        assert result["name"] == "x"

    def test_a_check_without_status_defaults_to_red(self):
        result = pf._guard("x", "示例", lambda: {"detail": {}})

        assert result["status"] == "red"


class TestDeliveryCheck:
    @staticmethod
    def _manifest(tmp_path, jobs):
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
        return str(path)

    def test_declared_push_without_a_configured_target_is_red(self, tmp_path, monkeypatch):
        """告警生成了却送不出去，是 issue #239 里 delivery.failed 那条的根因。"""
        monkeypatch.delenv("A_STOCK_FEISHU_CHAT_ID", raising=False)
        path = self._manifest(tmp_path, [
            {"id": "watch", "enabled": True, "deliver": "feishu_direct"},
        ])

        result = pf.check_delivery(path)

        assert result["status"] == "red"
        assert "A_STOCK_FEISHU_CHAT_ID" in result["reason"]
        assert result["detail"]["feishu_jobs"] == ["watch"]

    def test_no_push_jobs_means_nothing_to_check(self, tmp_path, monkeypatch):
        monkeypatch.delenv("A_STOCK_FEISHU_CHAT_ID", raising=False)
        path = self._manifest(tmp_path, [{"id": "a", "enabled": True, "deliver": "local"}])

        assert pf.check_delivery(path)["status"] == "ok"

    def test_disabled_push_jobs_do_not_demand_a_target(self, tmp_path, monkeypatch):
        monkeypatch.delenv("A_STOCK_FEISHU_CHAT_ID", raising=False)
        path = self._manifest(tmp_path, [
            {"id": "off", "enabled": False, "deliver": "feishu_direct"},
        ])

        assert pf.check_delivery(path)["status"] == "ok"


class TestRegistrationCheck:
    def test_unreadable_registry_is_warn_never_green(self, tmp_path):
        """读不到注册表 != 两边一致。这一条错了，整个体检就成了摆设。"""
        manifest = tmp_path / "manifest.json"
        manifest.write_text(
            json.dumps({"jobs": [{"id": "a", "enabled": True}]}), encoding="utf-8"
        )

        result = pf.check_registration(
            str(manifest), str(tmp_path / "nonexistent.sqlite"), "2026-06-23"
        )

        assert result["status"] == "warn"
        assert result["detail"]["unavailable_at"] == "openclaw"


class TestGatewayLogCheck:
    def test_missing_log_is_warn_not_ok(self, tmp_path):
        result = pf.check_gateway_log(str(tmp_path / "nope"), "2026-06-23")

        assert result["status"] == "warn"

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("HTTP 401 Unauthorized calling model", "model_auth"),
            ("error: 402 Insufficient Balance", "model_balance"),
            ("listen EADDRINUSE: address already in use :::8080", "port_conflict"),
        ],
    )
    def test_known_gateway_failures_are_red(self, tmp_path, line, expected):
        log_dir = tmp_path / "openclaw"
        log_dir.mkdir()
        (log_dir / "openclaw-2026-06-23.log").write_text(line + "\n", encoding="utf-8")

        result = pf.check_gateway_log(str(log_dir), "2026-06-23")

        assert result["status"] == "red"
        assert result["detail"]["counts"][expected] == 1

    def test_a_clean_log_is_ok(self, tmp_path):
        log_dir = tmp_path / "openclaw"
        log_dir.mkdir()
        (log_dir / "openclaw.log").write_text("all good\n", encoding="utf-8")

        result = pf.check_gateway_log(str(log_dir), "2026-06-23")

        assert result["status"] == "ok"
        assert result["detail"]["counts"] == {
            "model_auth": 0, "model_balance": 0, "port_conflict": 0
        }

    def test_credentials_in_the_log_are_redacted_before_they_reach_the_artifact(
        self, tmp_path
    ):
        """体检结果会被推出去，日志原文里的 key 绝不能跟着走。"""
        log_dir = tmp_path / "openclaw"
        log_dir.mkdir()
        (log_dir / "openclaw.log").write_text(
            "401 Unauthorized sk-abcdef0123456789ABCDEF\n", encoding="utf-8"
        )

        result = pf.check_gateway_log(str(log_dir), "2026-06-23")

        blob = json.dumps(result, ensure_ascii=False)
        assert "sk-abcdef0123456789ABCDEF" not in blob
        assert "REDACTED" in blob


class TestAuctionSourceCheck:
    def test_missing_easy_tdx_is_red(self, monkeypatch):
        """easy_tdx 是 09:15-09:25 竞价的唯一真源，缺它整条链只能降级。"""
        import builtins

        real_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "easy_tdx":
                raise ImportError("no easy_tdx")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)

        result = pf.check_auction_sources(probe=False)

        assert result["status"] == "red"
        assert result["detail"]["easy_tdx_importable"] is False

    def test_probe_disabled_reports_reachability_as_unknown_not_true(self):
        """可导入 != 连通。字段必须如实区分，否则体检在撒谎。"""
        result = pf.check_auction_sources(probe=False)

        assert result["detail"]["easy_tdx_reachable"] is None


class TestReportShape:
    def test_all_green_carries_no_signal_so_the_job_stays_silent(self, monkeypatch):
        for name in (
            "check_config", "check_state", "check_registration",
            "check_delivery", "check_auction_sources", "check_gateway_log",
        ):
            monkeypatch.setattr(
                pf, name, lambda *a, **k: {"status": "ok", "reason": None, "detail": {}}
            )

        report = pf.run_preflight(
            day="2026-06-23", runtime="hermes", manifest_path="m",
            openclaw_db="db", log_dir="logs",
        )

        assert report["status"] == "ok"
        assert report["has_signal"] is False
        assert report["alerts"] == []
        assert report["summary"] == {"total": 6, "red": 0, "warn": 0}

    def test_one_red_surfaces_in_alerts_and_raises_the_signal(self, monkeypatch):
        monkeypatch.setattr(
            pf, "check_config",
            lambda *a, **k: {"status": "red", "reason": "配置炸了", "detail": {}},
        )
        for name in (
            "check_state", "check_registration", "check_delivery",
            "check_auction_sources", "check_gateway_log",
        ):
            monkeypatch.setattr(
                pf, name, lambda *a, **k: {"status": "ok", "reason": None, "detail": {}}
            )

        report = pf.run_preflight(
            day="2026-06-23", runtime="hermes", manifest_path="m",
            openclaw_db="db", log_dir="logs",
        )

        assert report["status"] == "red"
        assert report["has_signal"] is True
        assert [item["name"] for item in report["alerts"]] == ["config"]
        assert report["summary"]["red"] == 1

    def test_a_failing_check_does_not_take_down_the_others(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(pf, "check_state", _boom)
        for name in (
            "check_config", "check_registration", "check_delivery",
            "check_auction_sources", "check_gateway_log",
        ):
            monkeypatch.setattr(
                pf, name, lambda *a, **k: {"status": "ok", "reason": None, "detail": {}}
            )

        report = pf.run_preflight(
            day="2026-06-23", runtime="hermes", manifest_path="m",
            openclaw_db="db", log_dir="logs",
        )

        assert len(report["checks"]) == 6
        assert report["status"] == "red"
        assert [item["name"] for item in report["alerts"]] == ["state"]


def test_cli_never_returns_nonzero_so_it_cannot_block_the_chain(monkeypatch, capsys):
    """体检发现问题 != 本次运行失败。

    返回非 0 会让 DAG 把它当失败依赖，反而挡住后面的链 —— 一个用来防止链路
    停摆的作业，自己变成停摆的原因，是最糟糕的形态。
    """
    monkeypatch.setattr(
        pf, "run_preflight",
        lambda **kwargs: {"schema": "x", "status": "red", "has_signal": True},
    )

    assert pf.main(["--json", "--no-probe"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "red"
