# 部署机任务书：OpenClaw 修复上线核对（2026-09-05）

给部署机执行。目标 commit：`origin/main` @ `002302c`（8 个 PR 已合，CI 3.10/3.13 全绿）。

本文是**这一轮的具体任务清单**，与三份长期文档配套使用：

- [openclaw-registration-reconcile.md](openclaw-registration-reconcile.md) —— 对账流程与五类动作的含义
- [openclaw-canary-and-rollback.md](openclaw-canary-and-rollback.md) —— canary 与逐组件最小回滚
- [openclaw-delivery-status.md](openclaw-delivery-status.md) —— 本轮改了什么、边界在哪

下一轮换 commit 时，把「目标 commit」和阶段 C4 的重算范围更新掉即可，流程本身不变。

## 0. 全程红线

- **阶段 A、B、C 全部只读。** 没跑完 C 并把结果交回来之前，**一条写操作都不做**。
- **不修改仓库里的任何源文件。** 这份任务书要的是核对与取证，不是改代码。
  发现代码有问题 → **写进报告，不要动它**。部署机 dispatcher **直接从工作区运行、
  不经构建**（见 `deployment-runbook.md` §1 ①），所以工作区里一个未提交的改动
  就是一份未经评审、未过 CI 的生产代码。
- **阶段 A 的两个 SHA 是必答项**，不是背景信息：拉取前的 SHA 是唯一的回滚点，
  拉取后的 SHA 决定了后面每一条结论说的是哪一版代码。缺了它们，
  报告里的测试数、产物计数、审计结果都无法归因。
- 不新建、不修改、不删除任何 OpenClaw 作业（除非阶段 D 被明确批准）。
- **不删除任何不属于本项目 `A-stock: ` 前缀的作业**，一条都不删。
- 不重启用户的 OpenClaw、不升级版本、不改全局权限、不改 tool policy。
- 不改真实持仓、不接券商、不发真实订单。首次 canary 一律 `--no-deliver`。
- 不直接读写 OpenClaw 内部 SQLite，只走官方 CLI。
- **含密钥、收件人、持仓的输出只落私有诊断文件，不要贴进聊天、不要提交 Git。**
- 任何一步失败：**停下，把原始输出交回来**，不要自己想办法绕过。

---

## 阶段 A：对齐代码（只读 + 一次 pull）

```bash
cd <部署机仓库 checkout>
git status --short                       # 必须干净；有未提交改动就停下报告
git fetch origin
git log --oneline -1                     # 记下当前 SHA，这是回滚点
```

**把当前 SHA 记进你的报告。** 然后：

```bash
git rev-parse --abbrev-ref HEAD          # 确认在 main
git merge --ff-only origin/main
git log --oneline -1                     # 应为 002302c
```

`--ff-only` 失败（有本地提交或在别的分支）→ **停下报告**，不要 rebase、不要 reset。

依赖与自检：

```bash
python -m pip install -c constraints.txt -e ".[dev]"
python -m pytest -q 2>&1 | tail -3
python -m ruff check .
python scripts/validate_cron_manifest.py
```

---

## 阶段 B：记录宿主事实（只读）

```bash
openclaw --version
openclaw cron --help
openclaw cron list --help
```

**逐字把这三条的完整输出交回来。** 尤其要从 `openclaw cron --help` 里找出：

> **停用一个已安装作业用的是哪个动词？**（`disable` / `edit --enabled false` /
> `pause` / 别的？）

这是本轮唯一一个我们无法在本地核实的东西，仓库里的 reconcile 计划因此把 disable
动作留成了 `command: null` + `command_status: "unverified_cli_verb"`。

再记录环境（**只报路径与是否存在，不要输出任何变量值**）：

```bash
echo "A_STOCK_STATE_HOME=$A_STOCK_STATE_HOME"
echo "A_STOCK_STATE_ID=${A_STOCK_STATE_ID:+(set)}"
echo "A_STOCK_ENV_FILE=${A_STOCK_ENV_FILE:+(set)}"
ls -ld "$A_STOCK_STATE_HOME"
python -c "import sys; print(sys.version)"
which python
```

同时确认：这台机器是不是 Gateway 所在机、是不是实际命令执行机、
有没有还活着的 launchd / system cron / Hermes 任务（`crontab -l`、
`launchctl list | grep -i stock`）。**不要凭历史文档判断，看当前输出。**

---

## 阶段 C：出注册差异（只读，不改任何东西）

### C1 备份当前注册（回滚要用）

```bash
mkdir -p "$HOME/a-stock-diagnostics"
openclaw cron list --json > "$HOME/a-stock-diagnostics/cron-before-$(date +%F).json"
wc -l "$HOME/a-stock-diagnostics/cron-before-$(date +%F).json"
```

这个文件**不要提交 Git、不要贴进聊天**，它可能含收件人。

### C2 生成差异计划

**不需要提供 `A_STOCK_DELIVERY_TO`。** 计划是只读诊断，不该要生产密钥：
16 个 `deliver: origin` 的作业会单独记 `blocked` / `delivery_target_missing`，
另外 48 个照常给出判定（PR #352）。缺收件人时生成的计划里没有任何 `--to`，
反而更适合贴出来看。

```bash
python scripts/generate_openclaw_cron.py --plan --state-home "$A_STOCK_STATE_HOME" \
  > "$HOME/a-stock-diagnostics/reconcile-plan.json"
python - <<'PY'
import json
p = json.load(open(__import__('os').path.expanduser("~/a-stock-diagnostics/reconcile-plan.json")))
print("summary:", p["summary"])
print("applicable:", p["applicable"])
print("orphans:", p["orphaned_managed_jobs"])
for a in p["actions"]:
    if a["action"] not in ("unchanged", "skipped"):
        print(a["action"], a["logical_id"], a.get("reason"),
              "drift=", a.get("drifted_fields"), "unverifiable=", a.get("unverifiable_fields"))
PY
```

**把上面这段的输出交回来**（是脱敏摘要，不含命令本身，可以贴）。

预期会看到：13 个 manifest 已 disabled 的作业里，凡是宿主上还装着的都出现
`disable` 动作、`command: null`、`applicable: false`。这正是本轮要修的东西。

### C3 双运行时审计

```bash
python scripts/dual_runtime_audit.py > "$HOME/a-stock-diagnostics/audit.json"
python -c "
import json,os;r=json.load(open(os.path.expanduser('~/a-stock-diagnostics/audit.json')))
print('status:',r['status'],'clean:',r['clean'])
print('registration:',{k:v for k,v in r['openclaw_registration'].items() if k!='reason'})
print('duplicate_runs:',len(r['concurrent_duplicate_runs']),'active_leases:',len(r['active_leases']))
print('state_identity:',r['state_identity'].get('status'))
"
```

**把这段输出交回来。** 重点看三个字段：

- `missing_from_openclaw` —— manifest 启用但宿主没装
- `disabled_but_installed` —— **manifest 关掉了但宿主还在跑**（本轮新增的检测）
- `duplicate_managed_names` —— 同名装了多份

### C4 历史结论重算清单（只读）

统计套件已升到 `statistical-validation-suite-v2`，v1 产物**刻意不再通过校验**，
需要重算才能重新支撑准入。先查部署机上有没有 v1 产物：

```bash
grep -rl "statistical-validation-suite-v1" "$A_STOCK_STATE_HOME" 2>/dev/null | head -50
grep -rl "statistical-validation-suite-v1" "$A_STOCK_STATE_HOME" 2>/dev/null | wc -l
```

**只报数量和路径，不要动它们。** 详见 `docs/statistical-method-migration.md`。

### C5 宿主模型评测（只读，不外发）

```bash
python scripts/evaluate_openclaw_host.py
```

现在应该从 `not_run / openclaw_binary_not_found` 变成
`not_run / no_observations_supplied`——那说明宿主被识别到了。
**把 `host.version` 那一段交回来。** 真实模型回合的观测采集另行安排，本轮不做。

---

## 到这里停。把以下内容交回来：

0. `git status --short` 的输出（应为空）。**非空就先停下**——
   工作区有未提交改动时，后面所有测试与审计结果说的都不是目标 commit 那一版。
1. 阶段 A 的**旧 SHA**（回滚点）和 pull 后的 SHA、四道自检的输出尾部。
   pull 后的 SHA 必须与任务书开头写的目标 commit 一致；不一致就报告差在哪，不要自行修补。
2. `openclaw --version` / `cron --help` / `cron list --help` 的**完整输出**。
3. **停用动词是哪个**（阶段 B 的核心问题）。
4. 这台机器的角色（Gateway / 执行机 / 都是）+ 有没有活着的 launchd / cron / Hermes。
5. C2 的计划摘要、C3 的审计摘要。
6. C4 的 v1 产物数量。
7. C5 的 `host.version`。

---

## 阶段 D：只有拿到批准后才执行

拿到停用动词后，**先再出一次计划**确认它能解析：

```bash
python scripts/generate_openclaw_cron.py --plan \
  --state-home "$A_STOCK_STATE_HOME" \
  --disable-command-template '<把阶段 B 查到的真实动词填进来，形如 {openclaw} cron disable {job_id}>' \
  | python -c "import json,sys;p=json.load(sys.stdin);print(p['summary'],p['applicable'])"
```

`applicable` 必须是 `true` 才能往下走。然后**再等一次明确批准**，才执行：

```bash
python scripts/generate_openclaw_cron.py --reconcile --apply \
  --state-home "$A_STOCK_STATE_HOME" \
  --disable-command-template '<同上>'
```

应用后**立刻**重跑一次 `--plan`：第二次必须全部落在 `unchanged`、零条命令。
不是的话停下报告。

### canary

优先挑一个**确定性只读作业**（如 `sector-crowding-daily`），手动触发一次，确认：

- 产物按时落盘、内容通过契约
- 没有重复成交、没有残留租约（重跑 C3 审计，`clean` 应为 true）
- 首次一律 `--no-deliver`，**真实发送只在明确授权且收件人已核实后才开**

---

## 回滚

三样东西已在阶段 A/C1 备好：旧 Git SHA、`cron-before-*.json`、状态配置。

```bash
git -C <checkout> checkout <旧 SHA>            # 代码回滚
# 作业回滚：用官方 CLI 按 cron-before-*.json 里的参数 cron edit 回去
```

**绝对不做**：不恢复覆盖真实持仓的旧账户快照、不重放已成交任务、
不删除任何追加日志（signal ledger / 事件账本 / retention hold 账本都是 append-only）。

算法层面出问题不需要回滚整个 checkout，最小动作是**停新实验 / 停其晋级消费，
旧证据继续只读**。逐组件的最小回滚动作见 `docs/openclaw-canary-and-rollback.md`。
