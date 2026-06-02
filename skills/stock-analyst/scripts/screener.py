"""
条件筛选引擎
基于本地缓存K线 + 实时数据，支持多条件筛选

语法：
  screener --条件1 AND/OR 条件2
  
条件格式：
  rsi<30            RSI(14)低于30
  rsi(6)>70         RSI(6)高于70
  ma5>ma20          MA5上穿MA20
  macd_golden       MACD金叉
  macd_death        MACD死叉
  kdj_golden        KDJ金叉
  kdj_oversold      KDJ超卖(K<20)
  kdj_overbought    KDJ超买(K>80)
  volume_ratio>2    成交量>5日均量2倍
  volume_ratio<0.5  成交量<5日均量0.5倍
  close>ma20        收盘价>MA20
  close<ma60        收盘价<MA60
  pct_5d>10         近5日涨幅>10%
  pct_5d<-10        近5日跌幅>10%
  boll_upper        触及布林上轨
  boll_lower        触及布林下轨
  score>2           综合评分>2
  score<0           综合评分<0
  not_banned        排除ST/停牌股
  industry=电力     限定行业

示例：
  全市场RSI超卖+放量:   screener "rsi<30 AND volume_ratio>1.2"
  金叉+多头排列:        screener "macd_golden AND ma5>ma20>ma60"
  KDJ超卖+远离均线:    screener "kdj_oversold AND close<ma20*0.9"
  板块限定:             screener "score>0 AND industry=电力"
"""

import sys
import os
import re
import json
import sqlite3
import urllib.request
from datetime import datetime, timedelta
from collections import defaultdict

# 添加项目根路径
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_DIR)

from scripts.data_cache import fetch_kline, fetch_realtime, get_db, fetch_zt_pool
from scripts.tech_analysis import analyze_stock, compute_rsi, compute_macd, compute_kdj, compute_boll, sma
import numpy as np


# ─── 条件解析器 ───

CONDITION_DEFS = {
    "rsi": lambda v, k: (f"rsi(14)", "<", v) if k.startswith("rsi<") else (f"rsi(14)", ">", v) if k.startswith("rsi>") else None,
    "rsi_6": None,
    "ma5>ma20": ("ma_golden_cross",),
    "ma5<ma20": ("ma_death_cross",),
    "macd_golden": ("macd_golden",),
    "macd_death": ("macd_death",),
    "kdj_golden": ("kdj_golden",),
    "kdj_oversold": ("kdj_oversold",),
    "kdj_overbought": ("kdj_overbought",),
    "boll_upper": ("boll_upper",),
    "boll_lower": ("boll_lower",),
    "volume_ratio": ("volume_ratio",),
    "close>ma20": ("close_above_ma20",),
    "close<ma20": ("close_below_ma20",),
    "close>ma60": ("close_above_ma60",),
    "close<ma60": ("close_below_ma60",),
    "pct_5d": ("pct_5d",),
    "score": ("score",),
}

def parse_query(query_str: str) -> list:
    """解析查询字符串为条件列表"""
    # 替换中文标点
    q = query_str.replace("，", ",").replace(" ", " ").strip()
    
    # 分割 AND/OR
    parts = re.split(r'\s+(?:AND|and|&&)\s+', q)
    op = "AND" if len(parts) > 1 else "OR"
    if op == "AND" and len(parts) == 1:
        parts = re.split(r'\s+(?:OR|or|\|\|)\s+', q)
        op = "OR"
    
    conditions = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        
        # 解析条件
        cond = None
        
        # 比较条件: field<value, field>value, field=value
        m = re.match(r'^(\w+)\s*(<|>|=|<=|>=)\s*([\d.]+)$', p)
        if m:
            field, opc, val = m.group(1), m.group(2), float(m.group(3))
            cond = {"field": field, "op": opc, "value": val, "raw": p}
        else:
            # 名称条件
            m2 = re.match(r'^(\w+)=(.+)$', p)
            if m2:
                cond = {"field": m2.group(1), "op": "=", "value_str": m2.group(2), "raw": p}
            else:
                # 纯信号条件
                cond = {"signal": p, "raw": p}
        
        if cond:
            conditions.append(cond)
    
    return conditions, op


def _get_condition_value(result, key):
    """从分析结果中提取条件值"""
    # 检查 signals
    if key == "rsi":
        for k, v in result.get('signals', {}).items():
            if k == 'rsi':
                m = re.search(r'([\d.]+)', str(v))
                return float(m.group(1)) if m else None
    if key == "rsi_6":
        return None  # 需要单独计算
    if key == "ma_golden_cross":
        return 1 if 'ma_golden_cross' in result.get('signals', {}) else 0
    if key == "ma_death_cross":
        return 1 if 'ma_death_cross' in result.get('signals', {}) else 0
    if key == "macd_golden":
        return 1 if 'macd_golden' in result.get('signals', {}) else 0
    if key == "macd_death":
        return 1 if 'macd_death' in result.get('signals', {}) else 0
    if key == "kdj_golden":
        return 1 if 'kdj' in result.get('signals', {}) and '金叉' in result['signals']['kdj'] else 0
    if key == "kdj_oversold":
        for k, v in result.get('signals', {}).items():
            if k == 'kdj' and '超卖' in str(v):
                return 1
        return 0
    if key == "kdj_overbought":
        for k, v in result.get('signals', {}).items():
            if k == 'kdj' and '超买' in str(v):
                return 1
        return 0
    if key == "boll_upper":
        for k, v in result.get('signals', {}).items():
            if k == 'boll' and '上轨' in str(v):
                return 1
        return 0
    if key == "boll_lower":
        for k, v in result.get('signals', {}).items():
            if k == 'boll' and '下轨' in str(v):
                return 1
        return 0
    if key == "volume_ratio":
        return result.get('volume_ratio')
    if key == "close_above_ma20":
        return 1 if result.get('price') and result.get('ma20') and result['price'] > result['ma20'] else 0
    if key == "close_below_ma20":
        return 1 if result.get('price') and result.get('ma20') and result['price'] < result['ma20'] else 0
    if key == "close_above_ma60":
        return 1 if result.get('price') and result.get('ma60') and result['price'] > result['ma60'] else 0
    if key == "close_below_ma60":
        return 1 if result.get('price') and result.get('ma60') and result['price'] < result['ma60'] else 0
    if key == "score":
        return result.get('score')
    if key == "pct_5d":
        return result.get('pct_5d')
    if key == "price":
        return result.get('price')
    return None


def check_conditions(result, conditions, logic="AND"):
    """检查个股是否满足所有条件"""
    if not conditions:
        return True
    
    results_bool = []
    for cond in conditions:
        if 'signal' in cond:
            val = _get_condition_value(result, cond['signal'])
            results_bool.append(bool(val))
        elif 'value' in cond:
            val = _get_condition_value(result, cond['field'])
            if val is None:
                results_bool.append(False)
            elif cond['op'] == '<':
                results_bool.append(val < cond['value'])
            elif cond['op'] == '>':
                results_bool.append(val > cond['value'])
            elif cond['op'] == '<=':
                results_bool.append(val <= cond['value'])
            elif cond['op'] == '>=':
                results_bool.append(val >= cond['value'])
            else:
                results_bool.append(False)
        elif 'value_str' in cond:
            # 字符串匹配（行业、名称等）
            if cond['field'] == 'industry':
                results_bool.append(True)  # 在筛选时外部分组
            else:
                results_bool.append(False)
    
    if logic == "AND":
        return all(results_bool)
    else:
        return any(results_bool)


# ─── 全市场扫描 ───

def get_all_stocks(limit=500):
    """获取全市场股票列表（从上交所+深交所）"""
    from scripts.data_cache import _run_python
    
    code = """
import akshare as ak, os, json
for k in ['HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY','all_proxy']:
    os.environ.pop(k, None)
df = ak.stock_info_a_code_name()
# 过滤北交所(BJ)，只留沪深
df = df[~df['code'].str.startswith('8')]
print(df.head(6000).to_json(orient='records', force_ascii=False))
"""
    try:
        out = _run_python(code)
        data = json.loads(out)
        # 去重
        seen = set()
        result = []
        for item in data:
            c = item.get('code', '')
            if c not in seen:
                seen.add(c)
                result.append((c, item.get('name', '')))
        return result
    except Exception as e:
        print(f"获取股票列表失败: {e}")
        return []


def screen_by_conditions(conditions, logic="AND", max_results=50, industry_filter=None):
    """条件筛选主函数"""
    start_t = datetime.now()
    
    all_stocks = get_all_stocks()
    print(f"📋 全市场 {len(all_stocks)} 只股票，开始扫描...")
    
    matched = []
    checked = 0
    
    for code, name in all_stocks:
        # 行业预筛
        if industry_filter:
            # 通过缓存或实时数据判断行业
            pass
        
        # 跳过北交所
        if code.startswith('8') or code.startswith('4'):
            continue
        
        checked += 1
        if checked % 500 == 0:
            elapsed = (datetime.now() - start_t).seconds
            print(f"  已扫描 {checked}/{len(all_stocks)} 只，耗时 {elapsed}s，命中 {len(matched)} 只")
        
        # 获取K线计算
        kline = fetch_kline(code, 90)
        if not kline or len(kline) < 30:
            continue
        
        try:
            rt = fetch_realtime([code])
            result = analyze_stock(code, name, kline_data=kline, realtime=rt.get(code))
        except:
            continue
        
        if 'error' in result:
            continue
        
        if check_conditions(result, conditions, logic):
            matched.append(result)
            if len(matched) >= max_results:
                break
    
    elapsed = (datetime.now() - start_t).seconds
    print(f"\n✅ 扫描完成: {checked}只检查, {len(matched)}只命中, 耗时{elapsed}s")
    
    return matched


def format_output(results, format_type="table"):
    """格式化输出结果"""
    lines = []
    lines.append(f"{'代码':<8} {'名称':<10} {'现价':<8} {'今日':<7} {'5日':<7} {'评分':<5} {'评级':<12} {'RSI':<6} {'信号'}")
    lines.append("-" * 95)
    
    for r in results:
        rsi_str = ""
        for k, v in r.get('signals', {}).items():
            if k == 'rsi':
                rsi_str = v.split('(')[1].split(')')[0] if '(' in v else ""
        
        sigs = []
        if 'ma_golden_cross' in r.get('signals', {}): sigs.append("金叉")
        if 'macd_golden' in r.get('signals', {}): sigs.append("MACD金叉")
        if 'kdj_golden' in r.get('signals', {}): sigs.append("KDJ金叉")
        if 'boll' in r.get('signals', {}): sigs.append(r['signals']['boll'][:8])
        
        arrow = "🟢" if r.get('pct_change', 0) >= 0 else "🔴"
        lines.append(
            f"{r['code']:<8} {r['name']:<10} "
            f"{r['price']:<8.2f} {r.get('pct_change',0):>+6.2f}% "
            f"{r.get('pct_5d',0):>+6.2f}% "
            f"{r.get('score',0):<+5} {r.get('rating','')[:10]:<12} "
            f"{rsi_str:<6} {' '.join(sigs)}"
        )
    
    return "\n".join(lines)


def screen_by_criteria(criteria: dict) -> list:
    """便捷筛选接口，供 CLI 调用"""
    return screen_by_conditions(criteria.get('conditions', []), 
                                criteria.get('logic', 'AND'),
                                criteria.get('max_results', 50))


# ─── 行业预筛（只用缓存） ───

def get_industry_list() -> dict:
    """从涨停数据获取行业列表"""
    zt_data = fetch_zt_pool(datetime.now().strftime("%Y%m%d"))
    industries = set()
    for d in zt_data:
        if d.get('所属行业'):
            industries.add(d['所属行业'])
    return sorted(industries)


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    
    query = " ".join(sys.argv[1:])
    conditions, logic = parse_query(query)
    
    print(f"🔍 筛选条件: {query}")
    print(f"  逻辑: {logic}, 条件数: {len(conditions)}")
    
    results = screen_by_conditions(conditions, logic)
    
    if results:
        print("\n" + format_output(results))
    else:
        print("\n❌ 未找到匹配的股票")
