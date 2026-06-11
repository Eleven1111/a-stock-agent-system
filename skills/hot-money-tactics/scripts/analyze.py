#!/usr/bin/env python3
"""
游资战法综合分析工具
=====================
功能：涨停板分析、连板梯队、板块热度、封板质量、市场情绪
数据源：AkShare (东方财富)
用法：
  python3 analyze.py                     # 今日分析
  python3 analyze.py 20260601            # 指定日期
  python3 analyze.py --all               # 今日完整报告
  python3 analyze.py --cache-only        # 仅刷新共享情绪上下文并输出 JSON
"""

import akshare as ak
import json
import os
import sys
import pandas as pd
from datetime import datetime, timedelta

pd.set_option('display.max_columns', None)
pd.set_option('display.width', 320)
pd.set_option('display.max_colwidth', 30)

# 清代理
for k in ['HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY','all_proxy','NO_PROXY']:
    os.environ.pop(k, None)

# ======================== 数据采集 ========================

def get_zt_pool(date_str):
    """涨停板池"""
    try:
        df = ak.stock_zt_pool_em(date=date_str)
        df['连板数'] = df['连板数'].fillna(0).astype(int)
        return df
    except Exception as e:
        return pd.DataFrame()

def get_strong_pool(date_str):
    """强势股池"""
    try:
        df = ak.stock_zt_pool_strong_em(date=date_str)
        return df
    except Exception as e:
        return pd.DataFrame()

def get_all_stocks():
    """全A股行情（Tencent API 降级方案）"""
    try:
        df = ak.stock_zh_a_spot_em()
        if not df.empty:
            return df
    except Exception:
        pass
    # 降级：用腾讯API获取大盤指数
    return _tencent_market_fallback()

def _tencent_market_fallback():
    """Tencent API 降级获取大盘数据（GBK编码）"""
    import urllib.request

    try:
        url = 'http://qt.gtimg.cn/q=sh000001,sz399001,sz399006'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode('gbk', errors='ignore')
        lines = text.strip().split('\n')
        rows = []
        for line in lines:
            if '=' not in line:
                continue
            val = line.split('=', 1)[1].strip('"')
            parts = val.split('~')
            if len(parts) < 40:
                continue
            rows.append({
                '名称': parts[1],
                '代码': parts[2],
                '最新价': float(parts[3]) if parts[3] else 0,
                '涨跌幅': float(parts[32]) if parts[32] else 0,
                '涨跌额': float(parts[31]) if parts[31] else 0,
                '成交额': float(parts[37]) * 10000 if len(parts) > 37 and parts[37] else 0,
                '成交量': float(parts[36]) if len(parts) > 36 and parts[36] else 0,
                '最高': float(parts[33]) if parts[33] else 0,
                '最低': float(parts[34]) if parts[34] else 0,
                '今开': float(parts[5]) if parts[5] else 0,
                '昨收': float(parts[4]) if parts[4] else 0,
            })
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame()

# ======================== 分析函数 ========================

def analyze_board(df_zt):
    """连板梯队分析"""
    lines = []
    lines.append("━━━ 连板梯队 ━━━")
    max_ban = df_zt['连板数'].max()
    for ban in range(int(max_ban), 0, -1):
        subset = df_zt[df_zt['连板数'] == ban]
        if len(subset) > 0:
            name_list = []
            for _, r in subset.iterrows():
                feng = r['封板资金'] / 1e8 if pd.notna(r['封板资金']) and r['封板资金'] > 0 else 0
                zhaban = " [炸]" if r['炸板次数'] > 0 else ""
                tag = ""
                # 优质板标记：封单>1亿且早盘封
                if feng >= 1 and pd.notna(r['首次封板时间']) and r['首次封板时间'] <= '09:35':
                    tag = " ⭐"
                name_list.append("{}({:.1f}亿{}){}".format(r['名称'], feng, zhaban.replace('[炸]','炸'), tag))
            lines.append("  {}板 ({}只): {}".format(ban, len(subset), " ".join(name_list)))
    return "\n".join(lines)

def analyze_quality(df_zt):
    """封板质量分析"""
    lines = []
    lines.append("\n━━━ 封板质量 ━━━")

    total = len(df_zt)
    df_t = df_zt.dropna(subset=['首次封板时间'])
    # 集合竞价 9:25 结束：≤09:25 才是真竞价封；09:25-09:31 是开盘秒封，强度不同，分开统计。
    auction = len(df_t[df_t['首次封板时间'] <= '09:25'])
    open_burst = len(df_t[(df_t['首次封板时间'] > '09:25') & (df_t['首次封板时间'] <= '09:31')])
    mid = len(df_t[(df_t['首次封板时间'] > '09:31') & (df_t['首次封板时间'] <= '10:00')])
    late = len(df_t[df_t['首次封板时间'] > '10:00'])
    zhaban_count = len(df_zt[df_zt['炸板次数'] > 0])

    lines.append("  封板总数: {}只".format(total))
    lines.append("  集合竞价封(≤9:25): {}只 ({:.0f}%)".format(auction, auction/total*100 if total else 0))
    lines.append("  开盘秒封(9:25-9:31): {}只 ({:.0f}%)".format(open_burst, open_burst/total*100 if total else 0))
    lines.append("  早盘封(9:31-10:00): {}只 ({:.0f}%)".format(mid, mid/total*100 if total else 0))
    lines.append("  午盘后封: {}只 ({:.0f}%)".format(late, late/total*100 if total else 0))
    lines.append("  有炸板记录: {}只 ({:.1f}%)".format(zhaban_count, zhaban_count/total*100 if total else 0))

    return "\n".join(lines)

def analyze_sector(df_zt):
    """板块热点分析"""
    lines = []
    lines.append("\n━━━ 板块热度 ━━━")
    sector_stats = df_zt.groupby('所属行业').agg(
        涨停家数=('名称', 'count'),
        最高连板=('连板数', 'max'),
        总连板=('连板数', 'sum')
    ).sort_values('涨停家数', ascending=False)

    for s, row in sector_stats.head(10).iterrows():
        # 看看这个板块的个股
        stocks = df_zt[df_zt['所属行业'] == s][['名称','连板数','封板资金']].sort_values('连板数', ascending=False)
        stock_str = " ".join(["{}({}板)".format(r['名称'], int(r['连板数'])) for _, r in stocks.iterrows()])
        lines.append("  {}: {}家涨停 最高{}板 → {}".format(
            s, int(row['涨停家数']), int(row['最高连板']), stock_str[:100]))

    return "\n".join(lines)

def analyze_market(df_all):
    """大盘情绪"""
    lines = []
    lines.append("\n━━━ 大盘情绪 ━━━")

    # 指数数据（Tencent fallback）
    if len(df_all) <= 5 and '上证指数' in df_all['名称'].values:
        for _, r in df_all.iterrows():
            amt = r['成交额'] / 1e8 if r['成交额'] > 1e8 else r['成交额']
            lines.append("  {}: {:.2f} ({:+.2f}%) | 成交额{:.0f}亿".format(
                r['名称'], r['最新价'], r['涨跌幅'], amt))
        lines.append("  (个股涨跌家数需收盘后全量数据)")
    else:
        up = len(df_all[df_all['涨跌幅'] > 0])
        down = len(df_all[df_all['涨跌幅'] < 0])
        flat = len(df_all[df_all['涨跌幅'] == 0])
        zt_count = len(df_all[df_all['涨跌幅'] >= 9.9])
        dt_count = len(df_all[df_all['涨跌幅'] <= -9.9])
        lines.append("  涨:{} 跌:{} 平:{} 涨停:{} 跌停:{}".format(up, down, flat, zt_count, dt_count))
        lines.append("  涨跌比: {:.2f}".format(up/down if down else up))
        total_vol = df_all['成交额'].sum() / 1e8
        lines.append("  全A成交额: {:.0f}亿".format(total_vol))

    return "\n".join(lines)

def analyze_fengdan_top(df_zt):
    """封单资金TOP"""
    lines = []
    lines.append("\n━━━ 封单资金TOP10 ⭐ ━━━")
    top = df_zt.sort_values('封板资金', ascending=False).head(10)
    for _, r in top.iterrows():
        feng = r['封板资金'] / 1e8
        time_str = ""
        if pd.notna(r['首次封板时间']):
            time_str = " " + str(r['首次封板时间'])
        lines.append("  {:.2f}亿 | {}{} | {}板 | {}".format(
            feng, r['名称'], time_str, int(r['连板数']), r['所属行业']))
    return "\n".join(lines)

# ======================== 游资情绪预判 ========================

def sentiment_judgment(df_zt, df_all):
    """游资情绪周期判断"""
    lines = []
    lines.append("\n━━━ 🧠 情绪周期判断 ━━━")

    zt_count = len(df_zt)
    max_ban = int(df_zt['连板数'].max()) if len(df_zt) > 0 else 0
    zhaban_rate = len(df_zt[df_zt['炸板次数'] > 0]) / len(df_zt) * 100 if len(df_zt) > 0 else 0
    df_t = df_zt.dropna(subset=['首次封板时间'])
    # 真集合竞价封（≤9:25）才反映隔夜情绪；09:25-09:31 的开盘秒封不计入。
    early_rate = len(df_t[df_t['首次封板时间'] <= '09:25']) / len(df_t) * 100 if len(df_t) > 0 else 0

    total_mcap = df_zt['总市值'].sum() / 1e8 if '总市值' in df_zt.columns else 0
    mean_mcap = df_zt['总市值'].mean() / 1e8 if len(df_zt) > 0 else 0

    # 判断情绪
    signals = []
    if zt_count >= 80:
        signals.append("🔥 涨停数{}超80只，情绪高温".format(zt_count))
    elif zt_count >= 50:
        signals.append("🔥 涨停数{}只，情绪活跃".format(zt_count))
    elif zt_count >= 30:
        signals.append("🌤 涨停数{}只，情绪温和".format(zt_count))
    else:
        signals.append("🌧 涨停数{}只，情绪低迷".format(zt_count))

    if max_ban >= 7:
        signals.append("🏆 最高{}连板，高度板打开空间".format(max_ban))
    elif max_ban >= 5:
        signals.append("🏆 最高{}连板，有一定高度".format(max_ban))
    elif max_ban >= 3:
        signals.append("📌 最高{}连板，高度一般".format(max_ban))
    else:
        signals.append("⚠️ 最高仅{}连板，无高度板".format(max_ban))

    if zhaban_rate >= 40:
        signals.append("⚡ 封板率{:.0f}%，炸板率高，警惕分歧".format(100-zhaban_rate))
    elif zhaban_rate >= 25:
        signals.append("📊 封板率{:.0f}%，正常".format(100-zhaban_rate))
    else:
        signals.append("✅ 封板率{:.0f}%，质量极好".format(100-zhaban_rate))

    if early_rate >= 50:
        signals.append("🚀 集合竞价封板占{:.0f}%，隔夜情绪极强".format(early_rate))
    elif early_rate >= 35:
        signals.append("⚡ 竞价封板率{:.0f}%，隔夜情绪较好".format(early_rate))
    else:
        signals.append("💤 竞价封板仅{:.0f}%，隔夜追高意愿不强".format(early_rate))

    # 整体判断
    score = 0
    score += 2 if zt_count >= 80 else (1 if zt_count >= 50 else 0)
    score += 2 if max_ban >= 7 else (1 if max_ban >= 5 else 0)
    score += -1 if zhaban_rate >= 40 else (0 if zhaban_rate >= 25 else 1)
    score += 1 if early_rate >= 50 else 0

    if score >= 4:
        mood = "🔥🔥🔥 沸点区 — 情绪亢奋，分歧随时来临，追高谨慎"
    elif score >= 2:
        mood = "🔥 回暖区 — 情绪好转，可参与但控制仓位"
    elif score >= 0:
        mood = "🌤 震荡区 — 情绪中性，选股重于择时"
    else:
        mood = "🌧 冰点区 — 情绪低迷，多看少动"

    lines.append("  " + mood)
    for s in signals:
        lines.append("  " + s)
    return "\n".join(lines)


# ======================== 板块轮动追踪 ========================

def analyze_rotation(days=5):
    """对比最近N个交易日，追踪板块轮动"""

    lines = []
    lines.append("═══════════════════════════════════")
    lines.append("  板块轮动追踪 · 近{}个交易日".format(days))
    lines.append("═══════════════════════════════════")

    today = datetime.now()
    dates_data = {}
    tried = 0

    for offset in range(1, 15):
        if len(dates_data) >= days:
            break
        d = today - timedelta(days=offset)
        ds = d.strftime('%Y%m%d')
        df = get_zt_pool(ds)
        if df.empty:
            continue
        # 聚合板块
        sector = df.groupby('所属行业').agg(
            涨停家数=('名称', 'count'),
            最高连板=('连板数', 'max')
        ).sort_values('涨停家数', ascending=False)
        dates_data[ds] = {
            'date': d.strftime('%m/%d'),
            'date_label': '周{}'.format(d.weekday() + 1) + (' **昨**' if len(dates_data) == 0 else ''),
            'total_zt': len(df),
            'max_ban': int(df['连板数'].max()),
            'sectors': sector.head(8).to_dict('index')
        }
        tried += 1
        if tried > 12:
            break

    if not dates_data:
        lines.append("⚠️ 无法获取历史数据")
        return "\n".join(lines)

    # 1. 板块热度随时间变化
    lines.append("\n━━━ 每日板块TOP5 ━━━")
    for ds in sorted(dates_data.keys()):
        d = dates_data[ds]
        sector_str = " | ".join([
            "{} {}家".format(s, int(v['涨停家数']))
            for s, v in list(d['sectors'].items())[:5]
        ])
        lines.append("  {} {}: {}只涨停 最高{}板".format(
            d['date'], d['date_label'], d['total_zt'], d['max_ban']))
        lines.append("    {}".format(sector_str))

    # 2. 合并所有板块，看持续性
    lines.append("\n━━━ 板块持续性分析 ━━━")
    all_sectors = set()
    for ds in dates_data.values():
        all_sectors.update(ds['sectors'].keys())

    # 对每个板块，统计出现天数 + 最新排名 + 涨停家数变化
    sector_tracker = {}
    sorted_dates = sorted(dates_data.keys())
    for sec in all_sectors:
        appearances = 0
        latest_count = 0
        prev_count = 0
        counts_trend = []
        for ds in sorted_dates:
            count = dates_data[ds]['sectors'].get(sec, {}).get('涨停家数', 0)
            if count > 0:
                appearances += 1
            counts_trend.append(int(count))
        latest_count = counts_trend[-1] if counts_trend else 0
        prev_count = counts_trend[-2] if len(counts_trend) >= 2 else 0

        sector_tracker[sec] = {
            'appearances': appearances,
            'latest_count': latest_count,
            'prev_count': prev_count,
            'trend': counts_trend,
            'total_days': len(sorted_dates),
            'max_count': max(counts_trend) if counts_trend else 0
        }

    # 持续性板块（连续出现且涨停数稳定/增长）
    persistent = {s: d for s, d in sector_tracker.items()
                  if d['appearances'] >= len(sorted_dates) * 0.6 and d['latest_count'] >= 3}
    if persistent:
        lines.append("  🔥 持续热点（连续{}天以上出现）：".format(int(len(sorted_dates)*0.6)))
        for s, d in sorted(persistent.items(), key=lambda x: -x[1]['latest_count']):
            trend = "↑" if d['latest_count'] > d['prev_count'] else ("↓" if d['latest_count'] < d['prev_count'] else "→")
            lines.append("    {}: {}家涨停 {} | {}天中{}天出现".format(
                s, d['latest_count'], trend, d['total_days'], d['appearances']))

    # 新冒头板块（最近才出现）
    emerging = {s: d for s, d in sector_tracker.items()
                if d['appearances'] == 1 and d['latest_count'] >= 3}
    if emerging:
        lines.append("\n  🌱 新冒头板块（今日首次出现且在3家以上）：")
        for s, d in sorted(emerging.items(), key=lambda x: -x[1]['latest_count']):
            lines.append("    {}: {}家涨停".format(s, d['latest_count']))

    # 退潮板块（之前有但现在消失或减少）
    fading = {s: d for s, d in sector_tracker.items()
              if d['prev_count'] >= 3 and d['latest_count'] == 0}
    if fading:
        lines.append("\n  ❄️ 退潮板块（昨日有3家以上涨停，今日无）：")
        for s, d in sorted(fading.items(), key=lambda x: -x[1]['prev_count']):
            lines.append("    {}: 昨日{}家 → 今日0家 ↓".format(s, d['prev_count']))

    # 3. 轮动方向判断
    lines.append("\n━━━ 🧭 轮动方向 ━━━")
    if persistent:
        hot_direction = max(persistent.items(), key=lambda x: x[1]['latest_count'])[0]
        lines.append("  主力方向: {}（持续{}天热度）".format(hot_direction,
            persistent[hot_direction]['appearances']))
    if emerging:
        lines.append("  新方向: " + "、".join(sorted(emerging.keys())))
    if fading:
        lines.append("  退潮: " + "、".join(sorted(fading.keys())))

    return "\n".join(lines)


# ======================== 主入口 ========================

def cache_signal_context(df_zt, date_str=None):
    """把涨停池提炼为情绪上下文（板块涨停数 + 连板梯队 + 封板质量），
    落入共享缓存供 four_dim 情绪面消费。失败不阻塞主输出。"""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        '..', '..', 'common'))
        from signal_context import update_signal_context

        sector_limitups = df_zt.groupby('所属行业').size().to_dict() \
            if '所属行业' in df_zt.columns else {}

        ladder = {}
        for _, r in df_zt.iterrows():
            code = str(r.get('代码', '')).zfill(6)
            if not code or code == '000000':
                continue
            seal = r.get('封板资金')
            entry = {
                "lianban": int(r.get('连板数', 0) or 0),
                "sector": r.get('所属行业'),
                "seal_yi": round(float(seal) / 1e8, 2) if pd.notna(seal) and seal else None,
            }
            first = r.get('首次封板时间')
            if pd.notna(first) and first:
                text = str(first).replace(':', '')
                entry["first_seal"] = f"{text[:2]}:{text[2:4]}" if len(text) >= 4 else str(first)
            ladder[code] = entry

        if date_str and len(str(date_str)) == 8:
            asof = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        else:
            asof = datetime.now().strftime('%Y-%m-%d')
        update_signal_context({
            "sector_limitups": {str(k): int(v) for k, v in sector_limitups.items()},
            "lianban_ladder": ladder,
            "ladder_asof": asof,
            "limitup_total": len(df_zt),
        })
        print(f"✅ 情绪上下文已缓存：{len(ladder)}只涨停 / {len(sector_limitups)}个板块", file=sys.stderr)
        return True
    except Exception as e:
        print(f"signal_context 写入失败: {e}", file=sys.stderr)
        return False


def main():
    date_str = datetime.now().strftime('%Y%m%d')
    do_rotation = False
    full_mode = False
    do_cache = False
    cache_only = False
    for a in sys.argv[1:]:
        if a == '--all':
            full_mode = True
        elif a == '--rotation':
            do_rotation = True
        elif a == '--cache':
            do_cache = True
        elif a == '--cache-only':
            do_cache = True
            cache_only = True
        elif a.isdigit() and len(a) == 8:
            date_str = a

    if do_rotation:
        print(analyze_rotation())
        return

    if not cache_only:
        print("═══════════════════════════════════")
        print("  游资战法 · 涨停板全景  {}".format(date_str))
        print("═══════════════════════════════════")

    # 数据采集
    df_zt = get_zt_pool(date_str)
    if df_zt.empty:
        if cache_only:
            print(json.dumps({
                "status": "insufficient_data",
                "asof": date_str,
                "reason": "无涨停板数据（可能非交易日）",
            }, ensure_ascii=False))
        else:
            print("⚠️ 无涨停板数据（可能非交易日）")
        return

    if do_cache:
        cache_ok = cache_signal_context(df_zt, date_str)
        if cache_only:
            print(json.dumps({
                "status": "ready" if cache_ok else "error",
                "asof": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}",
                "limitup_total": len(df_zt),
            }, ensure_ascii=False))
            if not cache_ok:
                raise SystemExit(1)
            return

    df_all = get_all_stocks()

    # 输出分析
    print(analyze_board(df_zt))
    print(analyze_quality(df_zt))
    print(analyze_fengdan_top(df_zt))
    print(analyze_sector(df_zt))
    if not df_all.empty:
        print(analyze_market(df_all))
    print(sentiment_judgment(df_zt, df_all))
    print()

if __name__ == '__main__':
    main()
