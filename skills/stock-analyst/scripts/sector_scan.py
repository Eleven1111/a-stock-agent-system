"""
全市场板块扫描脚本
基于涨停板数据的行业热度分析 + 同花顺板块列表 + 腾讯实时行情

数据源：
- 涨停板聚合: AkShare stock_zt_pool_em (push2ex，通)
- 板块列表: AkShare stock_board_industry_name_ths (同花顺)
- 实时行情: 腾讯 qt.gtimg.cn
"""
import sys
import os
from collections import defaultdict, Counter

# 清洗代理环境变量
for k in ['HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY','all_proxy']:
    os.environ.pop(k, None)

# 添加项目路径
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_DIR)

from data_cache import fetch_zt_pool, fetch_realtime, fetch_sectors_ths, fetch_kline, get_db, fetch_stock_list


def scan_from_zt_pool(dates=None):
    """从涨停板数据聚合板块热度"""
    if dates is None:
        from datetime import datetime, timedelta
        today = datetime.now()
        dates = []
        d = today
        while len(dates) < 7:
            if d.weekday() < 5:
                dates.append(d.strftime('%Y%m%d'))
            d -= timedelta(days=1)
    
    industry_data = defaultdict(lambda: {
        'count': 0, 'stocks': [], 'max_lianban': 0, 'days_appeared': set()
    })
    
    print("采集涨停数据中...")
    for d in dates:
        try:
            data = fetch_zt_pool(d)
            if data:
                ind_count = Counter(item.get('所属行业', '未知') for item in data)
                for ind, cnt in ind_count.items():
                    print(f"  {d}: {ind} x{cnt}")
                for item in data:
                    ind = item.get('所属行业', '未知')
                    industry_data[ind]['count'] += 1
                    industry_data[ind]['stocks'].append(item.get('名称', ''))
                    try:
                        lb = int(item.get('连板数', 0))
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


def list_th_sectors():
    """列出同花顺所有行业板块"""
    sectors = fetch_sectors_ths()
    ind = sectors.get("industry", [])
    con = sectors.get("concept", [])
    print(f"同花顺行业板块: {len(ind)}个")
    print(f"同花顺概念板块: {len(con)}个")
    print("\n行业板块 TOP20:")
    for s in ind[:20]:
        print(f"  {s['name']} ({s['code']})")
    print(f"\n概念板块 TOP20:")
    for s in con[:20]:
        print(f"  {s['name']} ({s['code']})")
    return sectors


def scan_sectors(dates=None):
    """兼容旧接口：直接调用原扫描"""
    return scan_from_zt_pool(dates)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    
    if cmd == "scan":
        scan_from_zt_pool()
    elif cmd == "list":
        list_th_sectors()
    elif cmd == "stocks":
        stocks = fetch_stock_list()
        print(f"全部A股: {len(stocks)}只")
    else:
        scan_from_zt_pool()
