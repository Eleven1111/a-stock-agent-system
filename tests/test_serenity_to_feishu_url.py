"""飞书文档 URL 校验 —— 替代原「整行含 feishu.cn 就当链接」的弱判断。

原实现命中 CodeQL py/incomplete-url-substring-sanitization：域名字符串出现在
URL 的任意位置（查询串、路径、子域前缀）都会被放行，且返回的是整行文本而非 URL。
"""

import os
import sys

import pytest

SCRIPT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills", "stock-triage", "scripts",
)
sys.path.insert(0, SCRIPT_DIR)

import serenity_to_feishu as sf  # noqa: E402


@pytest.mark.parametrize("url", [
    "https://example.feishu.cn/docx/abc123",
    "https://feishu.cn/docx/abc123",
    "http://example.feishu.cn/docx/abc123",
    "https://foo.larksuite.com/docx/abc123",
    "https://EXAMPLE.FEISHU.CN/docx/abc123",
])
def test_accepts_real_feishu_hosts(url):
    assert sf.is_feishu_doc_url(url) is True


@pytest.mark.parametrize("url", [
    # 域名出现在查询串里 —— 旧的子串判断会放行
    "https://evil.example/?ref=feishu.cn",
    # 白名单域被当作子域前缀
    "https://feishu.cn.evil.example/docx/abc",
    # 域名出现在路径里
    "https://evil.example/feishu.cn/docx",
    # 后缀拼接，没有点分隔
    "https://notfeishu.cn/docx/abc",
    "https://myfeishu.cn/docx",
    # 非 http(s) 协议
    "javascript:alert('feishu.cn')",
    "file:///tmp/feishu.cn",
    "",
    "feishu.cn/docx/abc",
])
def test_rejects_lookalikes_and_non_http(url):
    assert sf.is_feishu_doc_url(url) is False


def test_extract_picks_the_feishu_url_not_the_decoy():
    stdout = (
        "creating doc...\n"
        "see also https://evil.example/?ref=feishu.cn for details\n"
        "doc created: https://example.feishu.cn/docx/abc123\n"
    )
    assert sf.extract_feishu_doc_url(stdout) == "https://example.feishu.cn/docx/abc123"


def test_extract_strips_trailing_punctuation():
    stdout = "文档已创建：https://example.feishu.cn/docx/abc123。"
    assert sf.extract_feishu_doc_url(stdout) == "https://example.feishu.cn/docx/abc123"


@pytest.mark.parametrize("stdout", [
    "",
    None,
    "创建失败，请重试",
    # 旧实现会把整行原样返回，新实现必须给 None
    "error: could not reach feishu.cn",
])
def test_extract_returns_none_when_no_valid_url(stdout):
    assert sf.extract_feishu_doc_url(stdout) is None
