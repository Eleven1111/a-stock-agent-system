# 深度板块分析流程

当用户要求对热门板块做"深入分析"时，基础脚本的输出不够——需要叠加个股级数据才能判断哪只标的值得参与。

## 重要：涨停板 vs 非涨停板 两条路径

**本文件处理的是涨停板池内的板块深度分析。** 如果目标板块在涨停板池中涨停数很少（0-2家）但板块整体走强，应该改用 `references/non-zt-sector-scanning.md` 的流程——用成分股批量扫描替代涨停板数据分析。

判断标准：`len(df[df['所属行业'] == sector])` < 3 但板块有走强迹象 → 切到非涨停板流程。

## 完整工作流

### Step 1：基础扫描

```bash
# 今日板块分布 + 连板梯队 + 封板质量
~/.hermes/hermes-agent/venv/bin/python3 ~/.hermes/skills/hot-money-tactics/scripts/analyze.py

# 板块轮动追踪（识别持续热点/新方向/退潮）
~/.hermes/hermes-agent/venv/bin/python3 ~/.hermes/skills/hot-money-tactics/scripts/analyze.py --rotation
```

### Step 2：提取板块内个股级数据

基础脚本只显示板块汇总。要从 `ak.stock_zt_pool_em()` 数据集提取每只涨停股的详细参数（代码/现价/连板/封单/流通市值/封板时间/炸板次数），用 execute_code 直接查询：

```python
import akshare as ak
df = ak.stock_zt_pool_em(date="20260608")

# 按所属行业筛选
sector = "通用设备"
stocks = df[df['所属行业'] == sector]
for _, r in stocks.iterrows():
    feng_yi = r['封板资金'] / 1e8
    lt_yi = r['流通市值'] / 1e8
    ratio = feng_yi / lt_yi * 100 if lt_yi > 0 else 0
    print(f"{r['代码']} | {r['名称']} | 现价:{r['最新价']} | 连板:{int(r['连板数'])} | 封单:{feng_yi:.2f}亿 | 流通:{lt_yi:.0f}亿 | 封单比:{ratio:.1f}% | 封板时间:{r['首次封板时间']} | 炸板:{int(r['炸板次数'])}")
```

### Step 3：计算封单质量比

| 封单/流通市值 | 评级 |
|:------------|:----|
| > 3% | ✅ 强封 |
| 1% ~ 3% | 中等 |
| < 1% | ⚠️ 弱封 |

### Step 4：获取实时行情补全数据

用腾讯 API 批量查询，获取 PE、总市值、流通市值：

```python
import subprocess, json
codes = "sz001696,sz002046,..."
r = subprocess.run(['curl', '-sL', 'http://qt.gtimg.cn/q=' + codes],
                   capture_output=True, timeout=10)
text = r.stdout.decode('gbk', errors='ignore')
# parts[3]=现价 parts[32]=涨跌幅% parts[37]=成交额万 parts[39]=PE parts[44]=流通市值亿 parts[45]=总市值亿
```

### Step 5：历史K线 → 均线形态判断

用腾讯 ifzq API（`curl -sL` 必须）：

```python
import subprocess, json
r = subprocess.run([
    'curl', '-sL', '--connect-timeout', '10',
    f'https://ifzq.gtimg.cn/appstock/app/fqkline/get?param=sz001696,day,,,60,qfq'
], capture_output=True, text=True, timeout=15)
data = json.loads(r.stdout)
# 导航到数据: data["data"]["sz001696"]["qfqday"]
# 或 data["data"][first_key]["day"] 如果 qfqday 不存在
day_data = data["data"][list(data["data"].keys())[0]].get("qfqday") or \
           data["data"][list(data["data"].keys())[0]].get("day")
```

每条 K 线格式：`["2026-06-08", open, close, high, low, volume]`

计算均线判断趋势：
```
MA5 > MA10 > MA20 → 📈 多头排列 ✅
MA5 < MA10 < MA20 → 📉 空头排列
介于之间 → ➡️ 震荡
```

同时计算 **现价距MA5的偏离度**：
- `距MA5 = (现价 - MA5) / MA5 × 100%`
- > +8% → 短线追高风险大
- +3% ~ +8% → 正常涨停突破
- < +3% → 紧贴均线，蓄势型

### Step 6：资金适配检查

用户可用资金从 `portfolio_manager.py --check` 读取。

对每个涨停股计算 **1手成本 = 现价 × 100**，筛掉超预算的标的。

根据用户的偏好（中线上涨3~10%，非打板追涨停），重点推荐：
- PE合理的标的（非亏损、非泡沫估值）
- 均线多头或刚突破MA20的标的
- 封板质量稳定（无/少炸板）

### Step 7：1进2 竞价计划

参考 `references/day-2-chase.md` 的 1进2 追板流程。

## 输出格式

最终报告至少包含：

```
### 板块名
| 标的 | 现价 | 1手价 | PE | 封单 | 封单比 | 均线 | 评价 |
```

并给出明确的优先级排序和明日操作计划。

## 已知陷阱

- AkShare 的 `stock_zh_a_hist()` 在 TUN 模式下不可用（走 push2），用上方的腾讯 ifzq API 替代
- 北交所股票（920xxx）的K线数据可能不全，仅今日数据
- PE值为空/0/负的标的可能是亏损股，尽量排除
- 炸板3次以上的标的放弃——分歧太大，第二天低开概率高
- 成交额突然放大（>10亿）且封单比<2%的标的，警惕量化参与度高
