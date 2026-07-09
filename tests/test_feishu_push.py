from skills.common import feishu_push


def test_not_configured_when_chat_id_unset(monkeypatch):
    monkeypatch.delenv(feishu_push.CHAT_ID_ENV, raising=False)

    result = feishu_push.push_text("capital-flow", "北向净流入 12.3 亿")

    assert result == {"status": "not_configured", "job_id": "capital-flow"}


def test_empty_text_is_skipped(monkeypatch):
    monkeypatch.setenv(feishu_push.CHAT_ID_ENV, "oc_test123")

    result = feishu_push.push_text("event-calendar", "   ")

    assert result == {"status": "empty", "job_id": "event-calendar"}


def test_sends_via_lark_cli_when_configured(monkeypatch):
    monkeypatch.setenv(feishu_push.CHAT_ID_ENV, "oc_test123")
    calls = []

    class _Completed:
        returncode = 0
        stderr = ""

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Completed()

    monkeypatch.setattr(feishu_push.subprocess, "run", _fake_run)

    result = feishu_push.push_text("official-policy-watch", "国务院发布新政策")

    assert result == {"status": "sent", "job_id": "official-policy-watch"}
    assert calls[0][:3] == ["lark-cli", "im", "+messages-send"]
    assert "--chat-id" in calls[0] and "oc_test123" in calls[0]
    assert "--text" in calls[0] and "国务院发布新政策" in calls[0]


def test_reports_failure_without_raising(monkeypatch):
    monkeypatch.setenv(feishu_push.CHAT_ID_ENV, "oc_test123")

    class _Completed:
        returncode = 1
        stderr = "permission denied"

    monkeypatch.setattr(feishu_push.subprocess, "run", lambda cmd, **kwargs: _Completed())

    result = feishu_push.push_text("news-monitor", "资讯摘要")

    assert result["status"] == "failed"
    assert "permission denied" in result["error"]


def test_subprocess_exception_reports_failure(monkeypatch):
    monkeypatch.setenv(feishu_push.CHAT_ID_ENV, "oc_test123")

    def _raise(cmd, **kwargs):
        raise FileNotFoundError("lark-cli not found")

    monkeypatch.setattr(feishu_push.subprocess, "run", _raise)

    result = feishu_push.push_text("news-monitor-intraday", "盘中资讯")

    assert result["status"] == "failed"
    assert "lark-cli not found" in result["error"]
