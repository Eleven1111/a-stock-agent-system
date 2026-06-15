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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'common'))
from paths import cron_output_dir

LARK_CLI = "lark-cli"
REPORT_ROOT = cron_output_dir()
FEISHU_FOLDER = ""  # 不依赖 folder token，直接创建到飞书根目录


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


def create_feishu_doc(title: str, content: str) -> Optional[str]:
    """创建飞书文档"""
    # 飞书文档标题限制
    safe_title = title[:100].replace('"', "'")

    # 清理 markdown（飞书不支持表格）
    clean_content = sanitize_markdown(content)

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
            # fallback：扫描输出中的 URL
            for line in result.stdout.split("\n"):
                if "feishu.cn" in line or "larksuite.com" in line:
                    return line.strip()
            return result.stdout.strip()
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
