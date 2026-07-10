# 东方财富数据源故障诊断报告

**日期**: 2026-07-09  
**诊断人**: Hermes (自动化诊断)  
**严重程度**: 🔴 高 — 核心资金流数据不可用，影响投研决策链路

---

## 1. 问题描述

A股投研系统大量依赖东方财富 `push2` / `push2his` 子域名的 API 获取行情和资金流数据。近期出现频繁的 `RemoteDisconnected: Remote end closed connection without response` 错误，导致 circuit breaker 反复触发（open 状态），后续请求被短路。

### 症状汇总

| 症状 | 表现 |
|------|------|
| `push2his.eastmoney.com` fflow/daykline | 100% 失败（空响应） |
| `push2.eastmoney.com` clist/get | 100% 失败（空响应） |
| `push2his.eastmoney.com` stock/get | 100% 失败（空响应） |
| `push2his.eastmoney.com` trends2/get | 100% 失败（空响应） |
| Circuit breaker 频繁 open | 阈值仅 3 次失败，open 持续 300 秒 |
| `datacenter-web.eastmoney.com` | ✅ 正常 |
| `reportapi.eastmoney.com` | ✅ 正常 |
| `mxapi.eastmoney.com` (MCP) | ✅ 正常 |
| `push2his` kamt.kline/get | ✅ 正常 |

---

## 2. 根因分析

### 2.1 直接原因：服务端应用层连接拒绝

**核心发现**：`push2` / `push2his` 服务器对特定 API 路径实施了连接级别的封锁。

**证据链**：

1. **TLS 握手成功**，TCP 连接建立，但服务器立即关闭连接（发送空响应）
   ```
   * SSL connection using TLSv1.3 / AEAD-AES256-GCM-SHA384
   * ALPN: server accepted http/1.1
   > GET /api/qt/stock/fflow/daykline/get?... HTTP/1.1
   * Request completely sent off
   * Empty reply from server
   * Closing connection
   ```

2. **路径级别的选择性封锁**：同一服务器（`28.0.0.20`）上，`/api/qt/kamt.kline/get` 正常返回，而 `/api/qt/stock/fflow/daykline/get` 被拒绝

3. **参数无关**：无论是否添加 `ut` token、`Referer`、`User-Agent`、`cb` 回调参数，均无法绕过

4. **协议无关**：HTTP 和 HTTPS 均失败

5. **代理无关**：通过 Clash Verge 代理和直连均失败（结果一致）

### 2.2 触发条件分析

从 `provider_health/eastmoney.json` 的历史窗口数据看：

| 日期 | 成功率 | 备注 |
|------|--------|------|
| 2026-07-06 | 10% (1/10) | 首次出现大规模失败 |
| 2026-07-07 | 36% (16/45) | 02:30 和 07:40 UTC 有短暂恢复窗口 |
| 2026-07-08 | 14% (1/7) | 基本不可用 |
| 2026-07-09 | 46% (11/24) | 06:50 UTC 短暂恢复后再次失败 |

**关键观察**：
- 短暂恢复窗口出现在间隔较远的请求之后（如 07:40 UTC 连续 8 次成功）
- 连续快速请求（间隔 <2 秒）几乎 100% 失败
- 恢复后一旦出现连续请求，立即再次被封

**推断**：东财 `push2` / `push2his` 服务器对来自本 IP（`28.0.0.56` / `28.0.0.20` 解析）的请求实施了 **路径级别的速率限制**，触发条件包括：
- 单位时间内的请求频率过高
- 批量请求模式（多个子任务短时间内密集调用）
- 可能的 IP 信誉评分降级

### 2.3 间接原因：系统侧放大因素

#### 2.3.1 代理配置缺陷

系统运行 Clash Verge 代理（`127.0.0.1:7897`），macOS 系统级代理已启用。Python `urllib` 自动检测并使用系统代理：

```python
# Python 检测到的代理配置
{'http': 'http://127.0.0.1:7897', 'https': 'http://127.0.0.1:7897'}
```

但 **`NO_PROXY` 环境变量未在 Python 进程中设置**（仅在 `.bashrc` 中定义，不会传递到 cron/子进程）。这意味着：
- 所有东财请求都经过 Clash Verge 代理
- 代理出口 IP 可能与直连 IP 不同，影响东财的速率限制判断
- `.hermes/.env` 中设置了 `NO_PROXY`，但 `load_hermes_env()` 只加载到 `os.environ`，不会影响已初始化的 `urllib` 代理处理器

#### 2.3.2 Circuit Breaker 阈值过低

当前配置：
```json
{
  "circuit_failure_threshold": 3,    // 仅 3 次失败就 open
  "circuit_open_seconds": 300        // open 持续 5 分钟
}
```

问题：
- 3 次阈值太低，一次短暂抖动就会触发 open
- open 期间（300 秒）所有请求被短路，包括可能成功的 `kamt.kline` 等端点
- `half_open` 状态下再次失败会立即回到 open，形成 "open → half_open → 立即失败 → open" 的死循环
- 不同端点共享同一个 `push2his` 域名下的 circuit key，一个端点失败会影响同域名的其他端点

#### 2.3.3 请求突发模式

从健康数据可以看到，系统存在请求突发行为：
```
07:40:02 → 07:40:03 → 07:40:04 → 07:40:05 → 07:40:06 → 07:40:08 → 07:40:09 → 07:40:10
```
8 个请求在 8 秒内连续发出（间隔 ~1 秒），虽然有 `minimum_interval_seconds: 1.1` 的限制，但这是跨进程的文件锁协调，同一进程内的连续调用不受此限制。

#### 2.3.4 僵尸 Circuit Breaker

两个 datacenter 电路从 2026-06-20 起一直处于 open 状态（已 19 天）：

| 电路 | 错误 | 状态 |
|------|------|------|
| `datacenter:RPT_ORG_SURVEY` | `NOTICEDATE排序列不存在` (9501) | open 19天 |
| `datacenter:RPT_HOLDER_TRADE_STOCK` | `报表配置不存在` (9501) | open 19天 |

这两个错误是东财 API 字段变更导致的，不会自动恢复。`open_until_epoch` 已过期但电路状态未自动重置为 `half_open`（因为代码只在 `_circuit_before_call` 时检查时间，如果不调用该端点就不会重置）。

### 2.4 DNS 异常

所有东财域名解析到 `28.0.0.x` 地址段：
```
push2.eastmoney.com      → 28.0.0.56
push2his.eastmoney.com   → 28.0.0.20
datacenter-web.eastmoney.com → 28.0.0.6
mxapi.eastmoney.com      → 28.0.0.145
```

`28.0.0.0/8` 是 APNIC 分配的公网地址段，但在国内可能通过特殊路由基础设施访问。DNS 服务器为 `114.114.114.114`（中国公共 DNS）。这不是问题的直接原因，但可能影响路由路径和 CDN 调度。

---

## 3. 影响范围

### 3.1 直接影响的功能

| 功能模块 | 影响程度 | 说明 |
|----------|----------|------|
| **资金流向监控** (`capital_flow_monitor.py`) | 🔴 高 | 个股/板块主力资金流数据不可用 |
| **板块强度分析** (`theme_strength.py`) | 🔴 高 | 依赖 `push2 clist` 获取板块行情 |
| **新闻驱动板块分析** (`news-to-sector/main.py`) | 🔴 高 | 依赖 `push2 clist` 获取板块映射 |
| **解禁数据** (`fetch_lockups`) | 🟢 低 | 走 `datacenter-web`，正常 |
| **研报数据** (`fetch_reports`) | 🟢 低 | 走 `reportapi`，正常 |
| **龙虎榜** (`fetch_dragon_tiger`) | 🟢 低 | 走 `datacenter-web`，正常 |
| **北向资金** (`kamt.kline`) | 🟢 低 | `kamt.kline` 端点正常 |
| **MCP 智能分析** | 🟢 低 | 走 `mxapi`，正常 |

### 3.2 降级策略现状

`capital_flow_monitor.py` 已有降级设计：
- 当东财资金流不可用时，使用腾讯量价数据作为代理指标
- 但代理指标只能反映换手率和价格变化，无法提供主力/散户资金流向的精确数据
- 北向资金通过 `kamt.kline` 获取，不受影响

### 3.3 Circuit Breaker 连锁影响

当前 4 个电路处于 open 状态，但它们的 `circuit_key` 是按端点路径独立的，不会互相阻塞。真正的问题是 `fflow/daykline` 的电路 open 后，300 秒内所有对该端点的调用都会被短路，即使服务器侧的封锁可能已经解除。

---

## 4. 修复建议

### 4.1 🔴 紧急：设置 NO_PROXY 环境变量

**问题**：Python 进程未继承 `.bashrc` 中的 `no_proxy` 设置，所有请求经过代理。

**修复方案**：在 `a_stock_http.py` 的 `load_hermes_env()` 中强制设置代理绕过：

```python
def load_hermes_env() -> Dict[str, str]:
    """加载 $HERMES_HOME/.env 到 os.environ，并确保代理绕过生效"""
    env_file = _env_file()
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k not in os.environ:
                        os.environ[k] = v
    # 确保 NO_PROXY 生效（urllib 在 import 时缓存代理设置）
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy", "")
    if no_proxy:
        os.environ["NO_PROXY"] = no_proxy
        os.environ["no_proxy"] = no_proxy
    return dict(os.environ)
```

**同时**，在每个入口脚本的最开始调用 `load_hermes_env()`，确保在任何 `urllib` 导入之前设置好环境变量。

**更彻底的方案**：在 `http_client.py` 中强制绕过代理：

```python
def _build_opener():
    """构建不使用代理的 opener，避免系统代理干扰国内数据源"""
    proxy_handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(proxy_handler)

# 在 HttpClient.__init__ 中使用
self._opener = opener or _build_opener()
```

### 4.2 🔴 紧急：重置僵尸 Circuit Breaker

手动重置两个已 open 19 天的 datacenter 电路：

```bash
# 编辑 health.json，将两个 open 的 datacenter 电路重置
python3 -c "
import json
path = '/Users/eleven/.hermes/skills/stock-triage/cache/provider_coordination/eastmoney/health.json'
with open(path) as f:
    data = json.load(f)
for key in ['datacenter:RPT_ORG_SURVEY', 'datacenter:RPT_HOLDER_TRADE_STOCK']:
    if key in data.get('circuits', {}):
        data['circuits'][key]['state'] = 'closed'
        data['circuits'][key]['consecutive_failures'] = 0
        data['circuits'][key]['open_until_epoch'] = 0
with open(path, 'w') as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
print('Reset done')
"
```

**同时修复 API 调用**：
- `RPT_ORG_SURVEY`：排序列 `NOTICEDATE` 已被东财移除，需更新为正确的列名
- `RPT_HOLDER_TRADE_STOCK`：报表名已变更，需查询东财最新的报表名

### 4.3 🟡 重要：调整 Circuit Breaker 参数

当前配置过于激进。建议调整：

```json
{
  "circuit_failure_threshold": 5,     // 3 → 5，避免短暂抖动触发
  "circuit_open_seconds": 120         // 300 → 120，缩短 open 时间
}
```

修改文件：`/Users/eleven/meta-11/a-stock-agent-system/config/data_access.json`

### 4.4 🟡 重要：实现端点级降级与备用数据源

对于 `fflow/daykline`（资金流向）被封锁的情况：

1. **增加备用端点**：尝试使用 `push2.eastmoney.com` 的 `/api/qt/stock/fflow/kline/get`（分钟级资金流，可能未被封锁）
2. **增加请求间隔**：对 `push2` / `push2his` 的请求增加额外的间隔（建议 ≥3 秒）
3. **强化腾讯降级**：当东财资金流不可用时，使用腾讯行情的成交量变化率作为资金流向的粗略代理

### 4.5 🟢 优化：请求节奏控制

当前 `minimum_interval_seconds: 1.1` 仅通过文件锁跨进程协调。建议：

1. **进程内节流**：在 `eastmoney_intelligence.py` 中增加进程内的时间戳检查，避免同进程内的连续快速调用
2. **请求分散**：将批量查询（如多个板块的资金流）分散到更长的时间窗口
3. **指数退避增强**：在连续失败后，退避时间应指数增长到更长（如 30 秒、60 秒），而不是固定 300 秒 open

### 4.6 🟢 长期：监控与告警

1. 定期检查 circuit breaker 状态，对长时间 open 的电路发出告警
2. 监控东财各端点的成功率，当成功率低于 50% 时自动切换到备用数据源
3. 记录每次封锁的触发模式（请求频率、时间点），优化请求策略

---

## 5. 验证方法

### 5.1 验证代理绕过生效

```bash
cd /Users/eleven/meta-11/a-stock-agent-system
.venv/bin/python -c "
import sys, os
sys.path.insert(0, 'skills/common')
from a_stock_http import load_hermes_env
load_hermes_env()
import urllib.request
proxies = urllib.request.getproxies()
print(f'Proxies: {proxies}')  # 应该为空或只有 no=* 
"
```

### 5.2 验证端点连通性

```bash
# 直连测试（绕过代理）
curl -sS --max-time 5 --noproxy '*' \
  "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?lmt=1&klt=101&secid=1.000001&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
```

### 5.3 验证 Circuit Breaker 重置

```bash
cd /Users/eleven/meta-11/a-stock-agent-system
.venv/bin/python -c "
import sys; sys.path.insert(0, 'skills/common')
from eastmoney_intelligence import provider_health
h = provider_health()
print(f'State: {h[\"state\"]}')
print(f'Open circuits: {h[\"open_circuits\"]}')
"
```

---

## 6. 总结

| 维度 | 结论 |
|------|------|
| **根本原因** | 东财 `push2` / `push2his` 服务器对特定 API 路径实施连接级封锁（疑似反爬/WAF） |
| **触发因素** | 请求频率过高 + 可能的代理出口 IP 信誉问题 |
| **放大因素** | 代理未绕过、Circuit Breaker 阈值过低、请求突发模式 |
| **最紧急修复** | ① 设置 NO_PROXY 绕过代理 ② 重置僵尸电路 ③ 调整 CB 参数 |
| **长期方案** | 请求节奏优化 + 多数据源降级 + 监控告警 |

**优先级排序**：
1. 🔴 设置 `NO_PROXY` 环境变量（5 分钟）
2. 🔴 重置僵尸 circuit breaker（2 分钟）
3. 🟡 调整 circuit breaker 参数（5 分钟）
4. 🟡 修复东财 API 字段变更（30 分钟）
5. 🟢 请求节奏优化（1 小时）
6. 🟢 监控告警建设（半天）
