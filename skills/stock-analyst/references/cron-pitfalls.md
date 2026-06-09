# Cron 作业常见陷阱

## 陷阱1：terminal 命令在 cron 上下文中触发审批锁

**症状：** cron job 启动后卡住，日志显示 `tool terminal returned error: status=pending_approval, approval_pending=true`。cron 环境没有用户来批准命令，任务无法完成。

**根因：** 某些 terminal 命令（特别是涉及代理绕过、网络请求、文件写入等操作）被安全系统标记为需要人工批准。在 cron（无人值守）上下文中，这些审批请求永远不会得到响应。

**解决方式：**
- ⚠️ **`execute_code()` 不可用于网络数据采集** — 沙箱无网络能力（见下方勘误）
- 对 stock-analyst，**直接运行 `analyst.py` 命令**（带 NO_PROXY）是最可靠的方式
- 用 `enabled_toolsets` 限制 cron job 的工具集，只暴露需要的工具

### 在 stock-analyst 中的具体案例

高温主题开盘跟踪 cron job 的 terminal 命令：
```bash
curl -s 'http://qt.gtimg.cn/q=sh600011,...' --noproxy '*'
```
这个命令在交互会话中正常，但在 cron 中可能触发审批。

#### ✅ 勘误（2026-06-09 更新）：execute_code 网络能力详解

`execute_code()` 沙箱**有 HTTP 网络能力**，但 HTTPS 不可用（SSL 证书问题）。正确的约束：

```
✅ execute_code + HTTP (urllib) → gtimg.cn 全天候可用，push2 间歇性（约30% CDN丢包）
❌ execute_code + HTTPS (urllib) → SSL证书验证失败（sandbox 无完整 cert 链）
✅ execute_code + retry 模式 → push2 间歇性失败可通过 2-3 次重试恢复
✅ terminal + analyst.py 命令 → 正常工作（脚本内部有代理处理逻辑）
```

**数据源实测结论（按可靠性排序）：**

| 来源 | 协议 | 从 execute_code 可用性 | 说明 |
|------|------|----------------------|------|
| `qt.gtimg.cn` (腾讯实时) | HTTP | ✅ 全天候稳定 | 第一选择 |
| `ifzq.gtimg.cn` (腾讯K线) | HTTP | ✅ 可用但需跟随 302 重定向 | |
| `push2.eastmoney.com` | HTTP | ⚠️ 间歇性 RemoteDisconnected | 重试 2-3 次后可恢复 |
| `money.finance.sina.com.cn` | HTTPS | ❌ SSL 失败 | 需要 terminal |
| SerpAPI / 外部新闻 | HTTPS | ❌ SSL 失败 | 需要 terminal |

**正确做法：**

1. **execute_code 做数据采集时，优先用 gtimg（HTTP）：**
   ```python
   import os, urllib.request, json
   os.environ['NO_PROXY'] = '.gtimg.cn,.eastmoney.com'
   url = "http://qt.gtimg.cn/q=sh600011"
   req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
   resp = urllib.request.urlopen(req, timeout=10)
   text = resp.read().decode('gbk')
   fields = text.split('~')
   # fields[3]: 现价, fields[32]: 涨跌幅%, fields[37]: 成交额(万), fields[38]: 换手率%
   ```

2. **push2 需要 retry（约30%初次失败）：**
   ```python
   for attempt in range(3):
       try:
           resp = urllib.request.urlopen(req, timeout=10)
           data = json.loads(resp.read())
           if data.get('data'): break
       except: time.sleep(1)
   ```

3. **或直接运行 `analyst.py` 命令**（自动处理代理和重试）：
   ```bash
   cd ~/.hermes/skills/stock-analyst && \
   NO_PROXY='.eastmoney.com,.gtimg.cn,.sinajs.cn' \
   ~/.hermes/hermes-agent/venv/bin/python3 analyst.py index
   ```

4. **fund flow（AkShare）在 execute_code 中可用**（需重试2-3次）：
   ```python
   import akshare as ak
   for attempt in range(3):
       try:
           df = ak.stock_individual_fund_flow(stock="600011", market="sh")
           break
       except: time.sleep(2)
   ```

5. **所有 HTTPS 类数据源（SerpAPI 新闻、Sina K线等）不适用于 execute_code**，需用 terminal 或 analyst.py。

## 陷阱2：DeepSeek API 冷启动延迟

**症状：** cron job 的第一次 API 调用耗时 200-300 秒（正常应 <10 秒）。

**根因：** DeepSeek 在长时间未被调用后，第一个请求需要"热机"——加载模型权重、分配推理资源等。

**影响范围：** 仅限当日第一次调用。后续同一会话中的后续调用正常（5-6秒）。

**缓解方式：**
- 对时间敏感的任务（如开盘跟踪），API 调用的 timeout 设到 300 秒以上
- 或者在其他地方预先"暖"一次 API（发一个无关请求）

## 陷阱3：cron job prompt 必须自包含

Cron job 在一个全新的会话中运行，没有当前对话的任何上下文。prompt 必须包含所有需要的背景信息：日期、具体步骤、数据源路径、环境变量等。

不要依赖：
- 当前对话的历史消息
- memory 中的会话特定上下文
- 之前会话的输出

## 陷阱4：同时段 cron 扎堆

**症状：** 多个 cron job 在同一分钟启动，终端/文件/API 资源争用，输出互相干扰，部分 job 超时或乱序。

**用户明确要求：** 所有定时任务必须：

1. **分散时间，不扎堆** — 同一时间段内最多一个 job。比如 08:30 一个、08:55 一个、10:00 一个，而不是都在 09:00。
2. **避免整点** — 尽量 08:30、08:55、11:35、15:15 这样的非整分钟时间，减少并发冲突概率。

**实现方式：** 创建 cron job 时，用 `schedule` 参数手动分配一个合理的时间窗口。参考已建立的节奏：

```
08:30  BuilderPulse日报
08:55  PulseEngine日报
10:00  高温主题开盘跟踪
11:35  午盘热门板块复盘
15:05  封测跟踪
15:15  收盘热门板块复盘
```

新建 job 时选择上表中不存在的时间段，均匀分布。
