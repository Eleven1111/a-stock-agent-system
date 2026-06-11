#!/usr/bin/env python3
"""
资讯监控 v3 — 全量覆盖新浪财经+东方财富+百度热搜，深度触发词。

信源：
  ✅ 新浪财经快讯 — feed.mix.sina.com.cn（10+分类 × 50条）
  ✅ 东方财富要闻 — push2ex.eastmoney.com（3页 × 30条）
  ✅ 百度热搜 — top.baidu.com（前30）

触发词分级（120+关键词）：
  T1（🔴致命）: 加息/降息/战争/制裁/崩盘/国常会/政治局/数据泄露/事故/疫情
  T2（🟡重要）: 政策/监管/重组/定增/减持/解禁/贸易/关税/调查/诉讼/暴雷
  T3（🟢关注）: 行业关键术语+龙头公司名称

Usage:
  python3 news_monitor_v3.py           # 默认输出
  python3 news_monitor_v3.py --json    # JSON格式
  python3 news_monitor_v3.py --silent  # 无信号不输出
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
BJ = timezone(timedelta(hours=8))
SILENT = "--silent" in sys.argv

# ========== 触发词分级（150+关键词）==========
TRIGGER_T1 = [
    # 宏观
    "加息", "降息", "降准", "特别国债", "大规模刺激", "经济危机", "金融危机",
    "债务违约", "汇率干预", "资本管制",
    # 地缘政治
    "战争", "制裁", "军事冲突", "宣战", "核试验", "核设施", "袭击", "断交",
    # 市场极端
    "崩盘", "熔断", "救市", "停牌潮",
    # 政策顶层
    "国常会", "政治局", "党代会", "全会", "中央经济工作会议",
    # 系统性灾难
    "地震", "洪灾", "疫情", "封城", "核泄漏",
    # 金融系统性风险
    "挤兑", "资不抵债", "央行",
]

TRIGGER_T2 = [
    # 政策法规
    "政策", "监管", "约谈", "立案调查", "反垄断", "反不正当竞争",
    "证监会", "银保监会", "发改委", "商务部", "国务院", "外交部",
    # 交易所监管动作
    "问询", "问询函", "关注函", "监管函", "警示函", "责令改正",
    "通报批评", "公开谴责", "信披", "信息披露", "违规",
    # 异常公告（A股风险信号）
    "异常公告", "异常波动", "风险提示", "停牌核查",
    "业绩预告修正", "业绩修正", "修正预告",
    # 资本运作
    "重组", "定增", "减持", "解禁", "增持", "回购", "并购", "拆分上市",
    "退市", "停牌", "借壳", "股权激励", "配股",
    # 财务
    "业绩预告", "业绩快报", "业绩变脸", "亏损", "盈利预警",
    "审计", "问询函", "关注函", "监管函", "处罚", "保留意见",
    # 贸易/国际
    "贸易战", "关税", "反倾销", "出口管制", "实体清单", "进出口",
    "自贸区", "RCEP", "一带一路",
    # 供应链
    "断供", "缺芯", "供应链", "产能", "涨价潮", "价格战",
    # 劳动/社会
    "罢工", "裁员", "大规模", "调查",
    # 其他
    "诉讼", "仲裁", "专利", "知识产权",
    "产业基金", "大基金", "国家基金", "专项债", "地方债", "央企",
    # 公司风险（单公司事件不应T1，但需T2）
    "数据泄露", "数据安全", "用户数据", "隐私泄露",
    "重大事故", "爆炸", "违约", "暴雷", "暴跌",
    # 网络安全（政府级）
    "网络安全审查",
]

TRIGGER_T3 = [
    # 行业
    "新能源", "半导体", "AI", "人工智能", "大数据", "云计算", "区块链",
    "医药", "创新药", "医疗器械", "生物制药",
    "消费", "零售", "电商", "直播", "外卖", "餐饮", "旅游", "酒店",
    "军工", "航天", "卫星", "导航", "导弹",
    "芯片", "封测", "晶圆", "光刻", "GPU", "CPU", "算力",
    "光伏", "逆变器", "硅料", "钙钛矿",
    "锂电池", "电池", "电解液", "隔膜", "正极", "负极",
    "新能源汽车", "自动驾驶", "智能驾驶", "激光雷达", "毫米波",
    "储能", "氢能", "风电", "核电", "电力",
    "粮食", "农业", "种业", "化肥", "农药",
    "能源", "石油", "天然气", "煤炭", "稀土", "有色金属",
    "数据要素", "数字经济", "数字人民币",
    "机器人", "人形机器人", "工业互联网",
    "金融", "银行", "保险", "证券", "基金", "信托",
    "房地产", "基建", "城中村", "保障房",
    "通信", "5G", "6G", "卫星通信", "光通信", "光纤",
    "游戏", "教育", "教培", "互联网", "社交媒体",
    # 龙头公司
    "阿里巴巴", "阿里", "蚂蚁", "支付宝", "淘宝", "天猫", "菜鸟",
    "腾讯", "微信", "QQ", "王者荣耀", "视频号",
    "字节", "抖音", "TikTok", "飞书",
    "百度", "文心", "萝卜快跑",
    "华为", "鸿蒙", "问界", "麒麟",
    "宁德时代", "比亚迪", "特斯拉", "小米", "蔚来", "理想", "小鹏",
    "中芯", "台积电", "ASML", "英伟达", "AMD", "高通", "英特尔",
    "工商银行", "农业银行", "中国银行", "建设银行", "招商银行", "农业", "邮储",
    "茅台", "五粮液", "伊利", "蒙牛",
    "京东", "美团", "哔哩哔哩", "快手", "拼多多", "百度",
    "中国移动", "中国电信", "中国联通",
    "中石油", "中石化", "中海油",
    "国电", "国网", "南网", "华能", "长江电力",
    "联想", "浪潮", "紫光", "海康", "大华", "商汤",
    "钉钉", "企业微信", "飞书",
]

ALL_TRIGGERS = TRIGGER_T1 + TRIGGER_T2 + TRIGGER_T3


def fetch_sina_finance() -> list:
    """拉取新浪财经多个分类的快讯"""
    # 财经类分类ID
    lids = [
        2509,   # 财经-要闻
        2510,   # 财经-市场
    ]
    all_items = []
    for lid in lids:
        for page in range(3):  # 每类3页 × 50条 = 150条
            try:
                url = f"https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid={lid}&num=50&versionNumber=1.2.4&page={page+1}"
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read())
                items = data.get("result", {}).get("data", [])
                if not items:
                    break
                for item in items:
                    all_items.append({
                        "title": item.get("title", ""),
                        "ctime": item.get("ctime", ""),
                        "source": "新浪财经",
                    })
            except:
                break
    return all_items


def fetch_eastmoney_news() -> list:
    """拉取东方财富要闻"""
    items = []
    for page in range(3):  # 3页 × 30条 = 90条
        try:
            url = f"https://push2ex.eastmoney.com/getNews?type=1&page={page+1}&pageSize=30"
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=10) as r:
                news_data = json.loads(r.read())
            for article in news_data.get("data", {}).get("list", []):
                items.append({
                    "title": article.get("title", ""),
                    "ctime": article.get("date", ""),
                    "source": "东方财富",
                })
        except:
            break
    return items


def fetch_baidu_hot() -> list:
    """拉取百度热搜"""
    items = []
    try:
        url = "https://top.baidu.com/board?tab=realtime&sa=fyb_realtime_31065"
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8", errors="ignore")
        # 从HTML中提取热搜标题
        titles = re.findall(r'<div[^>]*class="c-single-text-ellipsis"[^>]*>(.*?)</div>', html)
        for t in titles[:30]:
            clean = re.sub(r'<.*?>', '', t).strip()
            if clean:
                items.append({
                    "title": clean,
                    "ctime": "",
                    "source": "百度热搜",
                })
    except:
        pass
    return items


def score_news(title: str) -> dict:
    """对一条新闻评分，返回匹配的触发词和级别"""
    matched = []
    level = None
    for t in TRIGGER_T1:
        if t in title:
            matched.append(t)
            level = "T1" if not level else level
    for t in TRIGGER_T2:
        if t in title:
            matched.append(t)
            level = "T2" if not level or level == "T3" else level
    for t in TRIGGER_T3:
        if t in title:
            matched.append(t)
            level = "T3" if not level else level

    return {"matched": matched, "level": level} if matched else None


# ========== 主流程 ==========
now = datetime.now(BJ)

# 1. 采集（全量，三信源）
all_news = []
all_news.extend(fetch_sina_finance())
all_news.extend(fetch_eastmoney_news())
all_news.extend(fetch_baidu_hot())

# 2. 去重+评分
seen = set()
hits = []
for news in all_news:
    title = news["title"].strip()
    if not title or title in seen:
        continue
    seen.add(title)
    result = score_news(title)
    if result:
        hits.append({
            "title": title,
            "source": news["source"],
            "level": result["level"],
            "matched": result["matched"],
        })

# 3. 按级别排序
level_order = {"T1": 0, "T2": 1, "T3": 2}
hits.sort(key=lambda x: level_order.get(x["level"], 99))

# 4. 输出
if "--json" in sys.argv:
    output = {
        "asof": now.strftime("%Y-%m-%d %H:%M"),
        "total_scanned": len(all_news),
        "total_hits": len(hits),
        "t1_count": sum(1 for h in hits if h["level"] == "T1"),
        "t2_count": sum(1 for h in hits if h["level"] == "T2"),
        "t3_count": sum(1 for h in hits if h["level"] == "T3"),
        "hits": hits[:20],
    }
    print(json.dumps(output, ensure_ascii=False))
elif hits:
    t1 = sum(1 for h in hits if h["level"] == "T1")
    t2 = sum(1 for h in hits if h["level"] == "T2")
    t3 = sum(1 for h in hits if h["level"] == "T3")
    print(f"📡 资讯监控 | {now.strftime('%m/%d %H:%M')}")
    print(f"扫描{len(all_news)}条 | 触发{t1+t2+t3}条 (🔴{t1} 🟡{t2} 🟢{t3})")
    print()
    for h in hits[:15]:
        icon = {"T1": "🔴", "T2": "🟡", "T3": "🟢"}.get(h["level"], "⚪")
        tags = ", ".join(h["matched"][:3])
        print(f"{icon} [{h['source']}] {h['title'][:100]}")
        if len(h["matched"]) > 3:
            print(f"   触发: {tags} (+{len(h['matched'])-3}个)")
elif not SILENT:
    print(f"📡 资讯监控 | {now.strftime('%m/%d %H:%M')} — 无触发新闻")
