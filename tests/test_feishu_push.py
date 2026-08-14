import json

import pytest

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


def test_push_text_normalises_raw_json_at_egress_boundary(monkeypatch):
    monkeypatch.setenv(feishu_push.CHAT_ID_ENV, "oc_test123")
    calls = _capture_text(monkeypatch)

    feishu_push.push_text(
        "capital-flow",
        '{"status":"degraded","summary":"资金流正常","alerts":[]}',
    )

    assert _sent_text(calls) == f"资金流正常\n{feishu_push.DISCLOSURE}"


def test_render_capital_flow_payload_as_summary_instead_of_json():
    rendered = feishu_push.render_delivery_text(
        "capital-flow",
        '{"schema":"capital_flow_v2","northbound":{"net_flow_yi":420},'
        '"stocks":[{"name":"韩建河山"}],"sectors":[{"name":"半导体"}],'
        '"alerts":[{"level":"🟡","msg":"换手率异常"}]}',
        500,
    )

    assert "资金流向：北向+420.0亿" in rendered
    assert "换手率异常" in rendered
    assert "capital_flow_v2" not in rendered
    assert "{\"schema\"" not in rendered


@pytest.mark.parametrize(
    ("job_id", "window", "label"),
    [
        (
            "hot-money-morning-checkpoint",
            "09:50",
            "09:50主线龙头承接确认",
        ),
        (
            "hot-money-afternoon-checkpoint",
            "13:15",
            "13:15主线龙头回流确认",
        ),
    ],
)
def test_render_hot_money_checkpoint_payload_for_both_windows(
    job_id, window, label
):
    rendered = feishu_push.render_artifact_text(
        {
            "job_id": job_id,
            "stdout": json.dumps(
                {
                    "status": "ready",
                    "profile": "checkpoint",
                    "window": window,
                    "research_only": True,
                    "observation_count": 2,
                    "confirmed_count": 1,
                    "confirmed": [
                        {"code": "600000", "name": "浦发银行"},
                    ],
                },
                ensure_ascii=False,
            ),
        },
        1000,
    )

    assert label in rendered
    assert "观察数量：2" in rendered
    assert "确认数量：1" in rendered
    assert "浦发银行（600000）" in rendered
    assert "研究专用" in rendered
    assert "不可交易" in rendered
    assert '"confirmed"' not in rendered


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
