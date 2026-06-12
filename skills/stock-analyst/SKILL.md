---
name: stock-analyst
description: "股票技术分析工具。基于多数据源（腾讯/新浪/BaoStock/SerpAPI）的本地缓存+技术指标分析系统，支持个股分析、板块批量扫描、涨停板监控、多因子评分、基本面分析、条件筛选、K线图、新闻驱动分析。纯numpy计算，无需talib。支持A股全量分析及港股基础行情+新闻分析。"
version: 3.1.1
author: Luna
metadata:
  hermes:
    tags: [A股, 技术分析, 选股, 板块扫描, MACD, RSI, KDJ, 基本面, 新闻, 条件筛选]
    category: finance
---

# Stock Analyst — 股票分析工具 (v3)

基于多数据源汇聚 + 本地SQLite缓存 + 纯numpy技术指标计算的分析系统。覆盖技术面、基本面、新闻情绪三个维度。

## 数据架构

```
                     ┌──────────────┐
                     │   SQLite缓存  │
                     │ daily_kline  │
                     │ realtime     │
                     └──────┬───────┘
                            ↑
  数据源链（有降级）：AkShare stock_zh_a_hist_tx（腾讯）→ 腾讯 ifzq → 新浪 → BaoStock（支持日/周/月）
  实时行情：腾讯 qt.gtimg.cn（主力）+ AkShare stock_zh_a_spot（新浪备选）
  资金流向：AkShare stock_individual_fund_flow（push2his，✅ 已验证可用，CDN 抽风时 retry）
  涨停板：AkShare stock_zt_pool_em（push2ex，通，✅已验证）
  板块列表：AkShare stock_board_industry_name_ths（同花顺，境外通，✅已验证）
  基本面：BaoStock（利润表、杜邦分析、ROE、营收同比）
- **SerpAPI**: Google News + Trends（3 key 轮询，见 `references/serpapi_key_rotation.md`）
- **港股行情**: 腾讯 `qt.gtimg.cn/q=hkXXXXX`，仅实时行情+新闻（见 `references/hk-stock-capabilities.md`）
```

## 触发场景

- "帮我分析一下600011"、"分析华能国际"
- "扫一下火电板块"、"高温主题现在什么位置"
- "今天涨停板有什么"
- "大盘现在怎么样"
- "通富微电有什么新闻"
- "查一下封测板块的搜索热度"
- "全市场RSI超卖的股票有哪些"

## 全市场扫描工作流

详见 `references/sector-scan-workflow.md`，五步法：
1. `sector_scan.py` 遍历所有行业板块（⚠️ 必须全量，不得只扫预设板块）
2. `hot-money-tactics --rotation` 板块轮动追踪
3. `analyst.py` 单股/板块批量深度分析
4. 三因子交叉判断（热度+技术面+催化）→ S/A/B/C 分级+买卖点
5. **Triage 升级 → Kanban 派发 → Serenity 深度投研**（见下方「多Agent Kanban编排系统」）

最后输出的报告必须包含具体买入区间、止损位、目标位、持有周期和仓位建议。
所有卖出/减仓建议必须先检查 A 股 T+1：当日买入或加仓股份不得建议当日卖出。
若被锁定，输出最早可卖交易日和隔夜跳空风险，不得把止损价描述成当日可执行成交。

## 洗盘/超卖捡筹分析

详见 `references/washout-detection.md`。

当用户问"哪些在洗盘"、"超卖捡筹"、"跌透了没有"、"疯狂洗盘"时，触发此工作流：

**四步排查法：**
1. **扫板块** — 遍历用户跟踪板块（封测/AI算力/军工航天/电网/家电/煤炭），批量拉行情，找10~20%跌幅区间的标的
2. **验信号** — 对候选逐个做技术分析，检查RSI(<30超卖)、KDJ(K<10极限)、布林位置、均线排列
3. **看钱包** — 读 portfolio.json 的现金，算1手成本是否买得起；如不够给出"卖A→腾钱→买B"路径
4. **出方案** — 区分"可以关注"和"可以买入"两个阶段，给出条件式入场点

**⚠️ 洗盘票的客观性铁律约束：** 洗盘票大多跌破布林下轨+空头排列，触犯铁律2/3/4。输出时必须诚实标注约束，不硬给买入评级。用条件句而不是断言句。

**洗盘 vs 真跌口诀：** 洗盘是"吓到你了"，真跌是"真的不行了"。行业逻辑没变+PE不贵+RSI<30→大概率洗盘。持续阴跌30%+RSI长期趴30以下→真跌。

## 使用

```bash
PY=~/.hermes/hermes-agent/venv/bin/python3
ANALYST=~/.hermes/skills/stock-analyst/analyst.py

# === 技术面 ===
$PY $ANALYST analyze 600011 华能国际        # 单股技术分析
$PY $ANALYST weekly 600900 长江电力          # 周线级别分析
$PY $ANALYST screen 封测                    # 预设板块批量扫描
$PY $ANALYST realtime 600011,600027         # 实时行情
$PY $ANALYST chart 600011 华能国际 30       # K线图
$PY $ANALYST zt                              # 今日涨停板
$PY $ANALYST index                           # 大盘指数

# === 基本面 ===
$PY $ANALYST fundamental 002156 通富微电    # 基本面（ROE/营收/净利）
$PY $ANALYST compare 封测                   # 板块横向对比（ROE+营收+技术评分）

# === 全市场 ===
$PY $ANALYST screener                       # 列出可用筛选条件
$PY $ANALYST screener "rsi<30 AND volume_ratio>1.2"  # 条件筛选引擎
$PY $ANALYST backtest 002156                # 回测评分系统

# === 新闻 ===
$PY $ANALYST news 002156 通富微电           # 个股新闻（含间接资金流向信号）
$PY $ANALYST news sector 电力               # 板块新闻
$PY $ANALYST news market                    # 大盘新闻
$PY $ANALYST news trend 封测                # 搜索热度趋势
```

## 预设板块（14个）

| 板块 | 核心标的 |
|------|---------|
| 火电 | 华能国际、华电国际、大唐发电、浙能电力、国投电力 |
| 水电 | 长江电力、华能水电、国投电力、川投能源、桂冠电力 |
| 电网 | 许继电气、国电南瑞、特变电工、三星医疗、国网英大 |
| 空调 | 格力电器、美的集团 |
| 高温主题 | 火电+水电+电网+空调合并 |
| 煤炭 | 山西焦煤、淮北矿业、平煤股份、山煤国际、晋控煤业、兖矿能源 |
| 封测 | 通富微电、长电科技、华天科技、深科技、太极实业 |
| 消费电子 | 立讯精密、工业富联、歌尔股份、蓝思科技、领益智造 |
| 半导体 | 中芯国际、韦尔股份、北方华创、圣邦股份、中微公司 |
| AI算力 | 中际旭创、工业富联、海光信息、寒武纪、中科曙光 |
| 军工航天 | 中国卫通、中国卫星、航天电子、中航重机、航天发展 |
| 新能源 | 阳光电源、隆基绿能、宁德时代、晶澳科技、天合光能 |
| 券商金融 | 中信证券、华泰证券、招商证券、东方财富、国泰君安 |
| 汽车 | 上汽集团、长安汽车、广汽集团、比亚迪、北汽蓝谷 |

## 技术指标

- **趋势**: MA5/10/20/60、多头/空头排列、金叉/死叉
- **动量**: MACD(DIF/DEA/柱)、RSI(6/14)、KDJ(K/D/J)
- **波动**: 布林带(20,2)、突破/跌破判断
- **量能**: 成交量比（相对5日均量）

## 评分体系

综合评分范围 -10 ~ +10：

| 评分 | 评级 | 条件 |
|------|------|------|
| +4以上 | 强烈买入 🟢🟢 | 多指标共振看多 |
| +2 ~ +3 | 买入 🟢 | 技术面偏多 |
| 0 ~ +1 | 观望 🌤 | 中性偏多 |
| -1 ~ -2 | 谨慎 🔶 | 技术面偏空或超买 |
| -2以下 | 卖出/回避 🔴 | 多指标警告，强制触发 |

## ⚠️ 客观性铁律（代码层强制执行）

1. **级别不可越级提拔** — 板块热度分14+→S, 10-14→A, 6-10→B, <6→观察
2. **趋势空头锁评分上限** — MA5<MA10<MA20<MA60时，评分上限锁定为-0.5，不可能给买入
3. **价格<MA20时超卖不视为买入** — 跌破布林下轨+价格在MA20以下时，信号改为"加速下跌中，等待企稳"
4. **评分<+2不给买入评级** — 只有评分≥+2才能出现🟢
5. **评分≤-2强制卖出** — 自动触发🔴
6. **用户历史关注标的必须标注** — 防止对话历史污染分析结果

**曾犯错误（已修复）：**
- ❌ 中国卫通趋势空头+评分+1.5 不应给布局建议
- ❌ 中科曙光跌破布林下轨+价格<MA20 不应给买入
- ❌ 通富微电33亿封单涨停时，不应绕过它去推荐康强电子（替身标）。用户核心跟踪标的有异常巨量信号时必须优先提示，资金不够也应明确说明而非推荐替代品。
- ❌ **国联股份：赛道偏好覆盖了客观性铁律** — 用户问"互联网/电商"板块，国联属于该板块就推荐了它，跳过了客观性铁律。实际上国联空头排列锁分-0.5，评分不够+2，且净利润同比下降9.12%，低PE是价值陷阱。**用户问的赛道只是筛选条件，不是买入理由。**

### 3. 用户赛道偏好不降低评分门槛
用户问"XX板块有哪些好标的"时，先筛出该板块所有候选标的，然后对每个标的走标准评分流程。客观性铁律（铁律2/3/4）优先级高于用户赛道偏好。不能说"用户想买这个板块的所以放松评分"。

### 4. PE便宜必须交叉验证利润趋势
PE<15时自动检查净利润同比趋势。净利润下降时的低PE标记为"⚠️价值陷阱风险"，不作为买入理由单独使用。确认步骤：运行 `analyst.py fundamental <code> <name>` 查看利润同比。

### 5. 分形图必须用缠论框架输出 Markdown
用户要求画分形图/走势图时，必须使用 `chanlun-backtest` 技能的缠论框架（分型、笔、线段、中枢），输出 Markdown 格式。禁止用普通 ASCII K线图替代缠论分形图。

### 6. 所有权归属明确
当用户同时提到"自己买入"和"朋友套牢"等多方持仓时，分析输出必须明确区分「你的」和「你朋友的」，不可混淆。建议分别标注，且朋友持仓只能从对话上下文推断，不写入 portfolio.json。

### 7. 推荐前必须标注阶段和风险
任何买入/卖出/加仓推荐前，必须标注：
- 标的当前阶段（连板第N天/分歧日/高位横盘/突破启动/回调中）
- 核心风险（距均线乖离率、换手率异常、连板后分歧、非基本面炒作）
- 追高场景下明确说"现在追是XX风险"再给操作建议，不能只说"可以"

判断方式：
- 用户说"我朋友"、"有人"、"他有" → 对方的票
- 用户说"我的"、"我买了"、"帮我记录" → 用户的票
- 默认假设是用户自己的，除非有明确证据指向他人

曾犯错误：
- ❌ 用户朋友套牢了华电能源，我分析完后把朋友的持仓和用户自己的能科科技混在一起回答，用户暴怒。
PE<15时自动检查净利润同比趋势。净利润下降时的低PE标记为"⚠️价值陷阱风险"，不作为买入理由单独使用。确认步骤：运行 `analyst.py fundamental <code> <name>` 查看利润同比。

## 🚨 分析纪律（agent 必须遵守）

### 1. 跟踪标的优先原则
用户有任何核心跟踪标的出现异常信号时（巨量封单涨停、板块龙头暴动等），**必须优先给该标的的上车分析**。不能因为用户资金暂时不够就跳过，去推荐次要标的。资金不足的解决方案是给出具体操作建议（如清其他仓位），而不是找便宜替代品。

### 2. 禁止猜测数据
任何关于"今天某股票怎么样了"的问题，**必须实时查询腾讯 API 给出精确数据**。禁止基于之前的分析推断（"大概率是…"、"应该…"）。查了再说话，不查别开口。

### 3. 时间必须动态获取
报告中出现的时间必须用 datetime.now() 实时获取，禁止硬编码。用户会纠正"你的时间不对"。显示格式：datetime.now().strftime("%H:%M")。

### 4. ⚠️ 子代理新闻搜索防伪造（2026-06-09 新增）
将新闻搜索委托给子代理（delegate_task）时，必须明确要求子代理**不能模拟/虚构搜索结果**。子代理在无法连接到真实API时，可能生成"看起来很合理"但实际是虚构的新闻内容（包括但不限于：虚构股价涨跌、捏造政策消息、编造公司公告）。

**安全做法：**
- 委托搜索时，prompt 中明确写："如果API调用失败或返回空结果，请如实报告失败，**不要**模拟或虚构搜索结果"
- 对子代理返回的新闻结果，始终保持怀疑态度。如果结果是"模拟"或"总结"而非真实API查询，不应作为分析依据
- 最可靠的做法：主agent自己调用 analyst.py news 命令（terminal），不走子代理委托
- 如果确实需要委托且新闻对结论至关重要，要求子代理返回每个结果的URL链接以便验证

**识别伪造信号的检查表：**
| 迹象 | 疑似伪造 | 说明 |
|------|---------|------|
| 子代理说"我模拟了四个搜索" | 🚨 确定伪造 | 直接丢弃结果 |
| 股价/行情信息与实时数据矛盾 | 🚨 高度怀疑 | 如报告说"华能国际涨4%"但实时数据是-0.95% |
| 详细的价格/时间/百分比数字 | ⚠️ 需交叉验证 | 子代理可能编造具体数字 |
| 看起来太完美的催化叙事 | ⚠️ 需交叉验证 | "利好大爆发"类内容需怀疑 |
| 只有摘要没有来源链接 | ⚠️ 可信度低 | 无法溯源验证 |

## 🔍 搜索热度分析 — 多关键词交叉验证

使用 `analyst.py news trend <关键词>` 获取搜索热度趋势（SerpAPI Google Trends），但**单一关键词的热度绝对数值可被孤立解读**。必须交叉验证：

### 核心原则：搜索热度必须与其他关键词对比

**❌ 错误示例**：电力保供搜索热度 100/100 → "主题热度冲顶，情绪过热"
**✅ 正确分析**：电力保供 100 + 高温 38 + 用电负荷 37 → 搜索热度被政策叙事推高，非天气驱动

### 实践模式

```
电力保供 100  +  拉闸限电 0（上周100脉冲） +  高温 38  +  用电负荷 37
   │                         │                    │              │
   ↓                         ↓                    ↓              ↓
政策驱动                   事件脉冲已过          正常水平        正常水平
```

### 交叉验证矩阵

| 关键词 | 热度趋势 | 解读 |
|--------|---------|------|
| 电力保供 ↑ | 100/100 连续冲高 | 主题情绪峰值，政策/资金驱动 |
| 拉闸限电 | 脉冲100→0 | 短期新闻事件，不具持续性 |
| 高温 | 35-43 温和波动 | 夏季正常波动，未出现极端天气 |
| 用电负荷 | 25-45 中位波动 | 未突破季节性峰值 |

**时机判断**：当「核心叙事关键词」（如电力保供）热度远高于「实质催化关键词」（如高温、用电负荷），且存在「脉冲型关键词」（如拉闸限电）短暂冲高后回落，通常意味着：
1. 行情由**政策预期 + 资金驱动**，非基本面/天气驱动
2. 核心叙事已进入**情绪高潮期**，追高风险增大
3. 脉冲型事件消失后，**后续催化不足**概率高

## Cron 执行注意事项

详见 `references/cron-pitfalls.md`（含 execute_code 网络能力勘误）和 `references/execute_code_data_patterns.md`（含可复用的代码模板）：

1. **`execute_code` 沙箱有 HTTP 网络但 HTTPS 不可用** — gtimg.cn（HTTP）全天候稳定，push2（HTTP）间歇性可用需重试。详见 `references/cron-pitfalls.md` 的勘误章节。直接运行 `analyst.py` 命令（自动处理代理）也是最可靠的路径之一。
2. **DeepSeek 冷启动** — 首次API调用可能耗时200-300s，设置timeout=300+
3. **Prompt 必须自包含** — cron无对话上下文，所有信息必须在prompt内
4. **定期跟踪型 job 用 `repeat: forever`，事件型 job 用 `repeat: once`**
5. **时间分散错开** — 避免整点，避免同分钟多job并发。新建job前检查现有时间表
6. **已验证可用的 cron 命令**：`analyst.py index`、`analyze`、`realtime`、`screen`、`news`（含 sector/market/trend）
7. **已验证的**：`execute_code` 内 gtimg.cn HTTP 调用全天候可用。push2 HTTP 调用约30%初次失败（CDN间歇性），加 retry 后可恢复。HTTPS 类（SerpAPI、Sina 等）不可用。

## SerpAPI 多 Key 轮询

详见 `references/serpapi_key_rotation.md`。

**为什么需要多 key：** SerpAPI 按 key 独立计费，免费额度有限。高温主题跟踪、市场新闻轮询等多个 cron job 同时调用时，单 key 可能快速耗尽。

**当前配置：** 3 个 key 循环有序轮询，每次 `_serpapi_request()` 自动取下一个。

## 数据源限制

- **东财 push2/push2his** — **CDN 间歇性 Empty reply**（约 30% 请求失败，重试 1-2 次恢复），非永久丢失。详见 `references/push2-connectivity-status.md`。AkShare 内置 `request_with_retry`（3 次指数退避），大多数单次请求函数可稳定使用。`stock_zh_a_spot_em()`（全A行情）因分页多失败率较高，建议用 `stock_zh_a_spot()` 新浪版替代。仅 `push2ex.eastmoney.com`（涨停板池）CDN 稳定。
- **资金流向** — `akshare stock_individual_fund_flow()` 可通过 `stock_individual_fund_flow(stock="600519", market="sh")` 调用，已验证可用。TUN 关闭后 push2his 可连通，CDN 抽风时加 retry 即可。
- **BaoStock 基本面** — 无需注册/API key
- **腾讯 ifzq K线 / qt.gtimg.cn 实时行情** — 全天候可用 ✓
- **SerpAPI 新闻** — 3 key 轮询，按量使用
- **资金流向** — `akshare stock_individual_fund_flow(stock="600519", market="sh")` 已验证可用（TUN 关闭后 push2his 通，CDN 抽风时 retry 即可）。备用降级路径：`analyst.py news <代码> <名称>` 从新闻摘要中提取资金信号（如"主力净买入3.43亿"），见 `references/fund_flow_from_news.md`。

## 多Agent Kanban编排系统

stock-analyst 是 A股全栈分析 Agent 系统的三个技术 worker 之一。整个系统通过 Kanban 板编排：

```
Cron 流水线 → Triage 编排器 → Kanban 板 → Worker Profiles → 报告
                    │
         ┌──────────┼──────────┐
         ↓          ↓          ↓
  stock-analyst  hotmoney  serenity-worker
  (技术面)      (情绪/催化)  (深度投研)
         └──────────┼──────────┘
                    ↓
              四维评分 → S/A/B/C
                    ↑
         global-market-monitor
         (外围输入: VIX/美股/期货/汇率)
```

### Worker Profiles（Kanban profile — 用 `hermes -p <name>` 调用）

| Profile | 模型 | 角色 | 绑定的 Skill |
|---------|------|------|-------------|
| `stock-analyst` | deepseek-v4-flash (OpenRouter) | 技术分析 | stock-analyst, a-stock-data |
| `hotmoney-worker` | deepseek-v4-flash (OpenRouter) | 游资情绪 | hot-money-tactics, news-to-sector |
| `serenity-worker` | deepseek/deepseek-v4-pro (OpenRouter) | 深度投研 | serenity-investment-research, stock-analyst |
| `stock-triage` | deepseek-v4-flash (OpenRouter) | 编排中枢 | 全部 + stock-triage skill |

### 快捷指令（Discord 中直接输入）

| 指令 | 作用 | 触发路径 |
|------|------|---------|
| `/deep 002156` | 深度分析 | Triage → 4-worker全链 → Serenity |
| `/scan 军工` | 板块扫描 | stock-analyst 快速扫描 |
| `/alert 600011 止损8.0` | 价格提醒 | 写入提醒数据库，每5分自动检查 |
| `/report 封测 本周` | 汇总报告 | stock-analyst + hotmoney |
| `/compare A B C` | 横向对比 | stock-analyst compare 模式 |
| `/global` | 全球扫描 | global-market-monitor → news-to-sector |

### 信号升级规则

当 Cron 流水线中的分析结果被 Triage 编排器拿到后，自动评分：

| 评分 | 等级 | 行动 |
|------|------|------|
| ≥ 8 | S | 4-worker全链 + Serenity深度投研 + 推送 |
| 6-7 | A | 3-worker（技术+情绪+催化），不加 Serenity |
| 4-5 | B | 单 worker 快扫 + 观察池 |
| < 4 | — | 记录信号，不执行 |

S级信号触发 Serenity 的完整供应链分析、财务拆解、估值赔率、熊市审计。

### 全局配置

- **所有 A 股 Worker profile 默认 provider**: OpenRouter（`OPENROUTER_API_KEY`）
- **轻量级 worker**（stock-analyst, hotmoney-worker, stock-triage）: `deepseek-v4-flash`（便宜）
- **深度 worker**（serenity-worker）: `deepseek/deepseek-v4-pro`（贵，计费分开）
- **官方 DeepSeek API**（非 OpenRouter）: 通过修改 `config.yaml` 的 `model.provider` 为 `deepseek` 切换

## Skill Hub 生态现状

当用户问"有没有别的 skill 可以实现"，首选检查 Skill Hub 是否真有替代方案。以下是已知事实（2026-06-02 实测）：

- **Hub 里几乎所有的 A 股技能依赖 AkShare/eastmoney** — `a-stock-review`、`a-stock-data`、`a-stock-screener`、`stock-alpha` 等数十个 clawhub 技能，底层全走 push2.eastmoney.com。这些技能现在**CDN 间歇性可用**（Empty reply 约30%，重试1-2次恢复），非永久封禁。`stock_board_industry_name_em()` 等单次请求类可稳定使用，`stock_zh_a_spot_em()`（全A分页）失败率较高。
- **GitHub repo tap 不可用** — `hermes skills tap add github:xxx` 可以注册 tap，但 GitHub 被 Clash 代理延迟/超时（connect timeout），repo 拉不下来。不去尝试安装 GitHub 来源的 A 股技能。
- **skills.sh 来源的 400+ 个 skills** — 在 skill hub 搜索时能看到它们（缓存在本地 index），但 `hermes skills inspect/install` 都需要从 skills.sh/GitHub 下载内容，Clash 下绝大多数失败。
- **唯一本地可用的 clawhub 技能** — `a-stock-data`（已安装）、`a-stock-review`（可安装）、`money-flow`（方法论知识，无数据抓取能力）、`magpie`（需本地 daemon）。
- **结论** — Clash 环境下，安装额外 A 股技能不能解决任何 stock-analyst 已有能力之外的问题。**stock-analyst + hot-money-tactics + news-to-sector + serenity-investment-research + global-market-monitor + stock-triage 编排器** 构成完整的 A 股全栈分析系统，无需安装额外 Hub 技能。

## 文件结构

```
stock-analyst/
├── SKILL.md
├── analyst.py                  # 主入口 + 预设板块组合
├── scripts/
│   ├── data_cache.py           # 数据缓存层 + 多源抓取
│   ├── tech_analysis.py        # 技术指标计算 + 分析评分（含客观性硬约束）
│   ├── screener.py             # 条件筛选引擎（全市场搜索）
│   ├── fundamentals.py         # 基本面数据（BaoStock ROE/营收/杜邦）
│   ├── chart.py                # 终端ASCII K线图
│   ├── news.py                 # SerpAPI新闻搜索 + 资金信号自动提取
│   └── sector_scan.py          # 全市场板块热度扫描（涨停聚合）
├── references/
│   ├── clash_proxy_bypass.md   # （过时）原Clash DNS劫持方案，push2实为CDN geo-block
│   ├── cron-pitfalls.md        # Cron作业常见陷阱和解决方案（含execute_code网络能力勘误）
│   ├── data_sources.md         # 数据源技术细节和API字段映射
│   ├── execute_code_data_patterns.md  # execute_code数据采集模式（含gtimg/push2/AkShare代码模板）
│   ├── fund_flow_from_news.md  # 资金流向替代方案：SerpAPI新闻提取
│   ├── push2-connectivity-status.md  # Push2 CDN 状态 + 诊断 Quick Reference
│   └── sector-scan-workflow.md # 全市场扫描四步法
```

## 相关技能

- `hot-money-tactics` — 游资战法、板块轮动追踪（--rotation）、涨停板情绪周期
- `news-to-sector` — 资讯驱动板块分析（产业链传导）
- `serenity-investment-research` — Serenity 风格深度投研（供应链瓶颈、财务拆解、估值赔率）— 仅 S/A 级信号触发
- `global-market-monitor` — 全球市场监控（VIX/美股/期货/汇率/中概ADR → A股板块影响评估）
- `stock-triage` — A股编排中枢（信号检测、Kanban 派发、评分升级）
- `a-stock-commands` — 快捷指令（/deep /scan /alert /report /compare /global）
