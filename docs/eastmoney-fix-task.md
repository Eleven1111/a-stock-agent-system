# 东财数据源修复任务

## 目标
修复东方财富 `push2`/`push2his` API 频繁失败的问题。

## 诊断报告
`docs/eastmoney-diagnosis-20260709.md` 已有完整分析，核心结论：
- 东财服务器对特定 API 路径实施连接级封锁（反爬/WAF）
- 系统侧有三个放大因素需要修复

## 任务清单（按优先级）

### 任务1：NO_PROXY 代理绕过 🔴
**问题**：Python 进程未继承 `.bashrc` 中的 `no_proxy`，所有东财请求经过 Clash Verge 代理（127.0.0.1:7897），代理出口 IP 被东财限流。

**修改文件**：`skills/common/http_client.py`

**方案**：在 `HttpClient` 的请求中强制绕过代理。具体做法：
1. 在 `http_client.py` 顶部或 `HttpClient.__init__` 中，检测 `NO_PROXY`/`no_proxy` 环境变量
2. 如果未设置，自动添加 `push2.eastmoney.com,push2his.eastmoney.com,datacenter-web.eastmoney.com,reportapi.eastmoney.com` 到 `no_proxy`
3. 或者更彻底：在 `_build_opener()` 中使用 `ProxyHandler({})` 绕过所有代理（系统是投研专用机，不需要代理访问国内数据源）
4. 确保在 `urllib` import 之前设置，因为 `urllib` 会缓存代理设置

**参考代码**：
```python
import os
import urllib.request

# 在模块加载时设置
_no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy", "")
if not _no_proxy:
    os.environ["NO_PROXY"] = "push2.eastmoney.com,push2his.eastmoney.com,datacenter-web.eastmoney.com,reportapi.eastmoney.com,mxapi.eastmoney.com"
    os.environ["no_proxy"] = os.environ["NO_PROXY"]
```

或者在 HttpClient 中使用无代理 opener：
```python
def _make_no_proxy_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))
```

### 任务2：重置僵尸 Circuit Breaker 🔴
**问题**：2个 datacenter 电路因东财 API 字段变更已 open 19天，不会自动恢复。

**修改文件**：
- `skills/common/eastmoney_intelligence.py`（修复 API 调用）
- 状态文件：`/Users/eleven/.hermes/skills/stock-triage/cache/provider_coordination/eastmoney/health.json`（重置状态）

**方案**：
1. 在 `eastmoney_intelligence.py` 中找到 `RPT_ORG_SURVEY` 和 `RPT_HOLDER_TRADE_STOCK` 的调用代码
2. 修复 `RPT_ORG_SURVEY`：排序列 `NOTICEDATE` 已被东财移除，改用正确的列名（查东财 datacenter API 文档或尝试 `NOTICE_DATE`/`NOTICEdate`/`UPDATE_DATE`）
3. 修复 `RPT_HOLDER_TRADE_STOCK`：报表名已变更，查东财最新的报表名
4. 手动重置 health.json 中这两个电路的状态为 `closed`

### 任务3：调整 Circuit Breaker 参数 🟡
**问题**：3次失败就 open 300秒，太激进。短暂抖动就全断。

**修改文件**：`config/data_access.json`

**方案**：
```json
{
  "circuit_failure_threshold": 5,
  "circuit_open_seconds": 120
}
```

### 任务4：请求节奏优化 🟡
**问题**：同进程内连续快速请求（间隔<2秒）触发东财限流。

**修改文件**：`skills/common/http_client.py`

**方案**：
1. 在 `HttpClient` 中增加进程内节流：同 source 的请求间隔至少 2 秒
2. 连续失败后指数退避增强：30秒 → 60秒 → 120秒（而不是固定 300 秒 open）
3. `half_open` 状态下只发一个探测请求，成功后立即 `closed`

### 任务5：Circuit Breaker 端点隔离 🟢
**问题**：同一域名下不同端点共享 circuit key，一个端点失败影响其他端点。

**修改文件**：`skills/common/http_client.py`

**方案**：circuit key 从 `source`（域名级）改为 `source + path`（端点级），让 `kamt.kline` 不被 `fflow/daykline` 的失败影响。

## 约束
- 项目目录：`/Users/eleven/meta-11/a-stock-agent-system`
- Python venv：`.venv/bin/python`
- 不要修改 `SOUL.md`、`AGENTS.md`、`MEMORY.md`
- 修改前先读文件理解现有逻辑
- 每个任务修改后运行测试验证
- 测试命令：`cd /Users/eleven/meta-11/a-stock-agent-system && PYTHONPATH=skills/common .venv/bin/python -c "from http_client import HttpClient; print('OK')"`

## 验证方法
```bash
# 验证代理绕过
cd /Users/eleven/meta-11/a-stock-agent-system
.venv/bin/python -c "
import sys; sys.path.insert(0, 'skills/common')
import urllib.request, os
os.environ['NO_PROXY'] = 'push2.eastmoney.com'
proxies = urllib.request.getproxies_environment()
print('NO_PROXY:', os.environ.get('NO_PROXY'))
print('Proxies:', proxies)
"

# 验证东财连通性
curl -sS --max-time 5 --noproxy '*' \
  "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.603616&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55&klt=101&fqt=1&beg=20260701&end=20260709" | head -c 200

# 验证 circuit breaker 状态
.venv/bin/python -c "
import sys; sys.path.insert(0, 'skills/common')
from eastmoney_intelligence import provider_health
h = provider_health()
print(f'State: {h[\"state\"]}')
print(f'Open circuits: {h.get(\"open_circuits\", 0)}')
"
```
