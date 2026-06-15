# 数据源技术细节

## Clash 代理（NO_PROXY）配置

Clash Verge（mihomo 内核）DNS 劫持导致东财 push2/push2his 端点在 Python urllib 下 502。解法：在 `.env` 设 `NO_PROXY` 绕过代理。

```env
NO_PROXY=.eastmoney.com,.gtimg.cn,.sinajs.cn,.10jqka.com.cn,.hexun.com
```

详见 `references/clash_proxy_bypass.md`。

## 数据源降级链

```bash
K线: 腾讯 ifzq → 新浪 → BaoStock（日/周/月）
实时行情: 腾讯 qt.gtimg.cn
资金流向: push2his.eastmoney.com（需 NO_PROXY，akshare stock_individual_fund_flow）
涨停板: AkShare stock_zt_pool_em
基本面: BaoStock（ROE/营收/杜邦）
新闻: SerpAPI Google News（多 key 轮询）
```

## 腾讯 ifzq 历史K线

**端点**: `https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{days},qfq`

**代码格式**: sh600519 或 sz000001

**注意**: 必须跟随 302 重定向（curl 加 `-sL`）。Python urllib 默认不跟随，需用 curl fallback。

**返回格式**:
```json
{
  "data": {
    "sh600519": {
      "qfqday": [
        ["2026-05-26", 1285.35, 1273.38, 1289.89, 1270.01, 45932, 5880000],
        // date, open, close, high, low, volume(百股), amount(元)
      ]
    }
  }
}
```

**字段顺序**: [date, open, close, high, low, volume(百股), amount(元)]

## 新浪历史K线

**端点**: `https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=sh600519&scale=240&ma=5&datalen=120`

**代码格式**: sh600519 或 sz000001

**返回格式** (JSON数组):
```json
[
  {
    "day": "2026-05-26",
    "open": "1285.350",
    "high": "1289.890",
    "low": "1270.010",
    "close": "1273.380",
    "volume": "4593162",   // 股数，需 /100 转手
    "ma_price5": 1295.092
  }
]
```

**注意**: volume 是股数（不是手），需除以100转成手。

## 腾讯实时行情 (qt.gtimg.cn)

**端点**: `http://qt.gtimg.cn/q=sh600519,sz000001,sh600000`

**编码**: GBK，需 `iconv -f GBK -t UTF-8` 或 Python decode

**个股字段映射** (parts 数组，按位置索引):

| 位置 | 字段 | 说明 |
|------|------|------|
| 1 | 名称 | |
| 3 | 现价 | |
| 4 | 昨收 | |
| 5 | 今开 | |
| 6 | 成交量(手) | |
| 7 | 成交额(元) | 大额数值有逗号 |
| 8 | 最高价 | 今日动态 |
| 9 | 最低价 | 今日动态 |
| 31 | 涨跌额 | |
| 32 | 涨跌幅(%) | |
| 33 | 最高 | |
| 34 | 最低 | |
| 37 | 成交额(万) | 需 * 10000 转元 |
| 38 | 换手率(%) | |
| 39 | 市盈率(动态) | |
| 45 | 总市值 | |

**指数格式** (如 sh000001):

| 位置 | 字段 |
|------|------|
| 31 | 涨跌额 |
| 32 | 涨跌幅(%) |
| 33 | 最高 |
| 34 | 最低 |
| 36 | "价/量/额"组合字符串 (如 `4057.74/676025806/1319801913951`) |
| 37 | 成交额(万) |

## BaoStock

**安装**: `pip install baostock`

**无需注册/API key**。使用前需 login/logout。

```python
import baostock as bs
lg = bs.login()
rs = bs.query_history_k_data_plus(
    "sh.600519",
    "date,open,high,low,close,volume,amount",
    start_date="20260101", end_date="20260602",
    frequency="d", adjustflag="2"  # 2=前复权
)
while rs.next():
    print(rs.get_row_data())
bs.logout()
```

**代码格式**: sh.600519 或 sz.000001

## AkShare & Clash 代理（已修复）

之前 push2/push2his 端点在 Clash 代理下被拦截，现在是**已修复状态**。修复方法：

1. 确保 `.env` 有 `NO_PROXY=.eastmoney.com,.gtimg.cn,...`
2. `scripts/news.py` 在 import 时自动加载这个环境变量

### 验证资金流向（现在可用 ✅）

```python
# push2his 资金流向 - 直接 HTTP
import os, urllib.request, json
os.environ['NO_PROXY'] = '.eastmoney.com'
url = "http://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?lmt=0&klt=101&secid=0.000001&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
data = json.loads(urllib.request.urlopen(req, timeout=10).read())
# 返回 120 天资金流向数据（主力净流入/超大单/大单/中单/小单）

# akshare 封装版本（内部也走 NO_PROXY）
import akshare as ak
df = ak.stock_individual_fund_flow(stock="000001", market="sz")
# 列：日期, 收盘价, 涨跌幅, 主力净流入-净额, 主力净流入-净占比, 超大单/大单/中单/小单
```

### 对比：what was blocked，what wasn't

| 是否被拦截 | 端点 | 原因 |
|-----------|------|------|
| ❌ 原 blocked | push2.eastmoney.com | Clash DNS 劫持 198.18.x.x |
| ❌ 原 blocked | push2his.eastmoney.com | 同上 |
| ✅ 始终可用 | qt.gtimg.cn（腾讯） | 直连不走代理 |
| ✅ 始终可用 | money.finance.sina.com.cn（新浪） | 直连不走代理 |
| ✅ 始终可用 | ifzq.gtimg.cn（腾讯K线） | 直连不走代理 |

## SQLite 缓存

**位置**: `~/.hermes/data/stock_cache.db`

**表结构**:
- `daily_kline(code, date, open, high, low, close, volume, amount, source, cached_at)`
- `realtime_quotes(code, name, price, pct_change, volume, amount, turnover_rate, pe, ...)`

**TTL**: 盘中 1 小时刷新。非交易日缓存可无限使用。

**清理**:
```bash
python3 ~/.hermes/skills/stock-analyst/scripts/data_cache.py clear        # 全清
python3 ~/.hermes/skills/stock-analyst/scripts/data_cache.py clear 600519  # 单股清
```
