# 证据保留期 vs 研究周期

## 缺口

`config/data_access.json` 的默认值：

| 项 | 值 |
|---|---|
| `snapshot_input_retention_days` | 30 |
| `snapshot_output_retention_days` | 90 |
| `cron_artifact_retention_days` | 30 |
| `reference_protection_days` | 30 |

引用保护来自 `storage_retention._scan_recent_references`，它**只读 30 天内被修改过的**
文件。一个预注册实验在第 0 天冻结输入之后就不再改那条记录了——一个月后它的 mtime
落在窗口外，它引用的快照全部变成可删。

而一轮 60 个拟合交易日 + 60 个 OOS 交易日大约要跑 **170 个自然日**。
证据会在研究还在进行时被回收。

## 处置：显式 retention hold

`skills/common/retention_protection.py`。hold 是一条**append-only** 的声明：
某些快照路径必须存活，与文件年龄无关。

```python
from retention_protection import place_hold, release_hold, active_holds
place_hold("study-a", [frozen_snapshot], reason="fitting_window")
```

- **不做 mtime 过滤**：holds 整份读取，这正是它存在的理由。
- **释放也是追加**：账本是历史不是可变集合，「第 N 天保护了什么、为什么」事后可回答。
- 同一 scope 后写的记录覆盖先写的，所以一个被释放的研究可以重新开启。
- `expires_at` 可选；**不给就一直有效**。一个结束日期未知的研究不该在一个猜出来的
  日期上悄悄过期。
- 便捷入口 `hold_experiment_evidence(experiment, refs)`：scope 绑定到冻结实验身份
  `experiment:<id>:<sha256[:16]>`，实验一改 hash，scope 就是另一个。

## GC 侧

`cleanup_storage` 把 holds 并进 `references`，因此过期删除与容量上限两条路径都跳过它们。
报告新增：

- `protected.held_snapshots`：被 hold 保住的快照数。
- `protected.holds`：`{active_scopes, reasons, held_reference_count, expiring}`——
  谁在保护、为什么。
- `capacity_blocked_by_holds`：**容量压力被显式报告，而不是靠删掉在研证据来解决**。
  阻断新的非必要采集是运维决定；静默丢弃 hold 住的快照不在选项里。

## 权威副本

研究证据已有完整不可变副本时不重复归档：hold 只保护**重现所需的原料**，
不无限保存所有日志。历史 signal ledger 不删不改，也不靠改 mtime 制造伪保护。

## 验收

`tests/test_retention_protection.py`：把时钟推进到 170 天（超过每一条普通 TTL），
hold 住的快照仍在、没 hold 的同批快照被删。全部在临时目录执行。
