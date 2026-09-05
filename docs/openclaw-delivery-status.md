# 本轮交付状态与边界（2026-09-05）

代码基线 `bbb102c`。本文只回答一件事：**哪些是工程接通的、哪些有实测、
哪些还在等前向样本。** 四级状态分开报，前三级完成**不自动推出**第四级。

## 四级状态

| 标签 | 含义 | 本轮 |
|---|---|---|
| `engineering_verified` | 代码与隔离测试通过 | ✅ B1–B7 全部 |
| `openclaw_integration_verified` | 安装版宿主实际集成验证通过 | ❌ 本机无 openclaw |
| `deployment_verified` | 目标部署已核查/变更并验收 | ❌ 无部署机访问权 |
| `empirically_validated` | 预注册策略获得足够真实前向/OOS 证据 | ❌ 无真实前向样本 |

**本机 `which openclaw` → not found，`A_STOCK_STATE_HOME` 未设置。**
所有与部署有关的结论一律标 `deployment_unverified`：未访问到的生产不等于不存在。

## OpenClaw 实际入口

- 唯一调度 owner 默认是 OpenClaw；作业以 **command payload** 直接执行
  `scripts/run_agent_dag.py <job> --runtime openclaw --emit-target`，
  不唤醒模型当 shell 转发器。
- 已核实的 CLI 动词只有 `cron list --json` / `cron create` / `cron edit`。
  **停用（disable）用哪个动词未经核实**，见
  [openclaw-registration-reconcile.md](openclaw-registration-reconcile.md)。
- `run_agent_dag.py --runtime` 合法取值是 `hermes` / `openclaw` / `local`，
  **三者都被支持**。「必须 hermes」只对仍由 Hermes/system cron 驱动的旧部署成立。
- Hermes / local 入口作为兼容工具保留。**没有运行证据就不判定双调度**——
  发现同一逻辑任务被两个 owner 启动时再处理重复注册。

## 版本与旧产物

| 组件 | 版本 | 旧产物 |
|---|---|---|
| 统计套件 | `statistical-validation-suite-v2` / `validation_program-v2` | v1 产物**只读保留，不再通过校验**，需重算才能重新支撑准入 |
| Deflated Sharpe | `deflated_sharpe-v2` | — |
| CSCV | `cscv-v2` | — |
| 前向结算 | `strategy-forward-settlement-v1`（未动） | 标签语义未变，只新增数据集级 `label_kind` / `research_clock` |
| 探索性实验 | `exploratory_paper_experiment_v1` | 新 |
| 板块归档 | `sector_daily_archive_v1` | 新 |
| retention hold | `retention_hold_v1` | 新 |

**旧产物不自动重新获批。** 重算清单见
[statistical-method-migration.md](statistical-method-migration.md)。

## 四种「收益」不是一回事

| | 测什么 | 可作执行准入证据 |
|---|---|---|
| **预测标签** `price_path_prediction` | 价格路径动了没有；horizon=1 时同日买卖 | **否** |
| **探索性模拟** `executable_simulated_result` | 过成交约束 + 自实际成交日起 T+1 | 是（但不代表策略被认可） |
| **正式 paper pilot** | 通过完整研究门后的受监督试点，broker 对账门仍在 | 是 |
| **真实准入** | 研究门 + 审批 + 验证 + 真实对账 | — |

「允许实验」不是「认可策略」。详见
[forward-label-taxonomy.md](forward-label-taxonomy.md) 与
[exploratory-paper-experiment.md](exploratory-paper-experiment.md)。

## 交易时钟

每个实验预先声明 `signal_cutoff` / `signal_available_at` / `earliest_entry` /
`exit_rule` / 适用市场状态。默认实验 `rank_surprise_next_open_paper_v1`
明确是 S1 的**隔日延续**变体：**不检验、也不声称检验了**原策略的竞价即时入场。
盘中原策略保持 `unvalidated_intraday`（缺分钟级 PIT 与可成交证据）。

## 样本分母与缺口

- **无真实前向样本**：`sample_start` 是 2026-09-08，本机无生产数据。
  金线目前只有 fixture 证据。
- 有效样本量是 **Kish 广度**不是独立性：同日 30 只票 `trade = 30` / `session = 1`，
  产物里 `basis: "kish_breadth"` / `autocorrelation_adjusted: false`。
- 板块合成分**只覆盖报告权重的 41%（上限）**，且实测覆盖率随缺项下降；
  缺的五项全是基本面（景气/盈利/资金/估值/regime），本轮不采购数据。
- 市场级拥挤分 `unavailable`：没有生产者，不新建第四套分类器。

## 成本口径

- 缺价格是 `unknown`，**不是 0**；采纳结果为 0 时每采纳成本是 `"undefined"`。
- 一条宿主 run 只计一次，父子不叠加。
- 确定性 command 作业模型 token 记 0，CPU / 取数 / IO 另列 `unknown`。
- **没有新增计费平台**，只复用宿主已有的 session/usage 记录。

## 阈值与规则：本轮一条都没改

- `config/validation_thresholds.json` 一字未动。
- `config/strategy_forward_settlement.json` 一字未动（approved hash 全部保留）。
- `strategy_registry` 的 `paper_only` 晋级通道未动，**broker 对账门原样保留**。
- 门禁只出清单，`production_rules_changed: false`。
- 保留期 TTL 数值未改，修的是保护机制。
