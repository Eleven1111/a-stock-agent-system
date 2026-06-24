# AkShare 数据查询配方

按需加载。SKILL.md 只保留数据源决策表与关键坑，完整 `ak.*` 调用范例集中在此。

## 实时行情数据

### 沪深京 A 股全部
```python
import akshare as ak

# 方式1：新浪财经实时行情（推，push2不通，走新浪API，境外通）
stock_zh_a_spot_df = ak.stock_zh_a_spot()

# 方式2：腾讯实时行情（直接 HTTP，不依赖 AkShare）见 tencent-sina-api.md
```

### 分市场实时行情
```python
ak.stock_sh_a_spot_em()   # 沪 A
ak.stock_sz_a_spot_em()   # 深 A
ak.stock_bj_a_spot_em()   # 北交所
ak.stock_new_a_spot_em()  # 新股
ak.stock_cy_a_spot_em()   # 创业板
ak.stock_kc_a_spot_em()   # 科创板
```

## 历史 K线

```python
# 日 K线 — 腾讯版（推，push2不通时的替代）
ak.stock_zh_a_hist_tx(symbol="sh000001", start_date="20240101", end_date="20240331")

# 日 K线 — 新浪财经接口（备用）
ak.stock_zh_a_hist(symbol="sz000001", start_date="19910403", end_date="20210327")

# 周 / 月 K线
ak.stock_zh_a_hist(symbol="000001", period="weekly")
ak.stock_zh_a_hist(symbol="000001", period="monthly")
```

## 股票信息

```python
ak.stock_info_a_code_name()                              # 所有股票代码和名称
ak.stock_individual_info_em(symbol="茅台")                # 个股详细信息（东方财富）
ak.stock_individual_basic_info_xq(symbol="SH600519")     # 雪球基本信息
ak.stock_individual_spot_xq(symbol="SH600519")           # 雪球历史K线
```

## 财务数据

```python
ak.stock_financial_analysis_indicator(stock="600519", symbol="财务指标")
ak.stock_balance_sheet_by_yearly_em(symbol="600519")          # 资产负债表
ak.stock_profit_sheet_by_reportly_em(symbol="600519")         # 利润表
ak.stock_cash_flow_sheet_by_reportly_em(symbol="600519")      # 现金流量表
```

## 市场数据

```python
ak.index_zh_a_hist(symbol="sh000001", period="daily")   # 上证指数历史
ak.index_sz_a_hist(symbol="sz399001", period="daily")   # 深证指数历史
```

## 资金流向与龙虎榜

```python
ak.stock_individual_em_xq(symbol="SH600519")   # 龙虎榜-营业部
# 龙虎榜统计需东财账号：stock_user_individual_info_em() / stock_user_statistics_em()
```

## 板块数据

```python
ak.stock_pool_em()   # 强势股池 / 涨停股池
```

## 股票市场总貌

```python
ak.stock_sse_summary()                       # 上交所
ak.stock_szse_summary(date="20250619")       # 深交所证券类别统计
ak.stock_szse_area_summary(date="20250619")  # 深交所地区交易排序
```

## BaoStock — 免费历史K线补充

安装：`~/.hermes/hermes-agent/venv/bin/python3 -m pip install baostock`

```bash
python3 -c "
import baostock as bs
lg = bs.login()
rs = bs.query_history_k_data_plus('sh.600519',
    'date,open,high,low,close,volume,amount',
    start_date='20260501', end_date='20260602',
    frequency='d', adjustflag='2')
while rs.next():
    print(rs.get_row_data())
bs.logout()
"
```

可用函数：`query_history_k_data_plus()`、`query_history_factor_data()`、`query_stock_basic()`、`query_stock_industry()`

## 返回格式

### 实时行情字段
代码、名称、最新价、涨跌幅、涨跌额、成交量(手)、成交额、昨收、今开、最高、最低、振幅、换手率、市盈率-动态、总市值、流通市值。

### K线字段
日期、开盘、收盘、最高、最低、成交量、成交额、振幅、涨跌幅、涨跌额、换手率。

## 复权说明

| 类型 | 说明 | 适用场景 |
|------|------|----------|
| 不复权（""） | 原始价格 | 查看历史走势 |
| 前复权（qfq） | 历史价格调整，当前价格不变 | 看盘、技术分析 |
| 后复权（hfq） | 当前价格不变，历史价格调整 | 收益率计算 |

## 股票代码规则

| 市场 | 代码格式 | 示例 |
|------|----------|------|
| 上交所 | 6xxxxx | 600000, 601318 |
| 深交所 | 0xxxxx | 000001, 300059 |
| 北交所 | 8xxxxx | 8xxxxx |

代码补齐前导 0（如 1 → 000001）。

## 使用示例

- 查个股实时行情：`stock_zh_a_spot()` 查 600519
- 查历史 K线：`stock_zh_a_hist_tx()` 指定日期范围
- 查涨停板：`stock_zt_pool_em()` 获取涨停板池
- 查行业板块列表：`stock_board_industry_name_ths()`（同花顺）
- 查财务数据：`stock_financial_analysis_indicator()`

## 错误处理

| 错误类型 | 可能原因 | 解决方法 |
|------|----------|----------|
| KeyError | 股票代码不存在或输入错误 | 检查代码并重试 |
| TimeoutError | 网络超时 | 重试或检查连接 |
| EmptyDataError | 当天无数据（非交易日） | 确认是否交易日 |

## 注意事项

1. **频率限制**：避免频繁请求，建议缓存结果。
2. **数据延迟**：实时数据可能有 1-5 分钟延迟。
3. **复权处理**：查询历史数据时注意复权方式选择。
4. **代码规范**：6 位数字代码，补齐前导 0。

## 支持的数据范围

A股/B股/港股/美股实时行情、创业板/科创板/新股、历史K线（日/周/月）、财务三表、技术指标（MA/MACD/RSI/KDJ/BOLL）、龙虎榜、资金流向、板块/概念、指数、市场总貌统计。
