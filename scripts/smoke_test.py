#!/usr/bin/env python3
"""Smoke Test — 验证核心脚本可运行且不崩溃"""

import subprocess
import sys
import json

PASS = 0
FAIL = 0

def run(cmd, name, timeout=60, check_json=True, allow_empty=False):
    global PASS, FAIL
    print(f"  [{name}] ", end="", flush=True)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            print(f"FAIL (exit={r.returncode})")
            print(f"    stderr: {r.stderr[:200]}")
            FAIL += 1
            return None

        if check_json and not allow_empty and not r.stdout.strip():
            print("FAIL (empty output)")
            FAIL += 1
            return None

        if check_json and r.stdout.strip():
            try:
                data = json.loads(r.stdout)
                print("OK")
                PASS += 1
                return data
            except json.JSONDecodeError:
                print(f"WARN (non-JSON output, {len(r.stdout)} chars)")
                PASS += 1
                return r.stdout
        else:
            print("OK")
            PASS += 1
            return r.stdout
    except subprocess.TimeoutExpired:
        print("FAIL (timeout)")
        FAIL += 1
        return None
    except Exception as e:
        print(f"FAIL ({e})")
        FAIL += 1
        return None


PY = sys.executable

tests = [
    (  # 1. 四维打分
        [PY, "skills/stock-triage/scripts/four_dim_scorer.py", "002156", "通富微电", "--json"],
        "four_dim_scorer"
    ),
    (  # 2. 全球市场监控（含 source_health，长超时避免 flaky）
        [PY, "skills/global-market-monitor/scripts/monitor.py", "--json"],
        "global_monitor", 120, True
    ),
    (  # 3. 港A联动 (yfinance may fail, but script must not crash)
        [PY, "skills/stock-triage/scripts/hk_a_linkage.py", "--json"],
        "hk_a_linkage", 120, True
    ),
    (  # 4. 新闻→板块
        [PY, "skills/news-to-sector/scripts/main.py", "焦煤期货主力合约触及涨停，涨幅8%"],
        "news_to_sector", 30, False
    ),
    (  # 5. 持仓风控 (no positions is fine)
        [PY, "skills/stock-triage/scripts/portfolio_manager.py", "--check", "--json"],
        "portfolio_manager", 30, True, True
    ),
    (  # 6. Cron manifest 验证
        [PY, "scripts/validate_cron_manifest.py", "cron/hermes-cron-manifest.json"],
        "cron_manifest", 10, False
    ),
]

print("=" * 50)
print("A-Stock Agent System Smoke Test")
print("=" * 50)

for t in tests:
    run(*t)

print("=" * 50)
print(f"Results: {PASS} passed, {FAIL} failed")
print("=" * 50)
sys.exit(FAIL)
