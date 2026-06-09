# 腾讯 ifzq K线 API 格式

## 端点

```
https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={PREFIX}{CODE},day,,,{DAYS},qfq
```

- `PREFIX`: sh（沪市）/ sz（深市）
- `CODE`: 6位数字代码
- `DAYS`: 请求K线根数
- `qfq`: 前复权

**必须带 sh/sz 前缀**，否则返回 `{"code":0, "msg":"param error", "data":[]}`

## 返回格式

```json
{
  "code": 0,
  "msg": "",
  "data": {
    "sh603859": {
      "qfqday": [
        ["2026-06-09", "51.00", "51.27", "52.49", "49.99", "288897"]
      ]
    }
  }
}
```

## 字段顺序（重要！不要搞反）

每根K线是一个长度6的数组：
```
[date, open, close, high, low, volume_or_amount]
```

不是 [date, open, high, low, close, volume]！close 在 open 之后，high 之前。

## 特殊坑位

1. **volume字段类型可变**：今日K线的第5个元素（volume）可能是 dict 或 list 而非字符串。包含除权信息如 `{"nd":"2025","fh_sh":"0.9","djr":"2026-06-08"}`。处理时需用 `isinstance` 判断类型。

2. **除权日**：当日K线日期为除权日（cqr），数据为前复权价格，可直接用于计算。

3. **网络**：需设置 `NO_PROXY=.gtimg.cn,.eastmoney.com` 绕过代理。

## 示例代码

```python
import os, urllib.request, json

os.environ["NO_PROXY"] = ".gtimg.cn,.eastmoney.com"

PREFIX = "sh" if CODE.startswith("6") else "sz"
url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={PREFIX}{CODE},day,,,{DAYS},qfq"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req, timeout=15) as resp:
    raw = json.loads(resp.read().decode("utf-8"))

kline = raw["data"][f"{PREFIX}{CODE}"]["qfqday"]

# 解析
for k in kline:
    date, o, c, h, l = str(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])
    v_raw = k[5]
    if isinstance(v_raw, (int, float)):
        vol = int(v_raw)
    elif isinstance(v_raw, str):
        vol = int(float(v_raw))
    else:
        vol = 0  # dict
```

## 腾讯实时行情 API

```
http://qt.gtimg.cn/q={PREFIX}{CODE}
```

返回 gbk 编码字符串，以 `~` 分隔的字段。关键字段索引：
- 1: 名称
- 2: 代码
- 3: 现价
- 4: 昨收
- 5: 开盘
- 6: 成交量(手)
- 32: 涨跌幅(%)
- 33: 最高
- 34: 最低
- 37: 成交额(元)
- 39: 市盈率
- 45: 总市值

```python
url = f"http://qt.gtimg.cn/q=sh603859"
with urllib.request.urlopen(req, timeout=10) as resp:
    raw = resp.read().decode("gbk")
for line in raw.strip().split(";"):
    val = line.split("=", 1)[1].strip().strip('"').split("~")
    name, price, chg_pct = val[1], float(val[3]), float(val[32])
```
