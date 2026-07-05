"""Runtime path and alert transaction regressions."""

import importlib.util
import os
import subprocess
import sys

from http_client import HttpResult
from runtime_context import resolve_runtime_name
from state_store import atomic_write_json, read_json, update_json_list


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def test_runtime_name_prefers_explicit_and_detects_host_runtime():
    assert resolve_runtime_name("openclaw", {"HERMES_HOME": "/tmp/hermes"}) == "openclaw"
    assert resolve_runtime_name(env={"OPENCLAW_HOME": "/tmp/openclaw"}) == "openclaw"
    assert resolve_runtime_name(env={"HERMES_HOME": "/tmp/hermes"}) == "hermes"
    assert resolve_runtime_name(env={}) == "local"


def load_module(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_scripts_honor_hermes_home_for_paths(tmp_path, monkeypatch):
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME（见 paths.hermes_home()），
    # conftest 为隔离测试状态无条件设置了它，这里要测的是 HERMES_HOME 回退
    # 路径，必须显式清掉 A_STOCK_STATE_HOME 才能观察到 HERMES_HOME 生效。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PYTHON", raising=False)

    check_alerts = load_module(
        "check_alerts_paths_test",
        "skills/a-stock-commands/scripts/check_alerts.py",
    )
    serenity = load_module(
        "serenity_to_feishu_paths_test",
        "skills/stock-triage/scripts/serenity_to_feishu.py",
    )
    fundamentals = load_module(
        "fundamentals_paths_test",
        "skills/stock-analyst/scripts/fundamentals.py",
    )

    assert check_alerts.ALERTS_FILE == os.path.join(str(tmp_path), "cron", "output", "alerts.json")
    assert serenity.REPORT_ROOT == os.path.join(str(tmp_path), "cron", "output")
    assert fundamentals.HERMES_PYTHON == os.path.join(
        str(tmp_path), "hermes-agent", "venv", "bin", "python3",
    )


def test_runtime_scripts_honor_env_file_under_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("SERPER_API_KEYS", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    (tmp_path / ".env").write_text(
        "SERPER_API_KEYS=key1,key2\nNO_PROXY=.eastmoney.com\n",
        encoding="utf-8",
    )

    news = load_module(
        "stock_news_paths_test",
        "skills/stock-analyst/scripts/news.py",
    )
    load_module(
        "capital_flow_paths_test",
        "skills/stock-triage/scripts/capital_flow_monitor.py",
    )

    assert os.environ["SERPER_API_KEYS"] == "key1,key2"
    assert news._next_serper_key() in {"key1", "key2"}
    assert os.environ["NO_PROXY"] == ".eastmoney.com"


def test_hermes_python_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PYTHON", "/custom/python3")

    fundamentals = load_module(
        "fundamentals_python_override_test",
        "skills/stock-analyst/scripts/fundamentals.py",
    )

    assert fundamentals.HERMES_PYTHON == "/custom/python3"


def test_check_alerts_runs_directly_with_hermes_home(tmp_path):
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            os.path.join(ROOT, "skills", "a-stock-commands", "scripts", "check_alerts.py"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_check_alerts_price_fetch_uses_shared_http_client(monkeypatch):
    check_alerts = load_module(
        "check_alerts_http_client_test",
        "skills/a-stock-commands/scripts/check_alerts.py",
    )

    def fake_request_text(url, **kwargs):
        assert url == "http://qt.gtimg.cn/q=sh600001"
        assert kwargs == {
            "source": "tencent",
            "timeout": 10,
            "encoding": "gbk",
            "headers": {"User-Agent": "Mozilla/5.0"},
        }
        return HttpResult('v_sh600001="1~示例~600001~12.34~";', "2026-06-12T06:00:00+00:00", 1)

    monkeypatch.setattr(check_alerts, "request_text", fake_request_text)

    assert check_alerts.get_price("600001") == 12.34


def test_check_alerts_price_fetch_preserves_zero_fallback(monkeypatch):
    check_alerts = load_module(
        "check_alerts_http_error_test",
        "skills/a-stock-commands/scripts/check_alerts.py",
    )
    monkeypatch.setattr(
        check_alerts,
        "request_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("slow")),
    )

    assert check_alerts.get_price("000001") == 0.0


def test_check_alerts_preserves_concurrent_append(tmp_path, monkeypatch, capsys):
    check_alerts = load_module(
        "check_alerts_transaction_test",
        "skills/a-stock-commands/scripts/check_alerts.py",
    )
    alerts_file = str(tmp_path / "alerts.json")
    monkeypatch.setattr(check_alerts, "ALERTS_FILE", alerts_file)

    atomic_write_json(alerts_file, [
        {
            "code": "600001",
            "name": "旧提醒",
            "type": "breakout",
            "price": 10.0,
            "active": True,
        }
    ])

    appended = False

    def fake_get_price(code):
        nonlocal appended
        if not appended:
            update_json_list(alerts_file, {
                "code": "600002",
                "name": "并发新增",
                "type": "breakout",
                "price": 20.0,
                "active": True,
            })
            appended = True
        return 12.0 if code == "600001" else 0.0

    monkeypatch.setattr(check_alerts, "get_price", fake_get_price)

    check_alerts.main()

    captured = capsys.readouterr()
    assert "SIGNAL: ALERT_TRIGGERED" in captured.out

    alerts = read_json(alerts_file, [])
    assert {a["code"] for a in alerts} == {"600001", "600002"}
    old_alert = next(a for a in alerts if a["code"] == "600001")
    new_alert = next(a for a in alerts if a["code"] == "600002")
    assert old_alert["active"] is False
    assert old_alert["trigger_price"] == 12.0
    assert new_alert["active"] is True
