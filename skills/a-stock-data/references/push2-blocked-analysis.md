# push2.eastmoney.com 封锁诊断报告

## 结论

**永久不可修复。** push2 系列 API 从境外 IP 段（包括本机当前网络）被 CDN/WAF 主动丢弃请求。与代理模式（TUN/redir-host）无关，与 ClashX 开关无关。

## 诊断过程

### 网络层
```
TCP 80 → ✅ 连接成功
TLS 443 → ✅ 握手成功
ping → ✅ 26ms 0%丢包
```

### HTTP 层
```
HTTP GET → ❌ "Empty reply from server" (curl err 52)
HTTPS GET → ❌ "Remote end closed connection without response"
HTTP/2 → ❌ 同上
```

### DNS
```
push2.eastmoney.com → push2ipv6.trafficmanager.cn (Azure Traffic Manager)
→ 43.144.251.121 / 47.112.165.11 / 61.129.129.196 (3个CDN节点)
→ 所有节点表现一致：API路径通通502或空响应
```

### 子域名测试
| 子域名 | 根路径(/) | API路径 | 
|--------|-----------|---------|
| push2.eastmoney.com | 404 ✅ | 502 ❌ |
| push2his.eastmoney.com | 404 ✅ | 502 ❌ |
| 82.push2.eastmoney.com | 404 ✅ | 502 ❌ |
| 17.push2.eastmoney.com | 404 ✅ | 502 ❌ |
| **push2ex.eastmoney.com** | 404 ✅ | **200 ✅** |

**关键发现：** `push2ex.eastmoney.com` 是唯一可用的子域名。`stock_zt_pool_em()` 走的就是它。

## 替代方案（已验证）

### ✅ 通过 AkShare（不走 push2）

| 函数 | 后端 | 状态 | 用途 |
|------|------|------|------|
| `stock_zh_a_spot()` | 新浪 | ✅ | 全A实时行情 |
| `stock_zh_a_hist_tx()` | 腾讯 | ✅ | 历史K线 |
| `stock_board_industry_name_ths()` | 同花顺 | ✅ | 行业板块列表(90个) |
| `stock_board_concept_name_ths()` | 同花顺 | ✅ | 概念板块列表(373个) |
| `stock_zh_index_spot_sina()` | 新浪 | ✅ | 指数行情 |
| `stock_zt_pool_em()` | push2ex | ✅ | 涨停板池 |
| `stock_individual_spot_xq()` | 雪球 | ⚠️ 盘后稳定 | 个股信息 |
| `stock_individual_fund_flow()` | push2his | ❌ | 资金流向——用 SerpAPI 新闻提取替代 |

### ✅ 直接 HTTP（不依赖 AkShare）

| 数据 | URL | 编码 | 备注 |
|------|-----|------|------|
| 实时行情 | `http://qt.gtimg.cn/q=sh600519` | GBK | 单次查询 |
| 历史K线 | `http://ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,day,,,60,qfq` | UTF-8 | JSON，需跟302 |
| 指数行情 | `http://qt.gtimg.cn/q=sh000001` | GBK | 与个股字段位置不同 |
| 涨停板池 | `http://push2ex.eastmoney.com/getTopicZTPool?type=ztgc` | UTF-8 | JSON |

## data_cache.py 当前回退链

```
K线: 腾讯ifzq → AkShare stock_zh_a_hist_tx → 新浪 → BaoStock
实时: 腾讯 qt.gtimg.cn（主）+ AkShare stock_zh_a_spot（新浪备）
涨停板: AkShare stock_zt_pool_em → push2ex
板块: AkShare stock_board_industry_name_ths → 同花顺
指数: 腾讯 qt.gtimg.cn
资金流向: ⚠️ 新闻提取（SerpAPI, news.py）
```
