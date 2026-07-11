#!/usr/bin/env node
/**
 * A股日报自动生成器（Node.js版本）
 * 功能：抓取多源板块数据，生成日报
 */

const path = require('path');
const { execFileSync } = require('child_process');
const ROOT = path.resolve(__dirname, '..', '..', '..');
const PYTHON = path.join(ROOT, '.venv', 'bin', 'python3');
const COMMON = path.join(ROOT, 'skills', 'common');

/**
 * Python 数据适配器调用。日报脚本不再直连 Eastmoney push2 被封路径。
 */
function pythonJson(code) {
  const output = execFileSync(PYTHON, ['-c', code], {
    cwd: ROOT,
    env: { ...process.env, PYTHONPATH: `${COMMON}:${process.env.PYTHONPATH || ''}` },
    timeout: 30000,
    encoding: 'utf8',
  });
  return JSON.parse(output);
}

/**
 * 获取板块数据
 */
async function fetchBoardData() {
  try {
    return pythonJson(`
import json, sys
sys.path.insert(0, ${JSON.stringify(COMMON)})
from market_adapters import fetch_board_quotes
rows = []
for item in fetch_board_quotes():
    change = item.get("f3")
    change = 0 if change is None else float(change)
    rows.append({
        "code": str(item.get("f12") or ""),
        "name": str(item.get("f14") or ""),
        "change": ("+" if change > 0 else "") + f"{change:.2f}%",
        "amount": item.get("f6") or item.get("f62") or 0,
    })
print(json.dumps(rows, ensure_ascii=False))
`);
  } catch (e) {
    console.error(`[ERROR] 获取板块数据失败: ${e.message}`);
    return [];
  }
}

/**
 * 获取大盘指数数据
 */
async function fetchIndexData() {
  try {
    return pythonJson(`
import json, sys
sys.path.insert(0, ${JSON.stringify(COMMON)})
from market_adapters import fetch_tencent_index_overview
mapping = {"上证综指": "sh_index", "深证成指": "sz_index", "创业板指": "cy_index"}
result = {"failed": [], "success": []}
for row in fetch_tencent_index_overview().to_dict("records"):
    key = mapping.get(row.get("名称"))
    if not key:
        continue
    result[key] = f"{float(row.get('最新价') or 0):.2f}"
    result[f"{key}_change"] = f"{float(row.get('涨跌幅') or 0):.2f}%"
    result["success"].append(row.get("名称"))
print(json.dumps(result, ensure_ascii=False))
`);
  } catch (e) {
    console.error(`[ERROR] 获取指数数据失败: ${e.message}`);
    return { failed: ['指数'], success: [], sh_index: '--', sh_index_change: '--' };
  }
}

/**
 * 分析数据并构建报告数据
 */
function analyzeAndBuildReportData(boards, indices) {
  const boardFailed = !boards || boards.length === 0;

  // 热门板块（涨幅前5）
  let hotBoards = [];
  let focusBoards = [];
  let riskBoards = [];

  if (!boardFailed) {
    // 按涨幅排序
    const getChangeVal = (b) => {
      try {
        return parseFloat(b.change.replace('%', '').replace('+', '')) || 0;
      } catch {
        return 0;
      }
    };

    const sortedBoards = [...boards].sort((a, b) => getChangeVal(b) - getChangeVal(a));

    hotBoards = sortedBoards.slice(0, 5).map((b) => ({
      name: b.name,
      change: b.change,
      leader: '--',
      reason: '当日涨幅靠前（不代表资金流入）',
    }));

    // 明日关注板块
    focusBoards = sortedBoards.slice(2, 5).map((b) => ({
      name: b.name,
      reason: '当日涨幅排名靠前',
      technical: '持续性未验证',
      suggestion: '仅作研究观察，需通过完整政策检查',
    }));

    // 风险板块（跌幅前3）
    riskBoards = sortedBoards.slice(-3).reverse().map((b) => ({
      name: b.name,
      reason: '当日跌幅靠前（不代表资金流出）',
      suggestion: '仅作风险观察',
    }));
  }

  // 判断市场情绪
  let sentiment = '中性';
  try {
    const shChange = parseFloat((indices.sh_index_change || '0%').replace('%', ''));
    sentiment = shChange > 0 ? '偏多' : '偏空';
  } catch {
    sentiment = '中性';
  }

  // 构建完整数据
  const data = {
    ...indices,
    market_sentiment: sentiment,
    hot_boards: hotBoards,
    focus_boards: focusBoards,
    risk_boards: riskBoards,
    north_money: '--',
    main_inflow: '--',
    margin_balance: '--',
    board_failed: boardFailed,
    index_failed: indices.failed || [],
    index_success: indices.success || [],
    strategy: `1. **仓位控制**：仓位由组合风险政策决定，本日报不提供固定仓位
2. **板块数据**：仅表示当日涨跌幅排名，不代表资金流向或持续趋势
3. **方向建议**：本日报未执行公告、数据质量、可交易性、价格计划和组合风险全链检查
4. **使用边界**：仅作研究摘要，不构成交易建议`,
  };

  return data;
}

/**
 * 生成Markdown报告
 */
function generateReport(boardData) {
  const today = new Date().toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).replace(/\//g, '年').replace('年', '年').replace(/年(\d{2})$/, '月$1日');

  let report = `# 📊 A股市场日报
**${today}**

---

## 🎯 大盘概览

| 指数 | 收盘点位 | 涨跌幅 |
|------|---------|--------|
| 上证指数 | ${boardData.sh_index || '--'} | ${boardData.sh_index_change || '--'} |
| 深证成指 | ${boardData.sz_index || '--'} | ${boardData.sz_index_change || '--'} |
| 创业板指 | ${boardData.cy_index || '--'} | ${boardData.cy_index_change || '--'} |
| 科创板指 | ${boardData.kc_index || '--'} | ${boardData.kc_index_change || '--'} |

**市场情绪**: ${boardData.market_sentiment || '中性'}
`;

  // 添加数据获取失败提示
  const warnings = [];
  if (boardData.index_failed && boardData.index_failed.length > 0) {
    warnings.push(`指数数据 - ${boardData.index_failed.join('、')}`);
  }
  if (boardData.board_failed) {
    warnings.push(`板块数据`);
  }
  if (warnings.length > 0) {
    report += `\n⚠️ **数据获取提示**: 以下数据获取失败 (${warnings.join('；')})，可能原因：非交易时间/网络异常/API 暂时不可用\n`;
  }

  report += `
---

## 🔥 热门板块 TOP 5

| 排名 | 板块名称 | 涨跌幅 | 领涨股 |
|------|---------|--------|--------|
`;

  const hotBoards = boardData.hot_boards || [];
  hotBoards.slice(0, 5).forEach((board, i) => {
    report += `| ${i + 1} | ${board.name} | ${board.change} | ${board.leader} |\n`;
  });

  report += `---

## 📈 明日关注

| 板块名称 | 关注理由 | 技术面 | 操作建议 |
|---------|---------|--------|---------|
`;

  const focusBoards = boardData.focus_boards || [];
  focusBoards.forEach((board) => {
    report += `| ${board.name} | ${board.reason} | ${board.technical} | ${board.suggestion} |\n`;
  });

  report += `---

## ⚠️ 风险提示

| 板块名称 | 风险理由 | 建议 |
|---------|---------|------|
`;

  const riskBoards = boardData.risk_boards || [];
  riskBoards.forEach((board) => {
    report += `| ${board.name} | ${board.reason} | ${board.suggestion} |\n`;
  });

  report += `---

## 💰 资金动向

`;

  if (hotBoards.length > 0) {
    const hotBoardsNames = hotBoards.slice(0, 3).map((b) => b.name).join('、');
    report += `- **主力流入方向**: ${hotBoardsNames}\n`;
  } else {
    report += `- **主力流入方向**: --\n`;
  }

  report += `- **北向资金**: ${boardData.north_money || '--'}
- **融资余额**: ${boardData.margin_balance || '--'}

---

## 📝 操作策略

${boardData.strategy || '1. **仓位控制**：仓位由组合风险政策决定，本日报不提供固定仓位\n2. **板块数据**：仅表示当日涨跌幅排名，不代表资金流向或持续趋势\n3. **方向建议**：本日报未执行完整政策检查\n4. **使用边界**：仅作研究摘要，不构成交易建议'}

---

**数据来源**: 项目数据适配器（具体 provider 见运行 artifact）
**生成时间**: ${new Date().toLocaleDateString('zh-CN').replace(/\//g, '-')}
`;

  return report;
}

/**
 * 主函数
 */
async function main() {
  const args = process.argv.slice(2);
  const command = args[0];

  // 判断输出格式
  const outputJson = command === 'json' || command === '--json';

  try {
    // 获取指数数据
    const indices = await fetchIndexData();

    // 获取板块数据
    const boards = await fetchBoardData();

    // 分析并构建报告数据
    const reportData = analyzeAndBuildReportData(boards, indices);

    // 根据命令输出不同格式
    if (outputJson) {
      // JSON 输出
      console.log(JSON.stringify(reportData, null, 2));
    } else {
      // 格式化输出（默认）
      const report = generateReport(reportData);
      console.log(report);
    }

    return 0;
  } catch (error) {
    console.error(`[ERROR] 生成报告失败: ${error.message}`);
    console.error(error.stack);
    return 1;
  }
}

// 如果是直接运行此脚本
if (require.main === module) {
  main()
    .then((code) => process.exit(code))
    .catch((error) => {
      console.error('[ERROR]', error);
      process.exit(1);
    });
}

module.exports = { generateReport, fetchBoardData, fetchIndexData };
