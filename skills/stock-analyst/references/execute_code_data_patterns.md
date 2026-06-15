# execute_code 沙箱数据采集模式

## 沙箱网络特性

| 协议 | 可用性 | 说明 |
|------|--------|------|
| HTTP | ✅ 可用 | gtimg.cn 全天候稳定，push2 间歇性（~30%丢包，重试恢复） |
| HTTPS | ❌ 不可用 | SSL证书验证失败（sandbox 无完整 cert 链） |
| NO_PROXY | ✅ 有效 | 设置后绕过 Clash 代理 |

## 失败前总重试模式

push2.eastmoney.com CDN 约30%请求返回 `RemoteDisconnected`。所有 push2 调用必须带重试：

```python
import os, json, urllib.request, time
os.environ['NO_PROXY'] = '.eastmoney.com,.gtimg.cn'

def get_push2(code, market='1'):
    url = f"http://push2.eastmoney.com/api/qt/stock/get?secid={market}.{code}&fields=f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f161,f168,f169,f170"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            if data.get('data') and data['data'].get('f43') is not None:
                return data['data']
        except Exception:
            if attempt < 2:
                time.sleep(1)
    return None
```

## 模式1：实时行情（gtimg.cn，推荐）

最可靠的数据源，execute_code 中可用，无需重试。

```python
import os, urllib.request
os.environ['NO_PROXY'] = '.gtimg.cn'

# 单股
resp = urllib.request.urlopen(
    urllib.request.Request("http://qt.gtimg.cn/q=sh600519"),
    timeout=10
)
text = resp.read().decode('gbk')
fields = text.split('~')
# fields[3]=现价, fields[31]=涨跌额, fields[32]=涨跌幅%
# fields[37]=成交额(万), fields[38]=换手率%

# 批量（最多约20只）
codes = "sh600519,sz000001,sh600000"
resp = urllib.request.urlopen(
    urllib.request.Request(f"http://qt.gtimg.cn/q={codes}"),
    timeout=10
)
text = resp.read().decode('gbk')
for line in text.split(';'):
    if line.strip():
        fields = line.split('~')
        print(fields[1], fields[3], fields[32])  # 名称, 现价, 涨跌幅

# 指数
resp = urllib.request.urlopen(
    urllib.request.Request("http://qt.gtimg.cn/q=sh000001,sz399001,sz399006"),
    timeout=10
)
text = resp.read().decode('gbk')
```

### gtimg.cn 字段映射（个股）

| 索引 | 字段 | 类型 | 备注 |
|------|------|------|------|
| 1 | 名称 | str | |
| 3 | 最新价 | float | |
| 4 | 昨收 | float | |
| 5 | 今开 | float | |
| 6 | 成交量(手) | int | |
| 7-30 | 买卖盘 | - | 五档+逐笔 |
| 30 | 时间戳 | str | yyyyMMddHHmmss |
| 31 | 涨跌额 | float | |
| 32 | 涨跌幅% | float | |
| 33 | 最高价 | float | |
| 34 | 最低价 | float | |
| 35 | 价/量/额 | str | "price/vol/amt" |
| 36 | 成交量 | int | 重复 |
| 37 | 成交额(万) | float | 除以10000转亿 |
| 38 | 换手率% | float | |
| 39 | 市盈率 | float | 动态PE |
| 45 | 总市值 | float | |
| 46 | 流通市值 | float | |

### gtimg.cn 字段映射（指数）

| 索引 | 字段 |
|------|------|
| 31 | 涨跌额 |
| 32 | 涨跌幅% |
| 33 | 最高 |
| 34 | 最低 |
| 37 | 成交额(万) |

## 模式2：push2 实时行情（带重试）

```python
def get_stock(code, market='1'):
    url = f"http://push2.eastmoney.com/api/qt/stock/get?secid={market}.{code}&fields=f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f161,f168,f169,f170,f84,f85"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            d = data.get('data')
            if d and d.get('f43') is not None:
                return {
                    'price': float(d['f43'])/100,
                    'chg_pct': float(d.get('f170',0))/100,
                    'chg_val': float(d.get('f169',0))/100,
                    'pre_close': float(d.get('f60',0))/100,
                    'open': float(d.get('f46',0))/100,
                    'high': float(d.get('f44',0))/100,
                    'low': float(d.get('f45',0))/100,
                    'volume': d.get('f47',0),
                    'amount': float(d.get('f48',0))/100000000 if d.get('f48') else 0,
                }
        except:
            time.sleep(1)
    return None
```

## 模式3：AkShare 资金流向（带重试）

AkShare 在 execute_code 中第1次可能失败，第2-3次成功。

```python
import os, time, akshare as ak
os.environ['NO_PROXY'] = '.eastmoney.com'

for attempt in range(3):
    try:
        df = ak.stock_individual_fund_flow(stock="600519", market="sh")
        # 取最近3行
        recent = df.tail(3)
        for _, row in recent.iterrows():
            print(f"{row['日期']} 主力{row['主力净流入-净额']:.0f}({row['主力净流入-净占比']:.1f}%)")
        break
    except Exception as e:
        if attempt < 2:
            time.sleep(2)
```

## 模式4：AkShare 成交额更大的全A行情

不适用于 execute_code（HTTPS），需用 terminal：

```python
# terminal 内执行：
NO_PROXY='.eastmoney.com' python3 -c "
import akshare as ak
df = ak.stock_zh_a_spot()
codes = ['600519','000001','600000']
print(df[df['代码'].isin(codes)][['代码','名称','最新价','涨跌幅']])
"
```

## 不适用于 execute_code 的操作

| 操作 | 原因 | 替代方案 |
|------|------|---------|
| HTTPS API 调用 | SSL cert fail | 用 terminal 或 analyst.py |
| SerpAPI 新闻搜索 | HTTPS 不可用 | terminal + analyst.py news |
| Sina 财经API | HTTPS 不可用 | terminal + curl |
| web_search / 浏览器 | 工具不可用 | delegate_task 但需防伪造 |
