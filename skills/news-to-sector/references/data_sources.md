# A股数据API参考

macOS 上的 ClashX/V2Ray 等系统代理（127.0.0.1:7897）会使 Python `requests` 调用国内金融 API 失败。以下 API 均通过 `curl --noproxy '*'` 绕过代理。

## 实时行情

| 数据源 | URL | 编码 | 可用时间 |
|--------|-----|------|---------|
| 腾讯（推荐） | `http://qt.gtimg.cn/q=sz000983` | GBK | 全天 |
| 新浪 | `https://hq.sinajs.cn/list=sz000983` | GBK | 全天 |
| 东方财富 | `https://push2.eastmoney.com/api/qt/clist/get?...` | JSON | 交易时段 |
| 东方财富个股 | `https://push2.eastmoney.com/api/qt/stock/get?secid=0.000983` | JSON | 交易时段 |

## 历史K线

| 数据源 | URL | 编码 | 可用时间 |
|--------|-----|------|---------|
| 腾讯（推荐） | `https://ifzq.gtimg.cn/appstock/app/fqkline/get?param=sz000983,day,,,22,qfq` | JSON | 全天 |
| 新浪 | `https://quotes.money.163.com/service/chddata.html?code=1000983` | CSV | 全天 |
| 东方财富 | `https://push2.eastmoney.com/api/qt/stock/kline/get?secid=0.000983&klt=101&fqt=1&lmt=30` | JSON | 交易时段 |

## 板块数据

| 数据源 | URL | 说明 |
|--------|-----|------|
| 东财行业板块 | `https://push2.eastmoney.com/api/qt/clist/get?fs=m:90+t:2&fields=f12,f14,f3` | 交易时段可用，有频率限制 |

## 关键注意事项

1. **东财API收盘后不可用** — A股15:00收盘后 push2 系列接口返回空响应。非交易时段请用腾讯API
2. **ClashX TUN模式阻断** — 当ClashX开启TUN模式时，`push2.eastmoney.com`被解析为假IP（198.18.x.x），即使`--noproxy '*'`也无法绕过。`datacenter.eastmoney.com`和`qt.gtimg.cn`不受影响
3. **GBK编码** — 腾讯/新浪的实时API返回GBK编码。Python处理：`raw_bytes.decode('gbk')`（不要用utf-8）
4. **频率限制** — 东财API有严格限流。调用间至少间隔1.5秒，带重试
5. **腾讯fqkline格式：** 返回 `[date, open, close, high, low, volume]`
6. **腾讯qt.gtimg字段映射（个股）：**
   - parts[3] = 现价
   - parts[4] = 昨收
   - parts[31] = 涨跌额
   - parts[32] = 涨跌幅(%)
   - parts[33] = 最高
   - parts[34] = 最低
   - parts[37] = 成交额(万)
   - parts[38] = 换手率
7. **腾讯qt.gtimg字段映射（指数，如sh000001）：**
   - parts[31] = 涨跌额
   - parts[32] = 涨跌幅(%)
   - parts[33] = 最高
   - parts[34] = 最低
   - parts[36] = "价/量/额"组合字符串
   - parts[37] = 成交额(万) ← 注意：个股的成交额在37，但个股的37是成交额(万)，指数的37也是成交额(万)但含义不同：个股的是股票级，指数的是全场级
8. **股票代码规则（腾讯/新浪）：** 深交所前缀 `sz`，上交所前缀 `sh`
