# 项目代码同步流程

当用户说"GitHub有更新，同步到本地"时，执行以下流程。

## 背景

a-stock-agent-system 的 GitHub 仓库有更新（通常由 Codex 提交的 PR 或项目更新），需同步到本地 Hermes 运行时环境。仓库在 `~/meta-11/a-stock-agent-system/`，运行时在 `~/.hermes/skills/`。

## ⚠️ 第一铁律：永远不要使用 `--delete`

repo 不包含 `data/` 运行时目录，`--delete` 会永久删除 portfolio.json、trade_history.json 等关键持仓数据。
**同步命令中绝对不给 `--delete`，且必须加 `--exclude='data/'`。**

已在坑位 4 中详细记录，但这里再强调一次——**这条铁律违反过一次，后果是仓位数据丢失，用户暴怒。**

## 同步步骤

### 1. 拉取 GitHub 更新

```bash
cd ~/meta-11/a-stock-agent-system
git fetch origin          # GitHub 走 Clash 代理可能超时，设 timeout=120
git log HEAD..origin/main --oneline  # 查看新 commit
git pull origin main --ff-only
```

**SSL 超时降级：** 如果 `git pull` 报 `SSL_ERROR_SYSCALL`（Clash 代理问题），改用 `gh repo clone`（gh CLI 走不同认证链路，通常可用）：

```bash
# gh 走 OAuth 认证，不经过 git HTTP 代理
gh repo clone Eleven1111/a-stock-agent-system /tmp/a-stock-agent-system-fresh
# 然后从临时目录复制回原位置
cp -a /tmp/a-stock-agent-system-fresh/* ~/meta-11/a-stock-agent-system/
rm -rf /tmp/a-stock-agent-system-fresh
```

或者也可以设置 git 使用 HTTP/1.1 解决 HTTP/2 代理卡死：
```bash
git config --global http.version HTTP/1.1
```

如果本地有未提交的修改，先 stash：
```bash
git stash push -m "local changes before sync"
git pull origin main --ff-only
# 检查 stash 内容，决定保留还是丢弃
git stash show -p stash@{0}
git stash drop stash@{0}  # 如已被 GitHub commit 覆盖则可丢弃
```

### 2. 同步到 Hermes 运行时目录（推荐：rsync）

仓库代码和运行时目录是**两份拷贝**（非 symlink），用 rsync 可以**一次性**同步所有 11 个 skill 及 common 模块：

```bash
REPO="~/meta-11/a-stock-agent-system/skills"
HERMES="~/.hermes/skills"

for s in stock-triage stock-analyst hot-money-tactics global-market-monitor \
         news-to-sector serenity-investment-research daban-stock-picker \
         chanlun-backtest a-stock-data a-stock-daily-report a-stock-commands; do
    rsync -av \
        --exclude='__pycache__/' \
        --exclude='data/' \
        --exclude='*.pyc' \
        "$REPO/$s/" "$HERMES/$s/"
done

# 共享模块（common/ — 容易遗漏！）
cp ~/meta-11/a-stock-agent-system/skills/common/*.py ~/.hermes/skills/common/

# 配置文件
cp ~/meta-11/a-stock-agent-system/config/scoring.yaml ~/.hermes/skills/stock-triage/config.yaml
```

**重要：不要使用 `--delete` 参数！** 因为仓库不包含运行时 `data/` 目录，`--delete` 会删掉本地的 portfolio.json、signal_history.json、intraday_alerts.json 等运行时数据文件。如果误删了，需要重建空目录：

```bash
mkdir -p ~/.hermes/skills/stock-triage/data
mkdir -p ~/.hermes/skills/daban-stock-picker/data
```

### 3. 验证兼容性

```bash
cd ~/meta-11/a-stock-agent-system
python3 skills/stock-triage/scripts/portfolio_manager.py --check
```

或者针对特定 skill 做测试：
```bash
python3 -c "from skills.common.paths import hermes_home; print(hermes_home())"
python3 skills/stock-analyst/analyst.py --help
```

## ⚠️ 坑位速查

### 坑位 1：common/ 模块缺失
新版本脚本导入 `state_store`、`paths`、`a_stock_http` 等（在 `skills/common/`）。漏了 `common/` 会报 `ModuleNotFoundError: No module named 'state_store'`。检查：
```bash
ls ~/.hermes/skills/common/*.py  # 应有 6 个文件
```

### 坑位 2：trade_history.json 格式
必须是 JSON 列表 `[]`。如果被写成 dict（如 `{"records": []}`），`save_history()` 报 `AttributeError: 'dict' object has no attribute 'append'`。修复：直接写 `[]`。

### 坑位 3：现金对账
旧版 `--add` 不扣现金、`--close` 不加现金。新版自动处理。切换后手工校正 `portfolio.json` 的 `cash`。`ensure_portfolio()` 仅处理 `cash >= total_cost` 的简单场景；复杂场景需手工算（余资 = 记忆中的现金 + 卖出收入 - 买入支出）。

### 坑位 4：rsync --delete 误删 data 目录
不要在 rsync 中使用 `--delete`，因为仓库没有 `data/` 目录，会导致本地运行时数据 **永久丢失**。这是本次事故的教训（2026-06-09，误删 portfolio.json 导致用户仓位记录丢失）。

**如果已经误删，按以下顺序恢复：**

1. **重建空 data 目录**（见第 2 步末尾的命令）
2. **检查旧备份**：在其他项目目录下找 `find ~/projects -name "portfolio.json" 2>/dev/null`
3. **搜索会话历史**：用 `session_search(query="portfolio_manager --add 买入 开仓")` 恢复最近买入记录
4. **检查 cron output artifacts**：`grep -rl "portfolio" ~/.hermes/cron/output/ | head -5`
5. **写一份干净的初始文件**：空仓时写 `{"cash": <已知现金>, "positions": [], "total_cost": 0.0, "cash_reconciled": true}`
6. **请用户重新口述持仓**：这是最终恢复手段，让用户重新报一次所有持仓

data/ 目录下的关键文件：
- `portfolio.json` — 持仓+现金+风控（必须手动恢复，无法自动生成）
- `signal_history.json` — 历史信号记录（performance_tracker.py 自动重建）
- `intraday_alerts.json` — 盘中告警去重缓存（intraday_monitor.py 自动重建）
- `trade_history.json` — 交易历史（portfolio_manager.py --add 时自动创建）
- `cash_flow.json` — 资金流水（portfolio_manager.py --deposit/--add 时自动创建）
- `recommendations.json` — 推荐审计档案（recommendation_audit.py 自动重建）

### 坑位 5：新增文件检查
rsync 默认不会复制仓库新增的文件到目标端——它会按源端更新已有文件，但新增文件需要 rsync 才能到达。用 `diff -rq` 验证：
```bash
diff -rq ~/meta-11/a-stock-agent-system/skills/stock-triage ~/.hermes/skills/stock-triage \
  | grep -v __pycache__ | grep -v 'Only in.*data/'
```
如果看到 `Only in .../a-stock-agent-system/...` 的行，说明有新增文件未同步。用 `rsync -av`（不带 --delete）即可补上。
