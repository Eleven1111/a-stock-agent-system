# 腾讯 / 新浪直连 API 字段映射

收盘后稳定、无需代理（但 urllib 必须设 `NO_PROXY` 绕过 ClashX TUN）。

## 备用 API 端点

- 腾讯实时行情：`http://qt.gtimg.cn/q=sz000983`（GBK）
- 腾讯指数行情：`http://qt.gtimg.cn/q=sh000001,sz399001,sz399006`（GBK，字段位置与个股不同）
- 腾讯历史K线：`https://ifzq.gtimg.cn/appstock/app/fqkline/get?param=sz000983,day,,,22,qfq`（JSON，⚠️ 必须跟随 302）
- 新浪实时行情：`https://hq.sinajs.cn/list=sz000983`（GBK）
- 新浪历史K线：`https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=sz000983&scale=240&ma=5&datalen=120`（JSON）

## 腾讯个股字段（qt.gtimg.cn/q=szXXXXXX）

| 位置 | 字段 | 说明 |
|------|------|------|
| parts[0] | 市场代码 | 51=深圳, 1=上海, 0=未知 |
| parts[1] | 名称 | UTF-8中文 |
| parts[2] | **股票代码** | **6位数字代码，不是 parts[0]！** |
| parts[3] | 现价 | |
| parts[4] | 昨收 | |
| parts[31] | 涨跌额 | |
| parts[32] | 涨跌幅(%) | |
| parts[33] | 最高 | |
| parts[34] | 最低 | |
| parts[37] | 成交额(万) | *10000=元, /10000=亿元 |
| parts[38] | 换手率(%) | |
| parts[39] | **市盈率-动态** | 空字符串表示亏损或暂无 |
| parts[44] | **流通市值(亿)** | **已是亿单位，直接使用** |
| parts[45] | **总市值(亿)** | **已是亿单位，直接使用** |

**⚠️ 解析注意：**
1. 股票代码在 `parts[2]`，不是 `parts[0]`（后者是市场代码 51/1）。
2. 成交额 `parts[37]` 单位"万元" → 转元 `*10000`，转亿 `/10000`。
3. 市值 `parts[44]/[45]` 已是"亿"，直接 `float()`，不要除以 1e8。
4. PE `parts[39]` 为空字符串表示亏损或暂未披露。
5. urllib 必须 `os.environ["NO_PROXY"]=".gtimg.cn"` 绕过 TUN（否则走系统代理连不上）。

**Python 解析模板：**
```python
import os, urllib.request
os.environ["NO_PROXY"] = ".gtimg.cn"

url = "http://qt.gtimg.cn/q=sz300059"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req, timeout=10) as resp:
    raw = resp.read().decode("gbk")

for line in raw.strip().split(";"):
    if not line or "=" not in line:
        continue
    val = line.split("=")[1].strip().strip('"').split("~")
    code = val[2]                          # 股票代码在索引2！
    name = val[1]
    price = val[3]
    chg_pct = val[32]
    amount_yi = float(val[37]) / 10000     # 成交额(亿元)
    pe = val[39] if val[39] else "-"
    total_mcap = float(val[45]) if val[45] else 0  # 总市值(亿)
```

## 腾讯指数字段（qt.gtimg.cn/q=sh000001）

| 位置 | 字段 | 说明 |
|------|------|------|
| parts[31] | 涨跌额 | |
| parts[32] | 涨跌幅(%) | |
| parts[33] | 最高 | |
| parts[34] | 最低 | |
| parts[36] | "价/量/额"组合 | 如 `4057.74/676025806/1319801913951` |
| parts[37] | 成交额(万) | 需 *10000 转元 |

## AkShare 函数兼容速查（本网络环境）

**稳定可用（走 push2ex）：**

| 函数 | 用途 |
|------|------|
| `stock_zt_pool_em(date)` | 涨停板池 — 连板数、封板资金、炸板次数、所属行业 |
| `stock_zt_pool_strong_em(date)` | 强势股池 |
| `stock_zt_pool_dt_em(date)` | 跌停板池 |
| `stock_sse_summary()` | 上交所总览 |

**间歇性可用（走 push2/push2his，CDN 抽风，重试可恢复）：**

| 函数 | 用途 | 重试建议 |
|------|------|---------|
| `stock_zh_a_spot_em()` | 全A实时行情 | 分页多失败率高，建议用 `stock_zh_a_spot()` |
| `stock_board_industry_name_em()` | 行业板块 | 单次请求，重试1-2次即可 |
| `stock_individual_info_em()` | 个股信息 | 单次请求，重试1-2次即可 |
| `stock_zh_a_hist()` | 历史K线 | 单次请求，重试1-2次即可 |
| `stock_individual_fund_flow()` | 个股资金流向 | 已验证可用 |

AkShare 内置 `request_with_retry`（3次重试，指数退避 2s）。详见 `push2-connectivity.md`。
