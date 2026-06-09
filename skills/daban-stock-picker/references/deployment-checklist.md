# 早盘流水线 Cron 部署检查清单

## 场景

你刚写好一个新 workflow（如竞价收口、开盘确认），需要注册为 cron job。逐项确认：

## 步骤

### 1. 脚本已存在且可执行
```bash
ls ~/.hermes/skills/<skill>/scripts/<script>.py
```

### 2. 创建 cron job
```bash
hermes cron create \
  --name "任务中文名" \
  --schedule "25 9 * * 1-5" \
  --skill <skill-a>,<skill-b> \
  --model deepseek-v4-flash --provider deepseek \
  --deliver "origin"
```

### 3. 关键参数
- **`schedule`**: 用 cron 表达式。交易日的用 `1-5`，避开整点（如 `25 9` 而非 `0 9`）
- **`repeat`**: 一次性任务用 `once`，重复任务用默认 `forever`
- **`skills`**: 必须包含 workflow 运行所需的所有 skill（缺了会工具不可用）
- **`model/provider`**: 如果用户有计费分离要求（如 OpenRouter vs DeepSeek 分开计费），必须显式指定对应 provider

### 4. 验证运行
```bash
hermes cron list          # 确认任务在列表中
hermes cron run <job_id>  # 手动触发一次测试（仅调试，不代表定时执行成功）
```

## 曾犯错误

| 日期 | 错误 | 修复 |
|------|------|------|
| 2026-06-05 | 竞价收口(09:25) + 开盘确认(09:35) 流程规划完整、脚本就绪，但未注册 cron job | 补注册 `5e20c1391483` + `65f8ef959f20` |
