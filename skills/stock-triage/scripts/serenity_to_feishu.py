#!/usr/bin/env python3
"""
Serenity 深度报告 → 飞书文档存档管道
====================================
将 Serenity/S级深度分析报告自动归档到飞书云文档。

Usage:
  echo "# 深度分析报告..." | python3 serenity_to_feishu.py "示例公司"
  python3 serenity_to_feishu.py --file /path/to/report.md "示例公司"
"""

import sys
import os
import json
import subprocess
import re
from datetime import datetime
from typing import Optional, Dict
from urllib.parse import urlparse

import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path
from feishu_push import DISCLOSURE
from paths import cron_output_dir

LARK_CLI = "lark-cli"
REPORT_ROOT = cron_output_dir()
FEISHU_FOLDER = ""  # 不依赖 folder token，直接创建到飞书根目录

# 飞书文档域名白名单。按 host 精确匹配或匹配子域，**不做整行子串判断** ——
# 子串判断会让 https://evil.example/?ref=feishu.cn 通过（CodeQL
# py/incomplete-url-substring-sanitization）。
FEISHU_DOC_HOSTS = ("feishu.cn", "larksuite.com")
# 结束边界含中英文标点：CLI 输出常把 URL 嵌在中文句子里（「已创建：https://…。」）
_URL_RE = re.compile(r"https?://[^\s\"'<>()（）「」【】，。；：]+")


def sanitize_markdown(text: str) -> str:
    """清理 markdown 以适配飞书文档限制（不支持表格）"""
    lines = []
    in_table = False
    for line in text.split("\n"):
        # 跳过表格分隔行
        if re.match(r'^\|[-:\s|]+\|$', line):
            in_table = True
            continue
        # 表格行转列表
        if line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if in_table:
                lines.append("• " + " — ".join(cells))
            else:
                lines.append("• " + " — ".join(cells))
            in_table = False
            continue
        in_table = False
        lines.append(line)
    return "\n".join(lines)


def with_disclosure(text: str) -> str:
    """在文档正文末尾补上与消息出口同一份免责声明，幂等。

    飞书有两类出口：聊天消息走 common/feishu_push.push_text，研究文档走本脚本。
    两处共用 feishu_push.DISCLOSURE，避免文案漂移导致"只有一半出口合规"。
    """
    if DISCLOSURE in text:
        return text
    separator = "" if text.endswith("\n") else "\n"
    return f"{text}{separator}\n{DISCLOSURE}"


def is_feishu_doc_url(candidate: str) -> bool:
    """URL 的 host 是否属于飞书文档域。

    只认 http/https，且 host 必须等于白名单域或是其子域。
    ``https://evil.example/?ref=feishu.cn``、``https://feishu.cn.evil.example``
    都必须判否 —— 前者是查询串里带域名，后者是把白名单域当子域前缀。
    """
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return any(
        host == domain or host.endswith("." + domain)
        for domain in FEISHU_DOC_HOSTS
    )


def extract_feishu_doc_url(stdout: str) -> Optional[str]:
    """从 CLI 输出里取第一个真正指向飞书文档域的 URL。

    旧实现是「整行含 feishu.cn 就返回整行」，既可能返回非 URL 文本，
    也会被任意位置出现的域名字符串骗过。
    """
    for match in _URL_RE.finditer(stdout or ""):
        url = match.group(0).rstrip(".,;:。，；：、")
        if is_feishu_doc_url(url):
            return url
    return None


def create_feishu_doc(title: str, content: str) -> Optional[str]:
    """创建飞书文档"""
    # 飞书文档标题限制
    safe_title = title[:100].replace('"', "'")

    # 清理 markdown（飞书不支持表格）+ 追加免责声明（出口级，不依赖调用方记得加）
    clean_content = with_disclosure(sanitize_markdown(content))

    # 写入临时文件
    tmpfile = f"/tmp/feishu_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    with open(tmpfile, "w", encoding="utf-8") as f:
        f.write(clean_content)

    try:
        cmd = [LARK_CLI, "docs", "+create", "--title", safe_title, "--markdown", tmpfile]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            try:
                resp = json.loads(result.stdout)
                doc_url = resp.get("data", {}).get("doc_url", "")
                if doc_url:
                    return doc_url
            except json.JSONDecodeError:
                pass
            # fallback：JSON 解析失败时，从输出里提取飞书文档域下的 URL
            return extract_feishu_doc_url(result.stdout)
        else:
            print(f"❌ 飞书创建失败: {result.stderr}", file=sys.stderr)
            return None
    except Exception as e:
        print(f"❌ 飞书创建异常: {e}", file=sys.stderr)
        return None
    finally:
        if os.path.exists(tmpfile):
            os.unlink(tmpfile)


def archive_report(stock_name: str, report_text: str) -> Dict:
    """归档一份报告"""
    date_str = datetime.now().strftime("%Y-%m-%d")
    title = f"🔬 Serenity深度分析：{stock_name} ({date_str})"

    doc_url = create_feishu_doc(title, report_text)

    # 本地存档
    local_dir = os.path.join(REPORT_ROOT, "serenity_archive")
    os.makedirs(local_dir, exist_ok=True)
    local_file = os.path.join(local_dir, f"{stock_name}_{date_str}.md")
    with open(local_file, "w", encoding="utf-8") as f:
        f.write(report_text)

    return {
        "stock": stock_name,
        "date": date_str,
        "feishu_url": doc_url,
        "local_path": local_file,
        "status": "ok" if doc_url else "feishu_failed_but_local_saved",
    }


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Serenity报告→飞书存档")
    parser.add_argument("stock_name", help="股票名称")
    parser.add_argument("--file", help="报告文件路径（不指定则从stdin读取）")
    args = parser.parse_args()

    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            report = f.read()
    else:
        report = sys.stdin.read()

    if not report.strip():
        print(json.dumps({"error": "空报告内容"}))
        sys.exit(1)

    result = archive_report(args.stock_name, report)
    print(json.dumps(result, ensure_ascii=False, indent=2))
