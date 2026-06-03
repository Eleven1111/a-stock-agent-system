"""
全市场板块扫描脚本
基于涨停板数据的行业热度分析 + 基础行情采集
"""
import akshare as ak
import os
from collections import defaultdict

# 清洗代理环境变量
for k in ['HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY','all_proxy']:
    os.environ.pop(k, None)

def scan_sectors(dates=None):
    """全量板块扫描"""
    if dates is None:
        from datetime import datetime, timedelta
        today = datetime.now()
        dates = []
        # 生成最近7个交易日（包含今天，跳过周末）
        d = today
        while len(dates) < 7:
            if d.weekday() < 5:  # 周一到周五
                dates.append(d.strftime('%Y%m%d'))
            d -= timedelta(days=1)

    industry_data = defaultdict(lambda: {
        'count': 0, 'stocks': [], 'max_lianban': 0, 'days_appeared': set()
    })

    print("采集涨停数据中...")
    for d in dates:
        try:
            df = ak.stock_zt_pool_em(date=d)
            if df is not None and not df.empty:
                if '所属行业' in df.columns:
                    industries = df['所属行业'].value_counts()
                    for ind, cnt in industries.items():
                        print(f"  {d}: {ind} x{cnt}")
                for _, r in df.iterrows():
                    ind = r.get('所属行业', '未知')
                    industry_data[ind]['count'] += 1
                    industry_data[ind]['stocks'].append(r.get('名称', ''))
                    lb = r.get('连板数', 0)
                    try:
                        lb = int(lb)
                    except:
                        lb = 0
                    if lb > industry_data[ind]['max_lianban']:
                        industry_data[ind]['max_lianban'] = lb
                    industry_data[ind]['days_appeared'].add(d)
            else:
                print(f"  {d}: 无数据")
        except Exception as e:
            print(f"  {d}: {e}")

    # 热度评分
    print("\n" + "=" * 95)
    print("【全市场板块热度扫描】基于近7日涨停数据")
    print("=" * 95)
    print(f"{'板块名称':<16} {'涨停数':>6} {'天数':>4} {'最高板':>6} {'热度分':>6} {'代表股'}")
    print("-" * 95)

    ranked = []
    for ind, data in industry_data.items():
        days_score = len(data['days_appeared']) / len(dates) * 10
        count_score = min(data['count'] / 5, 8)
        lb_score = min(data['max_lianban'], 6)
        total = round(count_score + days_score + lb_score, 1)
        top_stocks = list(set(data['stocks']))[:3]
        ranked.append((total, ind, data['count'], len(data['days_appeared']), data['max_lianban'], top_stocks))

    ranked.sort(key=lambda x: x[0], reverse=True)

    for i, (total, ind, cnt, days, max_lb, stocks) in enumerate(ranked):
        # 热度等级
        if total >= 14: level = "🔥🔥🔥"
        elif total >= 10: level = "🔥🔥"
        elif total >= 6: level = "🔥"
        elif total >= 3: level = "🌤"
        else: level = "❄️"

        print(f"{level} {ind:<14s} {cnt:>4d} {days:>3d}天 {max_lb:>3d}板 {total:>5.1f}  {stocks[0] if stocks else ''}")
        if len(stocks) > 1:
            print(f"   {'':16s} {'':>6} {'':>4} {'':>6} {'':>6}   {stocks[1]}")
        if len(stocks) > 2:
            print(f"   {'':16s} {'':>6} {'':>4} {'':>6} {'':>6}   {stocks[2]}")

    print("\n" + "=" * 95)
    print("热度分说明：涨停数权重+出现天数权重+最高连板权重")
    print("🔥🔥🔥=14分以上 🔥🔥=10-14分 🔥=6-10分 🌤=3-6分 ❄️=3分以下")

    return ranked

if __name__ == "__main__":
    scan_sectors()
