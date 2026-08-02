# chan.py reference oracle（第三方代码，仅测试引用）

## 出处

- 上游仓库：https://github.com/Vespa314/chan.py
- Pinned commit：`429d6ed3043e27c93a003ba2b10e70a05575e1f5`（2026-06-25，"chore"）
- License：MIT（见本目录 `LICENSE`，原样保留）

## 用途与边界

本目录是缠论算法升级（`docs/chanlun-upgrade-plan-2026-08.md`）的**差分测试 oracle**：
生产侧在 `skills/chanlun-backtest/scripts/` 用纯函数重写缠论笔/线段/中枢/买卖点算法，
本目录提供参照实现，供测试用例比对结构输出（笔端点、线段端点、中枢区间、买卖点位置）。

**严禁生产 import**：chan.py 的配置解析路径使用 `exec()`（`Common/func_util.py`
`_parse_inf`），属于本仓库安全红线（见 `~/.claude/rules/security.md`）。
`tests/test_chan_reference_guard.py` 静态扫描 `skills/` 与 `scripts/` 下所有源码，
断言不存在 `third_party.chan_py_reference` 的 import；任何生产代码引入该 import 都会
让门禁测试失败。

## 目录裁剪

相对上游仓库，已删除以下与算法核心无关的内容：

- `Plot/`、`Image/`（绘图与示例图片）
- `App/`（示例应用）
- `Debug/`（策略示例脚本，依赖 BaoStock 网络数据源）
- `main.py`（CLI 入口，依赖网络数据源）
- `DataAPI/` 下除 `csvAPI.py`、`CommonStockAPI.py`、`__init__.py` 以外的文件
  （`AkshareAPI.py`、`BaoStockAPI.py`、`ccxt.py` 均为网络数据源适配器，未引入）

保留：算法核心（`Bi/`、`Seg/`、`ZS/`、`BuySellPoint/`、`KLine/`、`Combiner/`、
`Common/`、`Math/`、`ChanModel/`）、`Chan.py`、`ChanConfig.py`、`Script/`
（仅 requirements.txt）、`quick_guide.md`（上游算法说明文档，未改动）。

## 离线 driver

`offline_driver.py` 提供 `run_offline(bars)`：接收合成日 K 线序列，通过
`CChan.trigger_load()` 直接灌入 `CKLine_Unit` 列表（`trigger_step=True`
绕过 `CChan.__init__` 中默认调用的网络加载路径 `GetStockAPI()`/`load()`），
全程无网络依赖，返回笔列表与买卖点列表的纯数据快照。仅供
`tests/test_chan_reference_driver.py` 及后续差分测试调用。

## 本地修补清单

| # | 文件 | 修补内容 | 原因 |
|---|---|---|---|
| （无） | — | — | 截至 T0，未对 chan.py 算法代码做任何修改；仅做目录裁剪（见上）。若后续差分测试发现裁剪导致悬空 import 或需要离线适配，在此表逐条追加，禁止无记录改动。 |
| 1 | `offline_driver.py`（本仓库自建，非上游文件） | `run_offline(bars, overrides=None)` 新增可选参数，合并进 `CChanConfig` 字典（`trigger_step` 仍强制为 True） | T1 差分测试需要按非默认笔配置（`bi_fx_check`/`bi_strict`/`gap_as_kl`/`bi_allow_sub_peak`）跑 oracle，验证 4 档分型有效性检查。**未触碰任何 chan.py 算法代码。** |
| 2 | `offline_driver.py`（本仓库自建，非上游文件） | 新增 `SegRecord`/`OfflineResult` 与 `run_offline_structure(bars, overrides=None)`，导出 `kl_list.seg_list` 的线段快照（方向、起止 klu 时间、`is_sure`、起止笔 idx、`reason`）；`run_offline` 保持原二元组签名，改为其薄封装 | T2 线段差分测试需要 oracle 侧的线段列表；沿用旧签名以免改动既有测试。**未触碰任何 chan.py 算法代码。** |
| 3 | `offline_driver.py`（本仓库自建，非上游文件） | 新增 `ZsRecord`，`OfflineResult` 增加 `zs_records` 字段，导出 `kl_list.zs_list` 的中枢快照（`zs.high`→zg、`zs.low`→zd、起止 klu 时间、`is_sure`、起止笔 idx） | T3 中枢差分测试需要 oracle 侧的中枢列表。`run_offline` 二元组签名不变。**未触碰任何 chan.py 算法代码。** |
| 4 | `offline_driver.py`（本仓库自建，非上游文件） | `BspRecord` 增加两个带默认值的字段：`is_sure`（`bsp.bi.is_sure`，锚定笔确定态）与 `features`（`bsp.features` 的普通 dict 快照） | T4 买卖点差分测试只比对**确定笔上**的买卖点，需要 oracle 侧的 `is_sure`；`features` 用于核对 feature_dict 键名对齐。字段带默认值，既有构造点与 `run_offline` 签名均不变。**未触碰任何 chan.py 算法代码。** |
