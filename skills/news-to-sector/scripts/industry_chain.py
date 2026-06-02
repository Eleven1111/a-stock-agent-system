"""
产业链知识图谱 — 商品→受影响板块的映射
每条链包含：
- commodity: 商品关键词
- chains: 上涨时和下跌时的传导路径
  - sector: 东方财富板块名称
  - direction: "bullish"/"bearish" 涨时利好还是利空
  - strength: 1-5 传导强度
  - reasoning: 逻辑说明
  - lag: "immediate"/"short"/"medium" 滞后程度
"""

INDUSTRY_CHAINS = [
    # ==================== 黑色系 ====================
    {
        "commodity": "焦煤",
        "keywords": ["焦煤", "焦煤期货", "焦煤主力"],
        "chains_up": [
            {"sector": "煤炭开采", "direction": "bullish", "strength": 5,
             "reasoning": "焦煤是煤炭开采企业的直接产品，涨价直接提升利润",
             "lag": "immediate"},
            {"sector": "焦化", "direction": "bullish", "strength": 4,
             "reasoning": "焦化企业库存升值，但需确认成本传导是否顺畅",
             "lag": "immediate"},
            {"sector": "煤化工", "direction": "bullish", "strength": 3,
             "reasoning": "部分煤化工产品跟涨，但传导有滞后",
             "lag": "short"},
            {"sector": "钢铁", "direction": "bearish", "strength": 4,
             "reasoning": "焦煤是钢铁冶炼核心原料，成本暴涨压缩利润",
             "lag": "immediate"},
            {"sector": "建筑材料", "direction": "bearish", "strength": 2,
             "reasoning": "钢铁成本上升传递至建材，次级传导",
             "lag": "medium"},
        ],
        "chains_down": [
            {"sector": "钢铁", "direction": "bullish", "strength": 4,
             "reasoning": "原料成本下降，利润改善",
             "lag": "immediate"},
            {"sector": "建筑材料", "direction": "bullish", "strength": 2,
             "reasoning": "建材成本端压力减轻，次级利好",
             "lag": "medium"},
            {"sector": "煤炭开采", "direction": "bearish", "strength": 4,
             "reasoning": "产品降价，利润压缩",
             "lag": "immediate"},
            {"sector": "焦化", "direction": "bearish", "strength": 3,
             "reasoning": "产品端降价，利润承压",
             "lag": "immediate"},
        ],
    },
    # ==================== 钢铁 ====================
    {
        "commodity": "螺纹钢",
        "keywords": ["螺纹钢", "螺纹", "钢筋", "热卷", "线材"],
        "chains_up": [
            {"sector": "钢铁", "direction": "bullish", "strength": 5,
             "reasoning": "钢铁产品直接涨价，利润增厚",
             "lag": "immediate"},
            {"sector": "煤炭开采", "direction": "bullish", "strength": 2,
             "reasoning": "钢铁增产预期拉动焦煤需求",
             "lag": "short"},
            {"sector": "房地产开发", "direction": "bearish", "strength": 3,
             "reasoning": "建材成本上升压制地产利润",
             "lag": "short"},
            {"sector": "汽车", "direction": "bearish", "strength": 2,
             "reasoning": "用钢成本上升，次级传导",
             "lag": "medium"},
            {"sector": "家电", "direction": "bearish", "strength": 2,
             "reasoning": "钢材是家电主要原材料成本",
             "lag": "medium"},
            {"sector": "机械", "direction": "bearish", "strength": 2,
             "reasoning": "机械制造用钢成本上升",
             "lag": "medium"},
        ],
        "chains_down": [
            {"sector": "钢铁", "direction": "bearish", "strength": 4,
             "reasoning": "钢价下跌，利润承压",
             "lag": "immediate"},
            {"sector": "房地产开发", "direction": "bullish", "strength": 3,
             "reasoning": "建材成本下降，利润改善",
             "lag": "short"},
            {"sector": "汽车", "direction": "bullish", "strength": 2,
             "reasoning": "用钢成本下降",
             "lag": "medium"},
            {"sector": "家电", "direction": "bullish", "strength": 2,
             "reasoning": "原材料成本下降",
             "lag": "medium"},
        ],
    },
    # ==================== 原油 ====================
    {
        "commodity": "原油",
        "keywords": ["原油", "石油", "WTI", "布伦特", "原油期货", "国际油价"],
        "chains_up": [
            {"sector": "石油开采", "direction": "bullish", "strength": 5,
             "reasoning": "油价直接决定开采企业利润",
             "lag": "immediate"},
            {"sector": "石油化工", "direction": "bullish", "strength": 4,
             "reasoning": "化工品价格随油价上涨，库存升值",
             "lag": "immediate"},
            {"sector": "油气设服", "direction": "bullish", "strength": 3,
             "reasoning": "高油价推动上游资本开支增加",
             "lag": "short"},
            {"sector": "航空运输", "direction": "bearish", "strength": 4,
             "reasoning": "航油成本是航空公司最大可变成本",
             "lag": "immediate"},
            {"sector": "交通运输", "direction": "bearish", "strength": 3,
             "reasoning": "物流运输燃油成本上升",
             "lag": "immediate"},
            {"sector": "化工", "direction": "bullish", "strength": 3,
             "reasoning": "化工品价格跟涨，但原料成本同步上升",
             "lag": "short"},
        ],
        "chains_down": [
            {"sector": "石油开采", "direction": "bearish", "strength": 5,
             "reasoning": "油价下跌直接冲击利润",
             "lag": "immediate"},
            {"sector": "航空运输", "direction": "bullish", "strength": 4,
             "reasoning": "航油成本大幅下降",
             "lag": "immediate"},
            {"sector": "交通运输", "direction": "bullish", "strength": 3,
             "reasoning": "燃油成本下降",
             "lag": "immediate"},
            {"sector": "化工", "direction": "bearish", "strength": 2,
             "reasoning": "产品端降价压力增大",
             "lag": "short"},
        ],
    },
    # ==================== 铜 ====================
    {
        "commodity": "铜",
        "keywords": ["铜", "沪铜", "国际铜", "电解铜", "铜期货"],
        "chains_up": [
            {"sector": "有色金属", "direction": "bullish", "strength": 5,
             "reasoning": "铜是核心品种，涨价直接增厚有色板块利润",
             "lag": "immediate"},
            {"sector": "工业金属", "direction": "bullish", "strength": 4,
             "reasoning": "铜价上涨带动其他工业金属情绪",
             "lag": "immediate"},
            {"sector": "电力", "direction": "bearish", "strength": 3,
             "reasoning": "铜是电力设备核心原材料，成本承压",
             "lag": "short"},
            {"sector": "电网设备", "direction": "bearish", "strength": 3,
             "reasoning": "变压器、电缆等用铜大户成本上升",
             "lag": "short"},
            {"sector": "新能源", "direction": "bearish", "strength": 2,
             "reasoning": "光伏、风电用铜量大，成本承压",
             "lag": "medium"},
        ],
        "chains_down": [
            {"sector": "有色金属", "direction": "bearish", "strength": 4,
             "reasoning": "核心品种下跌拖累板块",
             "lag": "immediate"},
            {"sector": "电力", "direction": "bullish", "strength": 3,
             "reasoning": "原材料成本下降",
             "lag": "short"},
            {"sector": "电网设备", "direction": "bullish", "strength": 3,
             "reasoning": "成本端减压",
             "lag": "short"},
        ],
    },
    # ==================== 黄金 ====================
    {
        "commodity": "黄金",
        "keywords": ["黄金", "金价", "国际金价", "COMEX黄金", "沪金"],
        "chains_up": [
            {"sector": "黄金", "direction": "bullish", "strength": 5,
             "reasoning": "金价直接决定黄金股利润",
             "lag": "immediate"},
            {"sector": "有色金属", "direction": "bullish", "strength": 3,
             "reasoning": "黄金上涨带动贵金属板块情绪",
             "lag": "immediate"},
            {"sector": "珠宝首饰", "direction": "bullish", "strength": 2,
             "reasoning": "库存升值，但需求端可能受高价抑制",
             "lag": "short"},
        ],
        "chains_down": [
            {"sector": "黄金", "direction": "bearish", "strength": 4,
             "reasoning": "金价下跌压缩利润",
             "lag": "immediate"},
        ],
    },
    # ==================== 锂 ====================
    {
        "commodity": "碳酸锂",
        "keywords": ["碳酸锂", "锂", "锂价", "锂盐", "氢氧化锂", "锂期货"],
        "chains_up": [
            {"sector": "能源金属", "direction": "bullish", "strength": 5,
             "reasoning": "碳酸锂是锂矿企业的核心产品",
             "lag": "immediate"},
            {"sector": "小金属", "direction": "bullish", "strength": 3,
             "reasoning": "板块联动效应",
             "lag": "immediate"},
            {"sector": "电池", "direction": "bearish", "strength": 3,
             "reasoning": "正极材料成本上升",
             "lag": "short"},
            {"sector": "新能源车", "direction": "bearish", "strength": 2,
             "reasoning": "电池成本上升传递至整车",
             "lag": "medium"},
        ],
        "chains_down": [
            {"sector": "能源金属", "direction": "bearish", "strength": 4,
             "reasoning": "产品跌价利润压缩",
             "lag": "immediate"},
            {"sector": "电池", "direction": "bullish", "strength": 3,
             "reasoning": "原材料成本下降，利润改善",
             "lag": "short"},
            {"sector": "新能源车", "direction": "bullish", "strength": 2,
             "reasoning": "整车制造成本下降",
             "lag": "medium"},
        ],
    },
    # ==================== 动力煤 ====================
    {
        "commodity": "动力煤",
        "keywords": ["动力煤", "电煤", "煤炭", "郑煤"],
        "chains_up": [
            {"sector": "煤炭开采", "direction": "bullish", "strength": 5,
             "reasoning": "动力煤是煤企核心产品",
             "lag": "immediate"},
            {"sector": "火电", "direction": "bearish", "strength": 4,
             "reasoning": "燃料成本是火电最大支出",
             "lag": "immediate"},
            {"sector": "电解铝", "direction": "bearish", "strength": 3,
             "reasoning": "电力成本占比高，煤价涨推高电价",
             "lag": "short"},
            {"sector": "化工", "direction": "bearish", "strength": 2,
             "reasoning": "煤化工原料成本上升",
             "lag": "short"},
        ],
        "chains_down": [
            {"sector": "火电", "direction": "bullish", "strength": 4,
             "reasoning": "燃料成本下降，利润改善",
             "lag": "immediate"},
            {"sector": "电解铝", "direction": "bullish", "strength": 3,
             "reasoning": "电力成本下降",
             "lag": "short"},
            {"sector": "煤炭开采", "direction": "bearish", "strength": 4,
             "reasoning": "产品降价利润压缩",
             "lag": "immediate"},
        ],
    },
    # ==================== 农产品 ====================
    {
        "commodity": "豆粕",
        "keywords": ["豆粕", "大豆", "豆油", "豆一", "豆二", "豆粕期货"],
        "chains_up": [
            {"sector": "种植业", "direction": "bullish", "strength": 3,
             "reasoning": "农产品涨价利好种植企业",
             "lag": "immediate"},
            {"sector": "饲料", "direction": "bearish", "strength": 4,
             "reasoning": "豆粕是饲料核心蛋白原料",
             "lag": "immediate"},
            {"sector": "养殖业", "direction": "bearish", "strength": 3,
             "reasoning": "饲料成本上升压缩养殖利润",
             "lag": "short"},
            {"sector": "食品加工", "direction": "bearish", "strength": 2,
             "reasoning": "原料成本上升",
             "lag": "medium"},
        ],
        "chains_down": [
            {"sector": "饲料", "direction": "bullish", "strength": 4,
             "reasoning": "原料成本下降",
             "lag": "immediate"},
            {"sector": "养殖业", "direction": "bullish", "strength": 3,
             "reasoning": "养殖成本下降",
             "lag": "short"},
            {"sector": "种植业", "direction": "bearish", "strength": 2,
             "reasoning": "农产品降价",
             "lag": "immediate"},
        ],
    },
    # ==================== 航运 ====================
    {
        "commodity": "航运",
        "keywords": ["航运", "海运费", "波罗的海", "BDI", "集装箱", "海运"],
        "chains_up": [
            {"sector": "航运", "direction": "bullish", "strength": 5,
             "reasoning": "运费是航运公司直接收入来源",
             "lag": "immediate"},
            {"sector": "物流", "direction": "bullish", "strength": 2,
             "reasoning": "物流运价联动上涨",
             "lag": "short"},
            {"sector": "港口", "direction": "bullish", "strength": 2,
             "reasoning": "港口吞吐量和费率受益",
             "lag": "short"},
            {"sector": "外贸", "direction": "bearish", "strength": 3,
             "reasoning": "出口企业运费成本上升",
             "lag": "short"},
            {"sector": "跨境电商", "direction": "bearish", "strength": 2,
             "reasoning": "物流成本上升压缩利润",
             "lag": "short"},
        ],
        "chains_down": [
            {"sector": "航运", "direction": "bearish", "strength": 4,
             "reasoning": "运费下降压缩收入",
             "lag": "immediate"},
            {"sector": "外贸", "direction": "bullish", "strength": 3,
             "reasoning": "出口物流成本下降",
             "lag": "short"},
            {"sector": "跨境电商", "direction": "bullish", "strength": 2,
             "reasoning": "跨境物流成本下降",
             "lag": "short"},
        ],
    },
    # ==================== 玻璃/纯碱 ====================
    {
        "commodity": "纯碱",
        "keywords": ["纯碱", "玻璃", "纯碱期货", "玻璃期货"],
        "chains_up": [
            {"sector": "化工", "direction": "bullish", "strength": 3,
             "reasoning": "纯碱是化工品，涨价利好化工板块",
             "lag": "immediate"},
            {"sector": "玻璃", "direction": "bearish", "strength": 4,
             "reasoning": "纯碱是玻璃生产核心原料",
             "lag": "immediate"},
            {"sector": "光伏", "direction": "bearish", "strength": 2,
             "reasoning": "光伏玻璃成本上升",
             "lag": "short"},
        ],
        "chains_down": [
            {"sector": "玻璃", "direction": "bullish", "strength": 4,
             "reasoning": "原料成本下降",
             "lag": "immediate"},
            {"sector": "化工", "direction": "bearish", "strength": 2,
             "reasoning": "化工品跌价",
             "lag": "immediate"},
        ],
    },
    # ==================== 生猪 ====================
    {
        "commodity": "生猪",
        "keywords": ["生猪", "猪肉", "猪价", "生猪期货", "猪肉价格"],
        "chains_up": [
            {"sector": "养殖业", "direction": "bullish", "strength": 5,
             "reasoning": "猪价是养殖企业核心利润驱动",
             "lag": "immediate"},
            {"sector": "饲料", "direction": "bearish", "strength": 2,
             "reasoning": "养殖扩产拉动饲料需求，但逻辑偏弱",
             "lag": "medium"},
            {"sector": "食品加工", "direction": "bearish", "strength": 2,
             "reasoning": "原料成本上升",
             "lag": "short"},
            {"sector": "肉制品", "direction": "bearish", "strength": 3,
             "reasoning": "猪肉采购成本上升",
             "lag": "immediate"},
        ],
        "chains_down": [
            {"sector": "养殖业", "direction": "bearish", "strength": 4,
             "reasoning": "猪周期下行，亏损加剧",
             "lag": "immediate"},
            {"sector": "肉制品", "direction": "bullish", "strength": 3,
             "reasoning": "原料成本下降，利润改善",
             "lag": "immediate"},
            {"sector": "食品加工", "direction": "bullish", "strength": 2,
             "reasoning": "成本端减压",
             "lag": "short"},
        ],
    },
    # ==================== 铁矿石 ====================
    {
        "commodity": "铁矿石",
        "keywords": ["铁矿石", "铁矿", "铁矿石期货", "大商所铁矿"],
        "chains_up": [
            {"sector": "钢铁", "direction": "bearish", "strength": 4,
             "reasoning": "铁矿石是钢铁主要原料，涨价压缩利润",
             "lag": "immediate"},
            {"sector": "有色金属", "direction": "bullish", "strength": 2,
             "reasoning": "情绪传导，矿类板块联动",
             "lag": "immediate"},
            {"sector": "建筑材料", "direction": "bearish", "strength": 2,
             "reasoning": "钢铁成本上升次级传导至建材",
             "lag": "medium"},
        ],
        "chains_down": [
            {"sector": "钢铁", "direction": "bullish", "strength": 4,
             "reasoning": "原料降价，利润改善",
             "lag": "immediate"},
            {"sector": "建筑材料", "direction": "bullish", "strength": 2,
             "reasoning": "成本端减压",
             "lag": "medium"},
        ],
    },
    # ==================== 橡胶 ====================
    {
        "commodity": "橡胶",
        "keywords": ["橡胶", "天然橡胶", "合成橡胶", "橡胶期货", "沪胶"],
        "chains_up": [
            {"sector": "橡胶", "direction": "bullish", "strength": 4,
             "reasoning": "橡胶是核心产品，涨价直接利好",
             "lag": "immediate"},
            {"sector": "汽车", "direction": "bearish", "strength": 3,
             "reasoning": "轮胎占汽车零部件成本，橡胶涨价抬高成本",
             "lag": "short"},
            {"sector": "化工", "direction": "bullish", "strength": 2,
             "reasoning": "合成橡胶跟涨，化工板块联动",
             "lag": "short"},
        ],
        "chains_down": [
            {"sector": "汽车", "direction": "bullish", "strength": 2,
             "reasoning": "轮胎成本下降",
             "lag": "short"},
            {"sector": "橡胶", "direction": "bearish", "strength": 3,
             "reasoning": "产品跌价",
             "lag": "immediate"},
        ],
    },
    # ==================== 纸浆 ====================
    {
        "commodity": "纸浆",
        "keywords": ["纸浆", "纸浆期货", "纸业", "造纸"],
        "chains_up": [
            {"sector": "造纸", "direction": "bullish", "strength": 3,
             "reasoning": "纸浆涨价推高纸品价格，库存升值",
             "lag": "immediate"},
            {"sector": "包装", "direction": "bearish", "strength": 2,
             "reasoning": "包装纸成本上升",
             "lag": "short"},
        ],
        "chains_down": [
            {"sector": "造纸", "direction": "bearish", "strength": 3,
             "reasoning": "原料跌价，产品端承压",
             "lag": "immediate"},
            {"sector": "包装", "direction": "bullish", "strength": 2,
             "reasoning": "成本下降",
             "lag": "short"},
        ],
    },
    # ==================== PTA/乙二醇（聚酯链）====================
    {
        "commodity": "PTA",
        "keywords": ["PTA", "精对苯二甲酸", "乙二醇", "聚酯", "涤纶", "短纤"],
        "chains_up": [
            {"sector": "化工", "direction": "bullish", "strength": 3,
             "reasoning": "PTA是化工中间品，涨价利好化工板块",
             "lag": "immediate"},
            {"sector": "石油化工", "direction": "bullish", "strength": 3,
             "reasoning": "PTA上下游联动，炼化利润增厚",
             "lag": "immediate"},
            {"sector": "纺织", "direction": "bearish", "strength": 3,
             "reasoning": "化纤原料成本上升，压缩纺织利润",
             "lag": "short"},
        ],
        "chains_down": [
            {"sector": "纺织", "direction": "bullish", "strength": 3,
             "reasoning": "化纤原料降价，利润改善",
             "lag": "short"},
            {"sector": "化工", "direction": "bearish", "strength": 2,
             "reasoning": "化工品跌价",
             "lag": "immediate"},
        ],
    },
    # ==================== 白糖 ====================
    {
        "commodity": "白糖",
        "keywords": ["白糖", "原糖", "白糖期货", "糖价", "食糖"],
        "chains_up": [
            {"sector": "食品加工", "direction": "bearish", "strength": 3,
             "reasoning": "白糖是食品加工重要原料",
             "lag": "short"},
            {"sector": "种植业", "direction": "bullish", "strength": 3,
             "reasoning": "糖料作物涨价利好种植",
             "lag": "short"},
        ],
        "chains_down": [
            {"sector": "食品加工", "direction": "bullish", "strength": 3,
             "reasoning": "原料成本下降",
             "lag": "short"},
        ],
    },
    # ==================== 玉米 ====================
    {
        "commodity": "玉米",
        "keywords": ["玉米", "玉米期货"],
        "chains_up": [
            {"sector": "种植业", "direction": "bullish", "strength": 3,
             "reasoning": "玉米涨价利好种植企业",
             "lag": "immediate"},
            {"sector": "饲料", "direction": "bearish", "strength": 4,
             "reasoning": "玉米是饲料核心能量原料",
             "lag": "immediate"},
            {"sector": "养殖业", "direction": "bearish", "strength": 3,
             "reasoning": "饲料成本上升压制养殖利润",
             "lag": "short"},
            {"sector": "食品加工", "direction": "bearish", "strength": 2,
             "reasoning": "深加工原料成本上升",
             "lag": "short"},
        ],
        "chains_down": [
            {"sector": "饲料", "direction": "bullish", "strength": 4,
             "reasoning": "原料成本下降",
             "lag": "immediate"},
            {"sector": "养殖业", "direction": "bullish", "strength": 3,
             "reasoning": "养殖成本下降",
             "lag": "short"},
            {"sector": "种植业", "direction": "bearish", "strength": 2,
             "reasoning": "农产品跌价",
             "lag": "immediate"},
        ],
    },
    # ==================== 铝 ====================
    {
        "commodity": "铝",
        "keywords": ["铝", "沪铝", "电解铝", "伦敦铝", "氧化铝", "铝期货"],
        "chains_up": [
            {"sector": "有色金属", "direction": "bullish", "strength": 4,
             "reasoning": "铝是有色核心品种，涨价利好板块",
             "lag": "immediate"},
            {"sector": "电解铝", "direction": "bullish", "strength": 5,
             "reasoning": "铝价是电解铝企业利润核心驱动",
             "lag": "immediate"},
            {"sector": "汽车", "direction": "bearish", "strength": 2,
             "reasoning": "汽车用铝成本上升",
             "lag": "medium"},
            {"sector": "电力", "direction": "neutral", "strength": 1,
             "reasoning": "电解铝是高耗能行业，电价联动",
             "lag": "medium"},
        ],
        "chains_down": [
            {"sector": "有色金属", "direction": "bearish", "strength": 3,
             "reasoning": "有色板块承压",
             "lag": "immediate"},
            {"sector": "电解铝", "direction": "bearish", "strength": 4,
             "reasoning": "铝价跌压缩利润",
             "lag": "immediate"},
            {"sector": "汽车", "direction": "bullish", "strength": 2,
             "reasoning": "用铝成本下降",
             "lag": "medium"},
        ],
    },
    # ==================== 镍 ====================
    {
        "commodity": "镍",
        "keywords": ["镍", "沪镍", "镍期货", "伦敦镍"],
        "chains_up": [
            {"sector": "有色金属", "direction": "bullish", "strength": 4,
             "reasoning": "镍是有色核心品种，涨价带动板块",
             "lag": "immediate"},
            {"sector": "钢铁", "direction": "bearish", "strength": 3,
             "reasoning": "镍是不锈钢核心原料，成本上升",
             "lag": "short"},
            {"sector": "电池", "direction": "bearish", "strength": 2,
             "reasoning": "三元电池用镍成本上升",
             "lag": "short"},
        ],
        "chains_down": [
            {"sector": "不锈钢", "direction": "bullish", "strength": 3,
             "reasoning": "原料成本下降",
             "lag": "short"},
            {"sector": "有色金属", "direction": "bearish", "strength": 3,
             "reasoning": "有色板块承压",
             "lag": "immediate"},
        ],
    },
]


def find_matching_chains(keywords):
    """
    给定资讯中的关键词列表，找出匹配的产业链。
    返回匹配的产业链条目列表，每条包含匹配的商品信息和传导路径。
    """
    results = []
    for chain in INDUSTRY_CHAINS:
        for kw in chain["keywords"]:
            for input_kw in keywords:
                if kw in input_kw or input_kw in kw:
                    # 判断是看涨还是看跌方向
                    # 先标记匹配，由调用方决定方向
                    results.append({
                        "commodity": chain["commodity"],
                        "matched_keyword": kw,
                        "chain_up": chain["chains_up"],
                        "chain_down": chain["chains_down"],
                    })
                    break
            else:
                continue
            break
    return results


def get_all_sector_names():
    """获取知识图谱中所有涉及的板块名称"""
    sectors = set()
    for chain in INDUSTRY_CHAINS:
        for item in chain["chains_up"]:
            sectors.add(item["sector"])
        for item in chain["chains_down"]:
            sectors.add(item["sector"])
    return sorted(sectors)
