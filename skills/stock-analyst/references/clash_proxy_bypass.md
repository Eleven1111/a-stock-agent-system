# Clash Verge 代理绕过（NO_PROXY）

## 背景

系统上运行 **Clash Verge**（mihomo 内核），作为系统 HTTP 代理（`127.0.0.1:7897`）。虽然 `.cn` 域名的规则设为 `DIRECT`，但 Clash 做了 DNS 劫持——所有域名都返回 fake IP（`198.18.x.x` 范围），再由内部路由决定走代理还是直连。

Python `urllib` 直接连接 fake IP 时，Clash 转发表现不稳定，导致东财 push2 端点返回 502 或断连。

**精细化的故障表现（实测）：**

| 方式 | push2 HTTP 行情 (80) | push2his HTTPS 资金流 (443) | QT.GTIMG 行情 |
|------|---------------------|---------------------------|--------------|
| `curl` (直接) | ✅ 正常 | ✅ 正常 | ✅ 正常 |
| `curl --noproxy '*'` | ✅ 正常 | ✅ 正常 | ✅ 正常 |
| `urllib` (无设置) | ❌ 502 | ❌ 断连 | ❌ 502 |
| `urllib` + NO_PROXY | ✅ 正常 | ✅ 正常 | ✅ 正常 |
| `requests` (直接) | ✅ 正常 | ❌ ProxyError | ✅ 正常 |
| `requests` + NO_PROXY | ✅ 正常 | ✅ 正常 | ✅ 正常 |
| `akshare fund_flow` | — | ❌ ProxyError | — |
| `akshare fund_flow` + NO_PROXY | — | ✅ 正常 (120行) | — |

**根因：** Clash 将 DNS 解析劫持到 `198.18.x.x`（fake IP 池），`urllib` 连接这些 fake IP 时走系统代理端口（127.0.0.1:7897），Clash 内部路由处理这些 IP 存在缺陷——部分协议/端口组合正确路由（如 HTTP 80 → 直连），部分失败（HTTPS 443 → 502/断连）。`NO_PROXY` 让 Python 直接跳过代理 DNS/路由，直达真实服务器。

## 解决方案

在 `.env` 中设置 `NO_PROXY` 环境变量，让 Python 的代理处理层直接绕过 Clash 转发这些域名：

```env
# ~/.hermes/.env
NO_PROXY=.eastmoney.com,.gtimg.cn,.sinajs.cn,.10jqka.com.cn,.hexun.com,.cnstock.com,.stcn.com,.cs.com.cn,.p5w.net
```

同时需要在 `scripts/news.py` 中自动从 `.env` 加载 `NO_PROXY`（因为 cron 环境不自动读取 `.env`）：

```python
if not os.environ.get("NO_PROXY"):
    try:
        with open(os.path.expanduser("~/.hermes/.env")) as f:
            for line in f:
                if line.startswith("NO_PROXY="):
                    os.environ["NO_PROXY"] = line.split("=", 1)[1].strip().strip("'").strip('"')
                    break
    except:
        pass
```

## 效果

| 端点 | 修复前 | 修复后 |
|------|--------|--------|
| `push2.eastmoney.com`（实时行情） | ❌ 502 Bad Gateway | ✅ 正常 |
| `push2his.eastmoney.com`（资金流向，HTTPS） | ❌ ProxyError | ✅ 正常 |
| `stock_individual_fund_flow()`（akshare 资金流） | ❌ ProxyError | ✅ 120 行完整数据 |
| `qt.gtimg.cn`（腾讯行情） | ✅ 本来就是通的 | ✅ 不变 |
| SerpAPI 搜索 | ✅ 直接走公网 | ✅ 不变 |
| 新浪财经 | ✅ 本来就是通的 | ✅ 不变 |

## 验证方式

```python
import os, urllib.request, json

os.environ['NO_PROXY'] = '.eastmoney.com,.gtimg.cn,.sinajs.cn'

# 测试 push2 实时行情
url = "http://push2.eastmoney.com/api/qt/stock/get?secid=1.600519&fields=f43,f44,f45"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
data = json.loads(urllib.request.urlopen(req, timeout=10).read())

# 测试 akshare 资金流向
import akshare as ak
df = ak.stock_individual_fund_flow(stock="600519", market="sh")
```

## 为什么不改 Clash 规则本身？

两种方案都试过：在 Clash 的 `Merge.yaml` 加 `DOMAIN-SUFFIX,eastmoney.com,DIRECT`，或在 Clash 控制面板加规则。这两个方案理论上应该生效，但实际上 Clash 的 DNS 劫持在 Python urllib 层面仍然导致连接不稳定。

`NO_PROXY` 方案直接在应用层解决——Python 的 urllib/requests 看到域名匹配 `NO_PROXY`，根本不去问系统代理，直接走本机网络。更干净、更可预测。

## 注意事项

- `NO_PROXY` 需要同时设到 `.env`（给交互会话用）和 `news.py` 的自动加载逻辑（给 cron 和独立 Python 调用用）
- 新加入 stock-analyst 的 Python 脚本如果用到 urllib/requests，确保 import 后立即调用 `from scripts.news import ensure_env` 或类似初始化
- 这个方案依赖 DNS 正常解析这些域名（Clash 虽然劫持 DNS，但 `.cn` 域名直连后域名的真实解析要能正常到达）
