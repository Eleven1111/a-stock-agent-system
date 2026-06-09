# 项目代码同步流程

当用户说"GitHub有更新，同步到本地"时，执行以下流程。

## 背景

a-stock-agent-system 的 GitHub 仓库有更新（通常由 Codex 提交的 PR），需同步到本地 Hermes 运行时环境。仓库在 `~/projects/a-stock-agent-system/`，运行时在 `~/.hermes/skills/`。

## 同步步骤

### 1. 拉取 GitHub 更新

```bash
cd ~/projects/a-stock-agent-system
git fetch origin          # GitHub 走 Clash 代理可能超时，设 timeout=120
git log HEAD..origin/main --oneline  # 查看新 commit
git pull origin main --ff-only
```

如果本地有未提交的修改，先 stash：
```bash
git stash push -m "local changes before sync"
git pull origin main --ff-only
# 检查 stash 内容，决定保留还是丢弃
git stash show -p stash@{0}
git stash drop stash@{0}  # 如已被 GitHub commit 覆盖则可丢弃
```

### 2. 同步到 Hermes 运行时目录

仓库代码和运行时目录是**两份拷贝**（非 symlink），必须手动复制：

```bash
# 核心脚本
cp ~/projects/a-stock-agent-system/skills/stock-triage/scripts/*.py \
   ~/.hermes/skills/stock-triage/scripts/

# 共享模块（common/ — 容易遗漏！）
cp -r ~/projects/a-stock-agent-system/skills/common \
   ~/.hermes/skills/common

# 文档
cp ~/projects/a-stock-agent-system/skills/stock-triage/SKILL.md \
   ~/.hermes/skills/stock-triage/SKILL.md
cp ~/projects/a-stock-agent-system/skills/stock-triage/AGENTS.md \
   ~/.hermes/skills/stock-triage/AGENTS.md
```

### 3. 验证兼容性

```bash
~/.hermes/hermes-agent/venv/bin/python3 \
  ~/.hermes/skills/stock-triage/scripts/portfolio_manager.py --check
```

## ⚠️ 坑位速查

### 坑位 1：common/ 模块缺失
新版本 `portfolio_manager.py` 导入 `state_store` 和 `paths`（在 `skills/common/`）。漏了 `common/` 会报 `ModuleNotFoundError: No module named 'state_store'`。

### 坑位 2：trade_history.json 格式
必须是 JSON 列表 `[]`。如果被写成 dict（如 `{"records": []}`），`save_history()` 报 `AttributeError: 'dict' object has no attribute 'append'`。修复：直接写 `[]`。

### 坑位 3：现金对账
旧版 `--add` 不扣现金、`--close` 不加现金。新版自动处理。切换后手工校正 `portfolio.json` 的 `cash`。`ensure_portfolio()` 仅处理 `cash >= total_cost` 的简单场景；复杂场景需手工算（余资 = 记忆中的现金 + 卖出收入 - 买入支出）。
