"""飞书文档出口的免责声明（与消息出口 feishu_push 共用同一份披露）。

背景：飞书有两类出口——聊天消息走 skills/common/feishu_push.push_text，研究文档走
skills/stock-triage/scripts/serenity_to_feishu.py 的 lark-cli docs +create。只在消息
出口加披露会留下一半合规缺口，本文件锁定文档出口同样带披露且文案与消息出口一致。
"""

import importlib.util
import os

from skills.common import feishu_push

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def load_serenity():
    spec = importlib.util.spec_from_file_location(
        "serenity_to_feishu_disclosure_test",
        os.path.join(ROOT, "skills", "stock-triage", "scripts", "serenity_to_feishu.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_doc_egress_shares_message_egress_disclosure():
    """文案单一事实源：文档出口直接复用 feishu_push.DISCLOSURE，杜绝两处漂移。"""
    assert load_serenity().DISCLOSURE == feishu_push.DISCLOSURE


def test_disclosure_appended_to_doc_body():
    serenity = load_serenity()
    assert serenity.with_disclosure("# 深度分析\n结论：观望").endswith(feishu_push.DISCLOSURE)


def test_disclosure_not_duplicated():
    serenity = load_serenity()
    once = serenity.with_disclosure("正文")
    assert serenity.with_disclosure(once) == once
    assert once.count(feishu_push.DISCLOSURE) == 1


def test_created_doc_content_carries_disclosure(tmp_path, monkeypatch):
    """端到端：走 create_feishu_doc，断言真正落到 lark-cli 的 markdown 文件里带披露。

    只 mock subprocess，不 mock 内容组装——否则测的是 mock 不是行为。
    """
    serenity = load_serenity()
    monkeypatch.setenv(feishu_push.EGRESS_ENABLED_ENV, "true")
    captured = {}

    class _Completed:
        returncode = 0
        stdout = '{"data": {"doc_url": "https://example.feishu.cn/docx/abc"}}'
        stderr = ""

    def _fake_run(cmd, **kwargs):
        # lark-cli docs +create --title <t> --markdown <tmpfile>
        captured["cmd"] = cmd
        captured["body"] = open(cmd[cmd.index("--markdown") + 1], encoding="utf-8").read()
        return _Completed()

    monkeypatch.setattr(serenity.subprocess, "run", _fake_run)

    url = serenity.create_feishu_doc("测试标题", "# 报告\n| 列A | 列B |\n|---|---|\n| 1 | 2 |")

    assert url == "https://example.feishu.cn/docx/abc"
    assert captured["cmd"][:3] == ["lark-cli", "docs", "+create"]
    assert captured["body"].rstrip().endswith(feishu_push.DISCLOSURE)
    # 披露不能破坏既有的表格清洗行为
    assert "• 列A — 列B" in captured["body"]


def test_doc_creation_is_disabled_by_default(monkeypatch):
    serenity = load_serenity()
    monkeypatch.delenv(feishu_push.EGRESS_ENABLED_ENV, raising=False)
    monkeypatch.setattr(
        serenity.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    assert serenity.create_feishu_doc("测试标题", "# 报告") is None
