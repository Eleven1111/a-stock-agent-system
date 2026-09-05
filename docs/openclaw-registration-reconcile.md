# OpenClaw 注册对账（reconcile）

对象：把 `cron/hermes-cron-manifest.json` 这份**项目任务声明**与部署机上
**实际安装的 OpenClaw 作业**对齐。生成器只管理带 `A-stock: ` 前缀的条目，
其他应用的任务对它不可见。

## 先读安装版的能力，别照抄文档

```bash
openclaw --version
openclaw cron --help
openclaw cron list --help
```

本仓库已核实使用的动词只有 `cron list --json` / `cron create` / `cron edit`。
**停用（disable）用哪个动词未经核实**——见下。

## 出差异计划（只读）

```bash
python scripts/generate_openclaw_cron.py --plan --state-home "$A_STOCK_STATE_HOME"
```

输出 `openclaw_reconcile_plan_v1`，每个逻辑作业一条动作：

| action | 含义 | 是否产出命令 |
|---|---|---|
| `create` | manifest 启用、宿主没有 | 是（`cron create`） |
| `update` | 宿主有，但参数漂移 | 是（`cron edit`），并在 `drifted_fields` 逐字段列出 |
| `unchanged` | 宿主参数与期望一致 | 否 |
| `disable` | **manifest 已关闭、宿主仍装着** | 见下 |
| `skipped` | manifest 已关闭、宿主也没有 | 否 |
| `conflict` | 同一个受管名字装了多份 | 否，人工处理 |

另外两个字段：

- `orphaned_managed_jobs`：带受管前缀、但逻辑 ID 已完全离开 manifest 的作业。
  **只报告，永不生成删除命令。**拥有一个名字不等于被授权删掉它。
- `unverifiable_fields`：宿主的 JSON 没有报告的字段。这些字段记 `unknown`，
  **不记 drift**——审计不能声称一个它观测不到的差异。

## disable 动词未核实：刻意留空

一个在 manifest 里被关掉、但宿主上仍装着的作业**会继续按点触发**。旧版生成器
对这种情况一条命令都不产出（2026-09-05 离线复现：13 个 disabled 作业中拿
`tail-close-decision` 造 installed fixture，生成 64 条命令、零条提及它）。

现在计划里会出现 `disable` 动作，但默认 `command: null`、
`command_status: "unverified_cli_verb"`，且整个计划 `applicable: false`。
在部署机上从 `openclaw cron --help` 读到真实动词后再传入：

```bash
python scripts/generate_openclaw_cron.py --plan \
  --state-home "$A_STOCK_STATE_HOME" \
  --disable-command-template '{openclaw} cron disable {job_id}'
```

（或用环境变量 `A_STOCK_OPENCLAW_DISABLE_TEMPLATE`。）
未确认动词就编一个出来，等于把「停用」变成一次静默失败。

## 应用差异

```bash
python scripts/generate_openclaw_cron.py --reconcile --apply --state-home "$A_STOCK_STATE_HOME"
```

`--apply` 必须配 `--reconcile`（否则会重复 create）。同一份计划重复应用不产生新 ID：
第二次 reconcile 全部落在 `unchanged`，零条命令。

## 审计侧

```bash
python scripts/dual_runtime_audit.py
```

`openclaw_registration` 段现在区分三类：

- `missing_from_openclaw`：manifest 启用但没装。
- `disabled_but_installed`：manifest 关闭但仍装着（**新增**；以前会被误报成 orphan）。
- `orphaned_in_openclaw`：manifest 里彻底没有这个 ID。

审计的 manifest 侧口径已与生成器对齐：**启用即计入，不再按 `deliver != "silent"` 过滤**。
生成器把 `local` / `silent` / `feishu_direct` 一律映射成 `--no-deliver` 并照常安装，
所以旧口径会在出现第一个 enabled silent 作业时把它误报成 orphan。当前 manifest 没有
enabled silent 作业，该差异是潜伏的，已由 fixture 守住。

## 收件人与密钥

生产收件人从 `A_STOCK_DELIVERY_TO` / 私有配置读取，密钥由子 runner 从
`A_STOCK_ENV_FILE` 加载。**任何值都不会被序列化进 cron 命令、dry-run 输出、
进程参数或 OpenClaw 作业库。**导出器的默认渠道（`discord`）是占位默认值，
不是用户的实际渠道——不要拿它当已配置的目标。
