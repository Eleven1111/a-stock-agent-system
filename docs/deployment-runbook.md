# 部署机运维手册

排查"线上为什么没动静"的固定流程。写下来的原因是：这些判据此前散在
`AUTOPILOT.md`、若干脚本的 docstring 和事后复盘里，每次排查都要重新拼一遍，
而拼错的代价是**基于错误对象做完整套分析**。

本手册只覆盖调度与部署，不覆盖策略语义。

---

## 0. 先确认你在看哪台机器

**判据是账户里有没有真实持仓，不是"有没有调度器在跑"。**

同一套代码会在多个状态根下运行，产物长得几乎一样：

| 角色 | 说明 |
|---|---|
| 部署机 | 生产。真实账户与持仓在这里 |
| 本地实例 | 天天产出候选/研究流水，但 `portfolio.json` 恒为空账户（`account_state: unconfigured`） |
| 历史回退目录 | 更早的废弃状态根，内容早已停更 |

曾因为"看到本机有调度器在跑"就断言"生产就在本机"，结论全错。先跑：

```bash
python -c "import json,os;p=os.path.join(os.environ['A_STOCK_STATE_HOME'],'skills/stock-triage/data/portfolio.json');d=json.load(open(p));print('positions:',len(d.get('positions') or []),'| state:',d.get('account_state'))"
```

有真实持仓 → 部署机。空账户 → 不是生产，别拿它的现象推断线上。

---

## 1. 三类会独立损坏的东西

| | 症状 | 根因类别 |
|---|---|---|
| ① 代码没到位 | 作业在跑，但跑的是旧代码 | dispatcher **直接从工作区运行、不经构建**，生产跑的是工作区当前 checkout 的分支 |
| ② 调度器没在跑 | 完全没有产物 | 调度器停摆是**静默**的 |
| ③ 环境不对 | 作业启动即失败 | `A_STOCK_RUNTIME` 与调度 owner 不匹配。`run_agent_dag.py --runtime` 合法取值是 `hermes` / `openclaw` / `local`，**三者都被支持**；「必须 hermes」只对仍由 Hermes/system cron 驱动的旧部署成立，不是项目全局规则（2026-09-05 核：仓内无任何代码拒绝非 hermes 取值）|

**① 最常发生。** PR 合并进 GitHub main **不等于**进了生产——曾出现两个修复 PR 都已合并、
但工作区停在另一个分支，导致事故的 bug 仍在生产运行。

---

## 2. 诊断序列

按顺序跑，每步的输出决定要不要继续。

### 第 1 步：代码位置

```bash
git rev-parse --abbrev-ref HEAD && git log --oneline -1 && git status --porcelain | head
```

不在 `main` / 落后 origin / 有未提交改动 → 就是根因。同步：

```bash
git pull --ff-only
```

### 第 2 步：spot-check 关键改动确实在文件里

**不要只看 `git log`。** 曾出现 git 历史显示已合并、文件内容却没有的情况。
挑本次修复的一个标志性字符串，直接 grep 文件，并确认它落在**预期的函数体内**：

```bash
grep -n "<本次修复的标志性字符串>" <目标文件路径>
```

### 第 3 步：环境与门禁

```bash
python scripts/state_doctor.py --runtime hermes
```

```bash
.venv/bin/python -m pytest -q
```

用例数应与开发机一致；不一致说明依赖或 Python 版本有分叉。

### 第 4 步：调度健康 —— **必须用对照组**

不要用"你关心的那个作业"判断调度器死活，要看**别的作业**跑没跑：

```bash
python -c "import json,os,collections;p=os.path.join(os.environ['A_STOCK_STATE_HOME'],'cron/output/job_runs.json');d=json.load(open(p));runs=d if isinstance(d,list) else d.get('runs',[]);c=collections.Counter(str(x.get('job_id') or x.get('id')) for x in runs);print('记录总数',len(runs));[print(' ',k,v) for k,v in c.most_common(8)];print('最近三条:');[print(' ',x.get('job_id') or x.get('id'),x.get('started_at') or x.get('finished_at')) for x in runs[-3:]]"
```

把最近几条的时间戳与它们在 `cron/hermes-cron-manifest.json` 里的 `schedule` 逐一对表：

```bash
python -c "import json,sys;d=json.load(open('cron/hermes-cron-manifest.json'));[print(f\"{j['id']:26} {j['schedule']:22} enabled={j['enabled']}\") for j in d['jobs'] if j['id'] in set(sys.argv[1:])]" <作业id> <作业id>
```

分钟级吻合 → 调度器健康，问题不在这一层。

### 第 5 步：具体作业

```bash
ls -la "$A_STOCK_STATE_HOME/cron/output/<作业id>/" 2>&1 | tail -5
```

无目录时**先算时间线**（见下），再下结论。

---

## 3. 判据陷阱

以下每条都被真实踩中过，写下来是为了不再犯。

**① 数据目录没有产物 ≠ 作业没跑。**
作业按设计 skip（如样本不足）时不写业务产物，只写 cron artifact。
判定作业是否执行，看 `cron/output/<作业id>/`，不看它的业务输出目录。

**② 作业没有运行记录 ≠ 调度器坏了。**
先算它**有没有到过执行时点**：新作业进入 manifest 的时间、部署机 pull 到该
提交的时间、以及作业 cron 表达式的下一个时点，三者一起看。
"机制还没到执行时间"不能证明"机制坏了"。

**③ 只看 `git log` 判断修复是否到位 → 用第 2 步的 grep 替代。**

**④ 用当前磁盘上的缓存文件反推历史 → 必错。**
缓存会被后续作业覆写。证据优先级见下。

---

## 4. 证据优先级

```
cron 产物的 cwd / started_at + 文件时间戳
  >  账本（signal_ledger.jsonl，只追加）
  >  当前磁盘上的缓存文件（会被覆写）
```

每个 cron 产物里都有 `cwd` 字段，一眼可验运行的是哪个工作区——比任何代码推理都快。

---

## 5. 兜底：系统 crontab

**仅当第 4 步证明调度器确实不健康时才用。**

```bash
python scripts/generate_system_crontab.py --repo-dir . --state-home "$A_STOCK_STATE_HOME"
```

它只**打印** crontab 行、不安装，绕开 Gateway 的 in-process 调度，产物格式不变。
在调度器健康的情况下改动生产调度方式，风险大于收益。

---

## 6. 变更生效方式

dispatcher 直接从工作区运行，因此：

- `git pull` 在下一次调度心跳即生效，**不需要重启调度器**；
- 反过来，工作区里任何未提交的改动、任何 `git checkout`，都会立即改变生产行为；
- 因此**保持部署机工作区与 main 一致**，不要在上面做实验性修改。

合并后的收尾清单：切回 main → 第 2 步 spot-check → 在生产工作区实跑门禁 →
第 4 步确认调度器在。

---

## 7. 已知但不处理

`scripts/hermes_gateway_doctor.py` 可能报出 `shadowing_detected` /
`source_install_mismatch`（Gateway 从源码 cwd 解析入口、且与已安装版内容漂移）。
它属于 Gateway 那套安装，不是本仓库。

**在它没有可观测症状（第 4 步对照组正常）时不要动**——那是环境级变更，
风险大于收益。等它真的引发调度递归或状态丢失再处理。
