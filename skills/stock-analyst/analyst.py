#!/usr/bin/env python3
"""
股票分析工具 - 命令行入口
用法：
  python3 analyst.py analyze 600011 华能国际    # 单股技术分析
  python3 analyst.py screen [板块名]             # 板块批量分析
  python3 analyst.py realtime 600011,600027     # 实时行情
  python3 analyst.py zt                         # 今日涨停板
  python3 analyst.py index                      # 大盘指数
  python3 analyst.py fundamental 600011         # 基本面分析
  python3 analyst.py compare [板块名]            # 板块横向对比（基本面+技术面）
  python3 analyst.py chart 600011 [天数]         # K线图
  python3 analyst.py weekly 600011              # 周线级别分析
  python3 analyst.py screener "rsi<30"          # 条件筛选
  python3 analyst.py screener "rs"              # 列出可用的筛选条件
  python3 analyst.py backtest 600011            # 简单回测评分系统
"""
import sys
import os

# 把脚本目录加入路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.data_cache import fetch_realtime, fetch_kline, fetch_zt_pool, fetch_index
from scripts.tech_analysis import analyze_stock, screen_stocks, format_report
from scripts.chart import draw_kline_chart
from scripts.news import search_stock_news, search_sector_news, search_market_news, format_news_with_fundflow as format_news, get_trends

# ─── 预设板块组合 ───

SECTOR_PRESETS = {
    "火电": [("600011","华能国际"),("600027","华电国际"),("601991","大唐发电"),("600023","浙能电力"),("600886","国投电力")],
    "水电": [("600900","长江电力"),("600025","华能水电"),("600886","国投电力"),("600674","川投能源"),("600236","桂冠电力")],
    "电网": [("000400","许继电气"),("600406","国电南瑞"),("600089","特变电工"),("601567","三星医疗"),("600517","国网英大")],
    "空调": [("000651","格力电器"),("000333","美的集团"),("002242","九阳股份")],
    "高温主题": [("600011","华能国际"),("600027","华电国际"),("601991","大唐发电"),("600900","长江电力"),("600025","华能水电"),("000400","许继电气"),("600406","国电南瑞"),("000651","格力电器"),("000333","美的集团")],
    "煤炭": [("000983","山西焦煤"),("600985","淮北矿业"),("601666","平煤股份"),("600546","山煤国际"),("601001","晋控煤业"),("600188","兖矿能源")],
    "封测": [("002156","通富微电"),("600584","长电科技"),("002185","华天科技"),("000021","深科技"),("600667","太极实业")],
    "消费电子": [("002475","立讯精密"),("601138","工业富联"),("002241","歌尔股份"),("300433","蓝思科技"),("002600","领益智造")],
    "半导体": [("688981","中芯国际"),("603501","韦尔股份"),("002371","北方华创"),("300661","圣邦股份"),("688012","中微公司")],
    "AI算力": [("300308","中际旭创"),("601138","工业富联"),("688041","海光信息"),("688256","寒武纪"),("603019","中科曙光")],
    "军工航天": [("601698","中国卫通"),("600118","中国卫星"),("600879","航天电子"),("600765","中航重机"),("000547","航天发展")],
    "新能源": [("300274","阳光电源"),("601012","隆基绿能"),("300750","宁德时代"),("002459","晶澳科技"),("688599","天合光能")],
    "券商金融": [("600030","中信证券"),("601688","华泰证券"),("600999","招商证券"),("300059","东方财富"),("601211","国泰君安")],
    "汽车": [("600104","上汽集团"),("000625","长安汽车"),("601238","广汽集团"),("002594","比亚迪"),("600733","北汽蓝谷")],
}

def cmd_realtime(codes_str=None):
    if codes_str:
        codes = codes_str.split(",")
    else:
        codes = ["600519", "000001", "399001", "399006"]
    data = fetch_realtime(codes)
    for code, info in data.items():
        arrow = "🟢" if info['pct_change'] >= 0 else "🔴"
        print(f"{arrow} {info['name']}({code}): {info['price']:.2f} | {info['pct_change']:+.2f}% | 成交{info['amount']/1e8:.2f}亿 | 换手{info['turnover_rate']}%")

def cmd_analyze(code, name=""):
    rt = fetch_realtime([code])
    result = analyze_stock(code, name, realtime=rt.get(code))
    print(f"\n{'='*60}")
    print(f" {result['name']}({result['code']}) 技术分析")
    print(f"{'='*60}")
    print(f" 现价: {result['price']:.2f} | 今日: {result['pct_change']:+.2f}%")
    if result.get('pct_5d'):
        print(f" 近5日: {result['pct_5d']:+.2f}% | 近10日: {result['pct_10d']:+.2f}%")
    print(f"\n 评级: {result['rating']} (综合分: {result['score']:+})")
    print("\n 关键位置:")
    if result.get('ma5'):
        print(f"   MA5: {result['ma5']}  MA10: {result['ma10']}  MA20: {result['ma20']}")
    if result.get('ma60'):
        print(f"   MA60(趋势线): {result['ma60']}")
    if result.get('support'):
        print(f"   布林下轨(支撑): {result['support']}")
    if result.get('resistance'):
        print(f"   布林上轨(阻力): {result['resistance']}")
    print("\n 技术信号:")
    for k, v in result['signals'].items():
        if k != 'score':
            print(f"   {v}")
    print(f" 数据: {result['data_points']}个交易日")

def cmd_screen(sector_name=None):
    if sector_name and sector_name in SECTOR_PRESETS:
        pairs = SECTOR_PRESETS[sector_name]
        print(f"\n{'='*60}")
        print(f" {sector_name}板块 批量分析")
        print(f"{'='*60}")
    else:
        args = sys.argv[2:]
        pairs = [(args[i], args[i+1]) for i in range(0, len(args)-1, 2)]
        if not pairs:
            pairs = SECTOR_PRESETS.get("高温主题", [])
            print(f"\n{'='*60}")
            print(" 高温主题板块 批量分析")
            print(f"{'='*60}")
    results = screen_stocks(pairs)
    print(format_report(results))
    valid = [r for r in results if 'error' not in r]
    if valid:
        buy = [r for r in valid if '买入' in r.get('rating','')]
        sell = [r for r in valid if '卖出' in r.get('rating','')]
        print(f"\n 统计: {len(valid)}只有效 | 买入建议{len(buy)}只 | 卖出建议{len(sell)}只")

def cmd_zt():
    import datetime
    today = datetime.datetime.now().strftime("%Y%m%d")
    data = fetch_zt_pool(today)
    print(f"\n📊 {today} 涨停板全景 ({len(data)}家)")
    print(f"{'='*60}")
    from collections import Counter
    industry_count = Counter(d.get('所属行业', '未知') for d in data)
    print("\n行业分布 TOP10:")
    for ind, cnt in industry_count.most_common(10):
        print(f"  {ind}: {cnt}家")
    print("\n涨停明细:")
    for d in data[:30]:
        ban = d.get('连板数', '?')
        feng = d.get('封板资金', '')
        feng_str = f" | 封单{feng}亿" if feng else ""
        print(f"  {d.get('名称','?')}({d.get('代码','?')}) | {d.get('涨跌幅','')}% | {ban}板 | {d.get('所属行业','')}{feng_str}")

def cmd_index():
    idx = fetch_index()
    arrow = "🟢" if idx['pct_change'] >= 0 else "🔴"
    print(f"{arrow} {idx['name']}: {idx['price']:.2f} | {idx['pct_change']:+.2f}% | 成交{idx['amount']/1e8:.2f}亿")

# ─── 新增命令 ───

def cmd_fundamental(code, name=""):
    """基本面分析"""
    from scripts.fundamentals import get_full_analysis, format_fundamental
    result = get_full_analysis(code, name)
    print(format_fundamental(result))

def cmd_compare(sector_name=None):
    """板块横向对比（基本面+技术面）"""
    if sector_name and sector_name in SECTOR_PRESETS:
        pairs = SECTOR_PRESETS[sector_name]
    else:
        sector_name = "高温主题"
        pairs = SECTOR_PRESETS[sector_name]

    print(f"\n{'='*80}")
    print(f" 📊 {sector_name}板块 横向对比（基本面+技术面）")
    print(f"{'='*80}")

    from scripts.fundamentals import get_full_analysis, format_brief

    results = []
    for code, name in pairs:
        try:
            r = get_full_analysis(code, name)
            results.append(r)
        except Exception:
            pass

    print(format_brief(results))

def cmd_chart(code, name="", days=60):
    """K线图"""
    print(draw_kline_chart(code, name, days))

def cmd_weekly(code, name=""):
    """周线级别分析"""
    from scripts.tech_analysis import analyze_stock
    klines = fetch_kline(code, 52, period="week")
    if not klines or len(klines) < 5:
        print(f"周线数据不足（{len(klines) if klines else 0}条）")
        return
    print(f"\n{'='*60}")
    print(f" {name or code}({code}) 周线分析")
    print(f"{'='*60}")
    print(f" 周线数据: {len(klines)}周")
    rt = fetch_realtime([code])
    result = analyze_stock(code, name, kline_data=klines, realtime=rt.get(code))
    print(f" 现价: {result['price']:.2f}")
    print(f" 周线评分: {result['rating']} ({result['score']:+})")
    print("\n 关键位置:")
    if result.get('ma5'):
        print(f"   周MA5: {result['ma5']}")
    if result.get('ma10'):
        print(f"   周MA10: {result['ma10']}")
    if result.get('ma20'):
        print(f"   周MA20: {result['ma20']}")
    if result.get('ma60'):
        print(f"   周MA60(趋势线): {result['ma60']}")
    print("\n 信号:")
    for k, v in result['signals'].items():
        if k != 'score':
            print(f"   {v}")

def cmd_screener(query_str=None):
    """条件筛选引擎"""
    if not query_str:
        print("可用筛选条件:")
        print("  rsi<30          RSI低于30（超卖）")
        print("  rsi>70          RSI高于70（超买）")
        print("  ma5>ma20        MA5金叉")
        print("  ma5<ma20        MA5死叉")
        print("  macd_golden     MACD金叉")
        print("  kdj_oversold    KDJ超卖")
        print("  volume_ratio>2  成交量大于2倍均量")
        print("  close<ma20      股价在MA20下方")
        print("  pct_5d<-10      近5日跌超10%")
        print("  score>0         综合评分为正")
        print("  not_banned      排除ST")
        print("  industry=电力   限定行业（实验性）")
        print("\n组合示例:")
        print("  screener \"rsi<30 AND volume_ratio>1.2\"")
        print("  screener \"macd_golden AND ma5>ma20\"")
        print("  screener \"kdj_oversold AND pct_5d<-10\"")
        return

    from scripts.screener import parse_query, screen_by_conditions, format_output
    conditions, logic = parse_query(query_str)
    print(f"🔍 筛选条件: {query_str}")
    print(f"  逻辑: {logic}, 条件数: {len(conditions)}")
    results = screen_by_conditions(conditions, logic)
    if results:
        print("\n" + format_output(results))
    else:
        print("\n❌ 未找到匹配的股票")

def cmd_backtest(code, name=""):
    """简单回测：验证评分系统的历史表现"""
    klines = fetch_kline(code, 360)
    if not klines or len(klines) < 60:
        print("数据不足（需要至少60个交易日）")
        return

    from scripts.tech_analysis import analyze_stock

    print(f"\n{'='*60}")
    print(f" 🔄 评分系统回测 — {name or code}({code})")
    print(f"{'='*60}")
    print(f" 数据区间: {klines[0]['date']} → {klines[-1]['date']}")
    print(f" 总交易日: {len(klines)}")

    # 滚动回测：每20天为一个窗口，模拟买入信号
    buy_signals = 0
    buy_win = 0
    sell_signals = 0
    sell_win = 0
    total_return = 1.0

    results_log = []
    window = 20
    step = 5

    for i in range(window, len(klines) - 20, step):
        window_data = klines[:i]
        future_data = klines[i:i+20]

        if len(window_data) < 30 or len(future_data) < 5:
            continue

        # 模拟分析（不带实时数据）
        result = analyze_stock(code, name, kline_data=window_data)
        score = result.get('score', 0)
        current_price = window_data[-1]['close']
        future_price = future_data[-1]['close']
        future_return = (future_price - current_price) / current_price

        if score >= 2:  # 买入信号
            buy_signals += 1
            if future_return > 0:
                buy_win += 1
            results_log.append(("买入", window_data[-1]['date'], score, future_return * 100))
        elif score <= -2:  # 卖出信号
            sell_signals += 1
            if future_return < 0:
                sell_win += 1
            results_log.append(("卖出", window_data[-1]['date'], score, future_return * 100))

        total_return *= (1 + future_return / 100 * 0.01)  # 模拟持仓

    print("\n 📈 回测结果:")
    print(f"    买入信号触发: {buy_signals}次")
    print(f"    买入胜率: {buy_win/max(buy_signals,1)*100:.1f}%")
    print(f"    卖出信号触发: {sell_signals}次")
    print(f"    卖出胜率: {sell_win/max(sell_signals,1)*100:.1f}%")

    if results_log:
        print("\n 最近5次信号:")
        for sig_type, date, score, ret in results_log[-5:]:
            arrow = "🟢" if ret > 0 else "🔴"
            print(f"    {date} | {sig_type} signal(score={score:+}) | 后续20日: {arrow}{ret:+.1f}%")

# ─── 帮助 ───

def cmd_help():
    print("=" * 60)
    print(" 📊 股票分析工具 — 完整用法")
    print("=" * 60)
    print("")
    print(" [技术面]")
    print("  analyze <code> [name]        # 单股技术分析")
    print("  weekly <code> [name]         # 周线级别分析")
    print("  screen [板块名]               # 板块批量分析")
    print("  realtime [codes]             # 实时行情")
    print("  chart <code> [days]          # K线图")
    print("")
    print(" [基本面]")
    print("  fundamental <code> [name]    # 基本面分析（PE/ROE/营收增速）")
    print("  compare [板块名]              # 板块横向对比（基本面+技术面）")
    print("")
    print(" [全市场]")
    print('  screener "条件1 AND 条件2"    # 条件筛选引擎')
    print("  screener                     # 列出可用条件")
    print("")
    print(" [新闻]")
    print("  news <code> [name]           # 个股最新新闻")
    print("  news sector <板块名>          # 板块新闻")
    print("  news market                  # 大盘新闻")
    print("  news trend <关键词>          # 搜索热度趋势")
    print("")
    print(" [数据]")
    print("  zt                           # 今日涨停板")
    print("  index                        # 大盘指数")
    print("  backtest <code>              # 回测评分系统")
    print("")
    print(f" 预设板块: {', '.join(SECTOR_PRESETS.keys())}")

# ─── 主入口 ───

if __name__ == "__main__":
    if len(sys.argv) < 2:
        cmd_help()
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "realtime":
        cmd_realtime(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "analyze":
        code = sys.argv[2] if len(sys.argv) > 2 else "600900"
        name = sys.argv[3] if len(sys.argv) > 3 else ""
        cmd_analyze(code, name)
    elif cmd == "screen":
        sector = sys.argv[2] if len(sys.argv) > 2 else None
        cmd_screen(sector)
    elif cmd == "zt":
        cmd_zt()
    elif cmd == "index":
        cmd_index()
    elif cmd == "fundamental":
        code = sys.argv[2] if len(sys.argv) > 2 else "600519"
        name = sys.argv[3] if len(sys.argv) > 3 else ""
        cmd_fundamental(code, name)
    elif cmd == "compare":
        sector = sys.argv[2] if len(sys.argv) > 2 else None
        cmd_compare(sector)
    elif cmd == "chart":
        code = sys.argv[2] if len(sys.argv) > 2 else "600519"
        name = sys.argv[3] if len(sys.argv) > 3 else ""
        days = int(sys.argv[4]) if len(sys.argv) > 4 else 60
        cmd_chart(code, name, days)
    elif cmd == "weekly":
        code = sys.argv[2] if len(sys.argv) > 2 else "600900"
        name = sys.argv[3] if len(sys.argv) > 3 else ""
        cmd_weekly(code, name)
    elif cmd == "screener":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else None
        cmd_screener(query)
    elif cmd == "backtest":
        code = sys.argv[2] if len(sys.argv) > 2 else "600519"
        name = sys.argv[3] if len(sys.argv) > 3 else ""
        cmd_backtest(code, name)
    elif cmd == "news":
        if len(sys.argv) > 2 and sys.argv[2] == "sector" and len(sys.argv) > 3:
            news = search_sector_news(sys.argv[3])
            print(format_news(news, f"{sys.argv[3]}板块新闻"))
        elif len(sys.argv) > 2 and sys.argv[2] == "market":
            news = search_market_news()
            print(format_news(news, "A股大盘新闻"))
        elif len(sys.argv) > 2 and sys.argv[2] == "trend" and len(sys.argv) > 3:
            t = get_trends(sys.argv[3])
            if t:
                print(f"\n📈 {t['keyword']} 搜索热度趋势")
                print(f"   当前: {t['current']}/100 | 峰值: {t['peak']}/100")
                for r in t['trend'][-5:]:
                    bar = "█" * max(1, int(r['value']) // 5)
                    print(f"   {r['date']}: {r['value']:>3} {bar}")
            else:
                print("趋势数据获取失败")
        else:
            code = sys.argv[2] if len(sys.argv) > 2 else "600519"
            name = sys.argv[3] if len(sys.argv) > 3 else ""
            news = search_stock_news(code, name)
            print(format_news(news, f"{name or code} 最新新闻"))
    elif cmd in ("-h", "--help", "help"):
        cmd_help()
    else:
        cmd_screen(cmd)
