# 东方财富数据源鲁棒性

筹码、机构、资金流、事件日历和新闻板块行情统一通过
`skills/common/eastmoney_intelligence.py` 访问东方财富。业务脚本不得直接调用
`request_json`、`curl` 或自行实现重试。

## 请求保护

每次请求依次经过：

1. 共享熔断状态检查。
2. 基于原子目录锁的跨进程、跨机器限速。
3. 单次 HTTP 请求。传输层内部不重试。
4. 严格业务响应校验。
5. 仅对超时、网络错误、429 和 5xx 做一次退避重试。
6. 成功后关闭熔断；连续失败达到阈值后打开熔断。

总尝试次数仍不超过 2。429 优先遵守数字格式的 `Retry-After`，否则使用指数退避。
HTTP 200 但 `success=false`、`code/rc` 非零、必要字段缺失或行结构漂移都视为失败，
不能转换成“合法空数据”。唯一白名单例外是数据中心明确返回
`code=9201/message=返回数据为空`，该响应与结构完整的空列表等价。

默认参数位于 `config/data_access.json` 的 `providers.eastmoney`：

```json
{
  "minimum_interval_seconds": 1.1,
  "backoff_base_seconds": 0.5,
  "circuit_failure_threshold": 3,
  "circuit_open_seconds": 300,
  "coordination_backend": "shared_file"
}
```

健康状态写入：

```text
$A_STOCK_STATE_HOME/skills/stock-triage/cache/provider_coordination/eastmoney/health.json
```

## 跨机器协调

Hermes 与 OpenClaw 跨机器运行时，`A_STOCK_STATE_HOME` 必须指向同一个共享挂载卷。
该卷必须支持：

- 原子创建目录；
- 同一文件系统内原子重命名；
- 所有节点看到一致的目录和文件更新时间。

限速与熔断锁不依赖 `flock`，而使用原子目录租约并回收超时 owner。若共享存储不满足
上述语义，不要并发运行两套调度器，应由外部调度器保证单写。

## 缓存与决策门禁

`stock_intelligence_v2` 为每个数据集记录独立状态：

- `queried_asof`
- `latest_record_date`
- `status=ok|empty|error`
- 查询与记录各自的最大允许年龄

解禁、两融和股东户数是方向性建议的必要数据。任一必要数据缺失或过期时：

- 质量报告降为 `conditional`；
- Policy 只能输出 `watch`；
- 不写入可结算的买入信号。

成功且完整的缓存会另存为 `market_intelligence/last_good/`。短暂刷新失败时可以回退到
仍在有效期内的最近可信快照；过期快照只能披露，不能恢复方向性建议。当前刷新中已经
识别出的硬风险不会被回退快照覆盖。

## 交叉验证边界

公司公告仍由 CNINFO 独立扫描，澄清、监管、减持和重大风险继续作为更高优先级事实门禁。
东方财富结构化解禁/增减持数据与 CNINFO 公告形成风险层交叉验证。

两融和股东户数当前没有仓内独立的结构化第二数据源。东方财富不可用时系统选择
fail-closed，而不是用旧值、零值或模型推断补齐。新增第二数据源时必须先接入统一
adapter，并保留来源、日期和版本信息。

## 验证

```bash
.venv/bin/ruff check .
pytest -q
python scripts/validate_cron_manifest.py cron/hermes-cron-manifest.json
python scripts/smoke_test.py
```
