"""
基本面数据模块
数据源：BaoStock（免费，无需API key）
覆盖：PE、PB、ROE、营收增速、净利增速、资产负债率
"""

import json
import subprocess
import sys
import os
from typing import Optional, Dict, List

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

from paths import hermes_python

# 使用 Hermes venv 的 Python（BaoStock 装在那里）
HERMES_PYTHON = hermes_python()


def _extract_json(text):
    """从可能包含login消息的stdout中提取JSON"""
    if not text:
        return None
    # 找到第一个合法的JSON起始位置：跳过login消息
    # JSON数组以 [ 开头，对象以 { 开头
    # 但login消息也包含 [，所以要从遇到的第一个 [{" 或 [{ 开始
    start = -1
    for pattern in ['[{"', '[["', '[\\"{', '{"', '[{']:
        idx = text.find(pattern)
        if idx >= 0:
            start = idx
            break

    if start < 0:  # 后备方案：直接找第一个 [ 或 {
        start = text.find('[')
        if start < 0:
            start = text.find('{')
    if start < 0:
        return None

    # 找到匹配的结束
    end = text.rfind(']') if text[start] == '[' else text.rfind('}')
    if end < 0:
        return None
    try:
        return json.loads(text[start:end+1])
    except Exception:
        return None


def _baostock_query(code: str, query_type: str, **kwargs) -> Optional[List[Dict]]:
    """通用 BaoStock 查询封装"""
    code_fmt = f"sh.{code}" if code.startswith('6') else f"sz.{code}"

    # 构建查询Python代码
    py_code = f"""
import sys, baostock as bs, json
lg = bs.login()
sys.stderr.write(lg.error_code + ' ' + lg.error_msg + '
')
if lg.error_code != '0':
    print('ERROR:' + lg.error_msg, file=sys.stderr)
    exit()

try:
    rs = bs.{query_type}("{code_fmt}", "{kwargs.get('start_date', '2025-01-01')}", "{kwargs.get('end_date', '2026-06-01')}")
    rows = []
    while rs.next():
        row = rs.get_row_data()
        rows.append(row)
    bs.logout()
    sys.stderr.write('logout success!
')
    sys.stdout.write(json.dumps(rows, ensure_ascii=False))
except Exception as e:
    bs.logout()
    print('ERROR:' + str(e), file=sys.stderr)
"""
    try:
        result = subprocess.run(
            [HERMES_PYTHON, "-c", py_code],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0 or result.stdout.startswith("ERROR"):
            return None
        return _extract_json(result.stdout)
    except Exception:
        return None


QUARTER_MAP = {1: "1", 2: "2", 3: "3", 4: "4"}


def get_stock_basic(code: str) -> Optional[Dict]:
    """获取股票基本面基础数据"""
    code_fmt = f"sh.{code}" if code.startswith('6') else f"sz.{code}"

    py_code = f"""
import baostock as bs, json
lg = bs.login()
if lg.error_code != '0': print('ERROR:' + lg.error_msg); exit()
try:
    rs = bs.query_stock_basic(code="{code_fmt}")
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    bs.logout()
    print(json.dumps(rows, ensure_ascii=False))
except Exception as e:
    bs.logout()
    print('ERROR:' + str(e))
"""
    try:
        result = subprocess.run(
            [HERMES_PYTHON, "-c", py_code],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0 or result.stdout.startswith("ERROR"):
            return None
        data = _extract_json(result.stdout)
        if data and len(data) > 0:
            d = data[0]
            return {
                "code": code,
                "name": d[1] if len(d) > 1 else "",
                "ipo_date": d[2] if len(d) > 2 else "",
                "status": d[3] if len(d) > 3 else "",
            }
        return None
    except Exception:
        return None


def get_profit_data(code: str, year=2025, quarter=4) -> Optional[Dict]:
    """获取利润表数据：营收、净利"""
    code_fmt = f"sh.{code}" if code.startswith('6') else f"sz.{code}"

    py_code = f"""
import baostock as bs, json
lg = bs.login()
if lg.error_code != '0': print('ERROR:' + lg.error_msg); exit()
try:
    rs = bs.query_profit_data(code="{code_fmt}", year={year}, quarter={quarter})
    rows = []
    while rs.next():
        row_data = rs.get_row_data()
        rows.append({{
            "date": row_data[1] if len(row_data) > 1 else "",
            "code": row_data[0] if len(row_data) > 0 else "",
            "roe": row_data[3] if len(row_data) > 3 else "",
            "eps": row_data[4] if len(row_data) > 4 else "",
            "revenue_rate": row_data[5] if len(row_data) > 5 else "",   # 营收同比增速
            "oper_rev": row_data[8] if len(row_data) > 8 else "",       # 累计营收
            "net_profit": row_data[9] if len(row_data) > 9 else "",     # 归母净利润
        }})
    bs.logout()
    print(json.dumps(rows, ensure_ascii=False))
except Exception as e:
    bs.logout()
    print('ERROR:' + str(e))
"""
    try:
        result = subprocess.run(
            [HERMES_PYTHON, "-c", py_code],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0 or result.stdout.startswith("ERROR"):
            return None
        data = _extract_json(result.stdout)
        if data and len(data) > 0:
            d = data[0]
            np_ = float(d['net_profit']) if d.get('net_profit') else 0
            return {
                "oper_rev": float(d['oper_rev']) if d.get('oper_rev') else 0,
                "net_profit": np_,
                "roe": float(d['roe']) if d.get('roe') else 0,
                "revenue_rate": float(d['revenue_rate']) if d.get('revenue_rate') else 0,
                "date": d.get('date', ''),
            }
        return None
    except Exception:
        return None


def get_dupont_data(code: str, year=2025, quarter=4) -> Optional[Dict]:
    """获取杜邦分析数据：ROE, ROA等"""
    code_fmt = f"sh.{code}" if code.startswith('6') else f"sz.{code}"

    py_code = f"""
import baostock as bs, json
lg = bs.login()
if lg.error_code != '0': print('ERROR:' + lg.error_msg); exit()
try:
    rs = bs.query_dupont_data(code="{code_fmt}", year={year}, quarter={quarter})
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    bs.logout()
    print(json.dumps(rows, ensure_ascii=False))
except Exception as e:
    bs.logout()
    print('ERROR:' + str(e))
"""
    try:
        result = subprocess.run(
            [HERMES_PYTHON, "-c", py_code],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0 or result.stdout.startswith("ERROR"):
            return None
        data = _extract_json(result.stdout)
        if data and len(data) > 0:
            d = data[0]
            return {
                "roe": float(d[6]) if len(d) > 6 and d[6] else None,        # ROE
                "roa": float(d[7]) if len(d) > 7 and d[7] else None,        # ROA
                "net_margin": float(d[8]) if len(d) > 8 and d[8] else None, # 净利率
                "asset_turnover": float(d[9]) if len(d) > 9 and d[9] else None,
                "equity_multiplier": float(d[10]) if len(d) > 10 and d[10] else None,  # 权益乘数
                "period": f"{year}Q{quarter}",
            }
        return None
    except Exception:
        return None


def get_growth_data(code: str) -> Optional[Dict]:
    """获取营收/净利增长率（同比近4个季度）"""
    current_year = 2026
    # 取最近4个季度
    quarters = [(2025, 1), (2025, 2), (2025, 3), (2025, 4)]

    profits = []
    for y, q in quarters:
        d = get_profit_data(code, y, q)
        if d:
            profits.append(d)

    if len(profits) < 2:
        return None

    # 营收增长率（最后一季 vs 第一季）
    rev_latest = profits[-1]['oper_rev']
    rev_earliest = profits[0]['oper_rev']
    np_latest = profits[-1]['net_profit']
    np_earliest = profits[0]['net_profit']

    rev_growth = round((rev_latest - rev_earliest) / rev_earliest * 100, 2) if rev_earliest > 0 else 0
    np_growth = round((np_latest - np_earliest) / np_earliest * 100, 2) if np_earliest > 0 else 0

    return {
        "rev_growth_4q": rev_growth,       # 4个季度营收增长率
        "np_growth_4q": np_growth,          # 4个季度净利增长率
        "latest_rev": rev_latest,
        "latest_np": np_latest,
        "avg_gross_margin": round(sum(p.get('gross_margin', 0) for p in profits) / len(profits), 2),
        "avg_net_margin": round(sum(p.get('net_margin', 0) for p in profits) / len(profits), 2),
    }


def get_full_analysis(code: str, name: str = "") -> Dict:
    """完整基本面分析"""
    from scripts.tech_analysis import analyze_stock
    from scripts.data_cache import fetch_realtime

    result = {
        "code": code,
        "name": name or code,
        "error": None,
    }

    # 1. 基本面
    basic = get_stock_basic(code)
    dupont = get_dupont_data(code)
    growth = get_growth_data(code)

    result['basic'] = basic
    result['dupont'] = dupont
    result['growth'] = growth

    # 2. 最新一季财务（优先取有营收数据的季度）
    rev_profit = None
    for yr, qtr in [(2026, 1), (2025, 4), (2025, 3), (2025, 2), (2025, 1)]:
        profit = get_profit_data(code, yr, qtr)
        if profit and profit.get('oper_rev', 0) > 0:
            rev_profit = profit  # 有营收数据
        if profit and not result.get('profit'):
            result['profit'] = profit  # 任意有数据的季度
    # 如果有营收数据，用它覆盖
    if rev_profit:
        result['profit'] = rev_profit

    # 3. 技术面
    try:
        rt = fetch_realtime([code])
        tech = analyze_stock(code, name, realtime=rt.get(code))
        result['technical'] = tech
    except Exception:
        result['technical'] = None

    return result


def format_fundamental(result: Dict) -> str:
    """格式化输出基本面分析"""
    lines = []
    lines.append(f"\n{'='*60}")
    lines.append(f" 📊 {result['name']}({result['code']}) 基本面分析")
    lines.append(f"{'='*60}")

    # 基础信息
    if result.get('basic'):
        b = result['basic']
        lines.append(f" 上市日期: {b.get('ipo_date', '-')} | 状态: {b.get('status', '-')}")

    # 财务数据
    if result.get('profit'):
        p = result['profit']
        lines.append(f"\n 最新财报({p.get('date','-')}):")
        lines.append(f"   营收: {p.get('oper_rev',0)/1e8:.2f}亿")
        lines.append(f"   净利: {p.get('net_profit',0)/1e8:.2f}亿")
        lines.append(f"   ROE: {float(p.get('roe',0))*100:.2f}%")
        if p.get('revenue_rate'):
            rr = float(p['revenue_rate']) * 100
            lines.append(f"   营收同比: {rr:+.2f}%")

    # ROE等
    if result.get('dupont'):
        d = result['dupont']
        lines.append(f"\n 杜邦分析({d.get('period','-')}):")
        lines.append(f"   ROE: {d.get('roe', 0):.2f}%")
        lines.append(f"   ROA: {d.get('roa', 0):.2f}%")
        lines.append(f"   资产周转率: {d.get('asset_turnover', 0):.2f}")

    # 增长
    if result.get('growth'):
        g = result['growth']
        lines.append("\n 增长趋势(近4季):")
        lines.append(f"   营收增速: {g.get('rev_growth_4q', 0):+.2f}%")
        lines.append(f"   净利增速: {g.get('np_growth_4q', 0):+.2f}%")
        lines.append(f"   平均毛利率: {g.get('avg_gross_margin', 0):.1f}%")

    return "\n".join(lines)


def format_brief(results: List[Dict]) -> str:
    """用于板块横向对比的简略格式"""
    lines = []
    lines.append(f"{'代码':<8} {'名称':<10} {'ROE':<7} {'营收同比':<9} {'评分':<5} {'评级':<12}")
    lines.append("-" * 60)

    for r in results:
        p = r.get('profit') or {}
        roe = f"{float(p.get('roe',0))*100:.1f}%" if p.get('roe') else "-"
        rev_rate = f"{float(p.get('revenue_rate',0))*100:+.1f}%" if p.get('revenue_rate') else "-"
        score = r.get('technical', {}).get('score', '-')
        rating = (r.get('technical', {}).get('rating', '') or '')[:10]

        lines.append(
            f"{r['code']:<8} {r['name']:<10} "
            f"{roe:<7} {rev_rate:<9} "
            f"{score:<5} {rating:<12}"
        )

    return "\n".join(lines)


if __name__ == "__main__":
    code = sys.argv[1] if len(sys.argv) > 1 else "600519"
    name = sys.argv[2] if len(sys.argv) > 2 else ""

    result = get_full_analysis(code, name)
    print(format_fundamental(result))
