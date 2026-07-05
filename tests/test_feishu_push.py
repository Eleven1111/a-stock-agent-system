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
    text_arg = calls[0][calls[0].index("--text") + 1]
    assert text_arg == f"国务院发布新政策\n{feishu_push.DISCLOSURE}"


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


def _capture_text(monkeypatch):
    calls = []

    class _Completed:
        returncode = 0
        stderr = ""

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Completed()

    monkeypatch.setattr(feishu_push.subprocess, "run", _fake_run)
    return calls


def _sent_text(calls):
    cmd = calls[0]
    return cmd[cmd.index("--text") + 1]


def test_disclosure_appended_without_trailing_newline(monkeypatch):
    monkeypatch.setenv(feishu_push.CHAT_ID_ENV, "oc_test123")
    calls = _capture_text(monkeypatch)

    feishu_push.push_text("capital-flow", "北向净流入 12.3 亿")

    assert _sent_text(calls) == f"北向净流入 12.3 亿\n{feishu_push.DISCLOSURE}"


def test_disclosure_appended_with_existing_trailing_newline(monkeypatch):
    monkeypatch.setenv(feishu_push.CHAT_ID_ENV, "oc_test123")
    calls = _capture_text(monkeypatch)

    feishu_push.push_text("capital-flow", "北向净流入 12.3 亿\n")

    sent = _sent_text(calls)
    assert sent == f"北向净流入 12.3 亿\n{feishu_push.DISCLOSURE}"
    assert "\n\n" not in sent


def test_disclosure_not_duplicated_when_already_present(monkeypatch):
    monkeypatch.setenv(feishu_push.CHAT_ID_ENV, "oc_test123")
    calls = _capture_text(monkeypatch)

    original = f"自定义提醒\n{feishu_push.DISCLOSURE}"
    feishu_push.push_text("news-l2-breaking", original)

    sent = _sent_text(calls)
    assert sent == original
    assert sent.count(feishu_push.DISCLOSURE) == 1


def test_whitespace_only_text_still_skipped_and_no_footer_only_send(monkeypatch):
    monkeypatch.setenv(feishu_push.CHAT_ID_ENV, "oc_test123")
    calls = _capture_text(monkeypatch)

    result = feishu_push.push_text("event-calendar", "   \n  ")

    assert result == {"status": "empty", "job_id": "event-calendar"}
    assert calls == []
