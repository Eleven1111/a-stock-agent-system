# 数据源迁移报告：Eastmoney push2 降级改造

日期：2026-07-09

## 背景

`push2.eastmoney.com` / `push2his.eastmoney.com` 的以下路径已出现路径级 WAF/断连：

- `/api/qt/stock/kline/get`
- `/api/qt/clist/get`
- `/api/qt/stock/get`
- `/api/qt/stock/fflow/daykline/get`

本次改造将这些端点从主路径移到 degraded last-resort，仅在 AkShare、adata、datacenter-web 或腾讯兜底均不可用时探测。

## 新数据源链路

| 数据路径 | 新优先级 | 说明 |
| --- | --- | --- |
| 全 A 实时行情 | AkShare `stock_zh_a_spot`(Sina) -> adata -> Eastmoney datacenter-web -> push2 degraded | 本地缓存 5 分钟，避免全量 5000+ 股票重复拉取 |
| 个股行情 | 全 A 缓存过滤 -> 腾讯 quote | 不再调用 `/api/qt/stock/get` |
| 日 K | AkShare `stock_zh_a_hist_tx`(Tencent) -> adata -> 腾讯直连 -> push2his degraded | 单票缓存 12 小时；候选发现改走此统一入口 |
| 分钟 K | 腾讯 minute/query | 原稳定链路保留 |
| 个股资金流 | AkShare 东财封装 -> AkShare THS 个股资金流 -> adata -> push2his degraded | 东财封装失败时使用 THS 资金流排名 |
| 板块资金/行情 | AkShare THS 板块摘要 -> adata -> push2 degraded | `news-to-sector` 和行业映射不再直连 push2 clist |
| 北向资金 | AkShare 北向汇总 -> Eastmoney `kamt.kline` | `kamt.kline` 当前仍可用，作为明确允许的后备 |
| 龙虎榜 | Eastmoney datacenter-web | 不依赖 push2 |

## 核心改动

- `skills/common/market_adapters.py`
  - 新增统一 fallback、缓存、provider health 记录。
  - 新增 `fetch_a_share_spot`、`fetch_a_share_quote`、`fetch_a_share_daily_kline`、`fetch_stock_fund_flow`、`fetch_sector_fund_flow`、`fetch_board_quotes`、`fetch_northbound_flow`。
  - `fetch_eastmoney_kline` 保持兼容函数名，但内部改为多源链路。
- `skills/common/http_client.py`
  - NO_PROXY 扩展到 `.gtimg.cn`、`.sinajs.cn`、`.10jqka.com.cn`、`.hexun.com`，覆盖 AkShare 走到的国内行情域名。
- `skills/common/data_access_config.py`、`config/data_access.json`
  - 新增 `akshare`、`adata`、`eastmoney_datacenter`、`eastmoney_push2_degraded` provider 节点。
  - `field_chains` 改为多源链路声明。
- `skills/stock-triage/scripts/candidate_discovery.py`
  - 候选 K 线改走 `fetch_a_share_daily_kline`。
- `skills/stock-triage/scripts/capital_flow_monitor.py`
  - 个股/板块资金流改走统一适配器。
  - 板块代码缓存改由 resilient board adapter 填充。
- `skills/common/industry_map.py`
  - 行业板块清单默认不再直连 push2 clist。
- `skills/news-to-sector/scripts/main.py`
  - 板块行情改走 `fetch_board_quotes`。
- `skills/a-stock-daily-report/scripts/a-stock-report.js`
  - Node 日报脚本改为调用 Python `market_adapters`，不再直接请求 push2。
- `scripts/provider_doctor.py`
  - 探针改为验证多源链路，而不是直接探测封禁路径。
- `scripts/datasource_fallback_smoke.py`
  - 新增完整 live smoke，覆盖实时、K 线、资金流、龙虎榜、板块、北向和并发 K 线。

## Live 验证结果

命令：

```bash
.venv/bin/python3 scripts/datasource_fallback_smoke.py --workers 3 --json
```

结果：`status=ok`

| Probe | 结果 | 行数/说明 |
| --- | --- | --- |
| a_share_spot | ok | 5528 |
| single_quote | ok | 1 |
| daily_kline | ok | 10 |
| minute_kline | ok | 267 |
| stock_fund_flow | ok | 1 |
| sector_fund_flow | ok | 1 |
| dragon_tiger | ok | 1 |
| board_quotes | ok | 90 |
| northbound_flow | ok | 1 |
| concurrent_daily_kline | ok | 5/5 |

冷启动全 A Sina 行情约 22 秒；命中本地缓存后约 0.6 秒。

命令：

```bash
.venv/bin/python3 scripts/provider_doctor.py --json
```

结果：`status=ok`。关键数据集 `tencent_quote`、`stock_fund_flow`、`daily_kline`、`board_quotes`、`northbound_flow`、`dragon_tiger`、`a_share_spot` 全部通过。

## 自动化检查

已通过：

```bash
.venv/bin/python3 -m py_compile skills/common/market_adapters.py skills/stock-triage/scripts/capital_flow_monitor.py skills/stock-triage/scripts/candidate_discovery.py scripts/provider_doctor.py scripts/datasource_fallback_smoke.py
node --check skills/a-stock-daily-report/scripts/a-stock-report.js
git diff --check
```

未能运行：

```bash
.venv/bin/python3 -m pytest -q tests/test_market_adapters_resilient.py tests/test_industry_map.py tests/test_business_http_client_migrations.py tests/test_data_access_config.py tests/test_provider_doctor.py
```

原因：当前 `.venv` 缺少 `pytest`（`No module named pytest`）。

已用系统 Python 补充运行：

```bash
python3 -m pytest -q tests/test_market_adapters_resilient.py tests/test_industry_map.py tests/test_business_http_client_migrations.py tests/test_data_access_config.py tests/test_provider_doctor.py
```

结果：`38 passed in 2.28s`

未能运行：

```bash
.venv/bin/python3 -m ruff check skills/common/market_adapters.py skills/stock-triage/scripts/capital_flow_monitor.py skills/stock-triage/scripts/candidate_discovery.py scripts/provider_doctor.py scripts/datasource_fallback_smoke.py tests/test_market_adapters_resilient.py
```

原因：当前 `.venv` 缺少 `ruff`（`No module named ruff`）。

另外运行：

```bash
.venv/bin/python3 scripts/validate_cron_manifest.py
```

结果：失败在既有 `job[43] (candidate-freshness-check)` manifest 缺字段/未通过 `run_agent_dag.py` 路由；本次未修改 cron manifest。

## 剩余风险

- AkShare/THS 免费源仍可能限频；当前用缓存与分批/并发边界降低重复请求。
- adata 在本机实测存在慢响应/空结果，因此作为二级后备，并由 circuit breaker 记录健康状态。
- push2 degraded 端点仍保留代码以便未来恢复探测，但不会作为主源影响实时链路。
