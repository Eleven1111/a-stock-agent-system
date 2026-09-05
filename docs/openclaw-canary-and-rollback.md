# 上线：canary 与回滚

**PR 合并 ≠ 部署完成。** 部署机 dispatcher 直接从工作区运行、不经构建，
main 上的修复不会自己走到生产（见 `deployment-runbook.md` §1 ①）。

## 顺序

```
分支完成 → 必需 CI 全绿 → 按仓库规则开 PR → 有授权时合并
→ 部署前核对 checkout 与注册差异 → 最小 canary → 观察
```

### 1. 核对部署机 checkout

```bash
git -C <deploy-checkout> log --oneline -3
git -C <deploy-checkout> status --short
```

跑的是哪个分支的哪个 commit，用眼睛确认，不从「PR 已合并」推断。

### 2. 核对注册差异（只读）

```bash
openclaw --version
openclaw cron --help          # 读出这个版本真实支持的动词
python scripts/generate_openclaw_cron.py --plan --state-home "$A_STOCK_STATE_HOME"
python scripts/dual_runtime_audit.py
```

`--plan` 的 `applicable: false` 或审计的 `disabled_but_installed` /
`duplicate_managed_names` 非空 → **先解决差异再谈 canary**。

### 3. 最小 canary

优先用**已有的确定性只读或隔离任务**，不要拿会外发的任务做第一次验证。

- 真实消息发送**仅在已有授权且目标已核实**时使用；否则一律 `--no-deliver`。
- 检查 Gateway 是否在跑，但**不擅自重启用户整个 OpenClaw、不升级版本、
  不启用全局权限**。
- 观察窗口内确认：产物按时落盘、内容通过契约、没有重复成交、
  超时被杀时子进程树一起退出。

### 4. 观察指标

`scripts/dual_runtime_audit.py` 的 `status` 应为 `ok`；
`concurrent_duplicate_runs` 与 `active_leases` 应为空。

## 回滚

**先留后路，再动手。**

保留三样：

1. 上一个 Git SHA（`git -C <deploy-checkout> rev-parse HEAD` 记下来）。
2. 受管作业的**原始注册参数**：`openclaw cron list --json > <私有诊断目录>/cron-before.json`。
3. 状态配置备份（`A_STOCK_STATE_HOME` 下的 `state_identity.json` 等）。

回滚动作：

- 通过**正式 CLI** 恢复本项目作业（`cron edit` 回原参数）。不直接改 OpenClaw 的内部存储。
- **不恢复覆盖真实持仓的旧账户快照。**
- **不重放已成交的任务。**
- **不删除追加日志**（signal ledger、事件账本、retention hold 账本都是 append-only）。

算法新版本出错时的最小动作：**停新实验 / 停它的晋级消费，旧证据继续只读**。
不需要回滚整个 checkout。

具体到本轮：

| 出问题的东西 | 最小回滚 |
|---|---|
| 统计 v2 | 停止用 v2 产物做准入判断；v1 产物本来就只读保留，不需要动 |
| 探索性实验 | `release_hold` + 停跑该实验；`strategy_registry` 未被改过，真实晋级不受影响 |
| 板块归档 | 停归档；`*_latest.json` 行为与改动前一致 |
| retention hold | `release_hold(scope)`；GC 回到原有的 recency-only 保护 |
| reconcile plan | 计划是只读产物；未 `--apply` 就没有任何宿主副作用 |

## 本轮实际状态

**未部署。** 本机没有 openclaw、没有部署机访问权，上述每一步都**没有在真实宿主上执行过**。
状态标签是 `engineering_verified`，不是 `deployment_verified`。
