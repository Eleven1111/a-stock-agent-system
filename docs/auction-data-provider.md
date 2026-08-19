# 真实集合竞价量能接入

竞价链现在使用统一的竞价 provider，并用同一条 easy_tdx 连接补齐昨日量能：

- `easy_tdx.MacClient.get_auction()` 的 MAC `0x123D` 接口，读取 `09:15-09:25` 的 `price`、`matched`、`unmatched`；
- `easy_tdx.MacClient.get_stock_kline(..., Period.DAILY)` 在本地历史缓存缺失时，
  严格读取事件日前最近一根日线，并写入 `market/history.sqlite3`；结果记录
  `prev_day_provider=easy_tdx_daily` 与 `prev_day_provenance`。
- 若 easy_tdx 日线也失败，再回退到 `mootdx_adapter.fetch_mootdx_bars()`，最后使用
  `market_adapters.fetch_tencent_kline()` 历史日线入口。实时五档接口不参与昨日量能补齐。

`easy_tdx` 返回的 `matched`/`unmatched` 单位是股。provider 保留原始股数字段，
并将 `matched / 100` 作为现有 `auction_volume`（手）供金额公式使用。没有做单位猜测或零值填充。

只有以下字段全部有效时，`candidate_pipeline.rank_auction_shortlist()` 才允许进入交易短名单：

- `matched > 0`；
- `unmatched >= 0`（0 是合法的完全匹配结果）；
- `auction_volume > 0`；
- `prev_day_volume > 0`。

接口缺失、通达信服务器不可用、腾讯历史 K 线不可用、昨日量缺失，均产生
`blocked`/`degraded`，不回退到腾讯实时五档的盘中累计量，也不会把 `research_only`
候选提升到交易候选。竞价指示价冲高回落门禁保持不变。

安装依赖：

```bash
.venv/bin/python -m pip install -c constraints.txt -e '.[auction]'
```

只读真实探针（不发送 Discord、不触发交易、不写 shortlist）：

```bash
.venv/bin/python scripts/auction_data_probe.py \
  --codes sh600519,sz000001 \
  --output reports/auction-data-probe-YYYY-MM-DD.json
```

探针记录 provider、证券覆盖率、竞价时点覆盖率及逐证券失败原因。服务器或网络不可用时返回退出码 `75`，
保留 fail-closed 结果。
