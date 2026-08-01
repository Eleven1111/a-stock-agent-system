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
