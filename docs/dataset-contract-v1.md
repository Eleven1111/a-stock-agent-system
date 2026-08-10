# Dataset Contract v1

`skills/common/dataset_contract.py` 为研究数据增加类型化、内容寻址的语义边界。
它不抓取新数据，也不改变任何策略权重；调用者只有在目录、记录、PIT 与覆盖率
全部验证通过后，才能把数据交给研究评估算子。

默认目录位于 `config/dataset_catalog.json`。首次登记的数据集是
`cross_sectional_direction_rows_v1`，用于把决策时点已经可见的排序分数与后来
观察到的前瞻收益连接为评估数据。

## 两层哈希

- 每个 `dataset_contract_v1` 规范化后生成 `contract_hash`。
- 排序后的全部契约再生成 `dataset_catalog_v1.catalog_hash`。

哈希不依赖 JSON key 顺序。分析产物必须同时记录 dataset ID、contract hash 和
catalog hash；只记录文件路径不足以证明使用了同一份语义。

## 契约内容

每个数据集必须声明：

- provider 与 adapter version；
- source rank、频率、时区、币种和复权/调整语义；
- 字段名、类型、业务语义、单位和 nullable；
- 特征截止、特征可用、结果期末、结果可用与 snapshot ref 的 PIT 字段；
- universe、最低覆盖率与 missing policy；
- producer、producer version 与上游输入；
- validators 和 known limitations。

`not_applicable` 是合法而明确的调整语义；缺字段、空字符串或“以后再确认”不是。

## 记录验证

```python
from pathlib import Path

import dataset_contract

catalog = dataset_contract.load_catalog(Path("config/dataset_catalog.json"))
contract = dataset_contract.resolve_dataset(
    catalog,
    "cross_sectional_direction_rows_v1",
)
validation = dataset_contract.validate_records(
    rows,
    contract,
    coverage_ratio=observed_coverage_ratio,
)
```

下列情况一律抛出 `DatasetContractError`：

- 记录出现契约外字段或类型不匹配；
- `forward_return` 等已知语义使用错误单位；
- 特征在 `src` 之后才可用；
- 结果在 `dst` 之前已经“可见”；
- snapshot ref 缺失；
- 覆盖率缺失、非法或低于目录阈值；
- dataset ID 重复或调用者绑定的 contract hash 已过期。

## 边界

- 目录描述数据语义，不证明数据源已经部署或覆盖率已经达标。
- `forward_return` 只允许进入评估算子，禁止作为实时特征。
- 新数据集登记不等于策略准入；方向评估仍受独立样本与 strategy registry 门禁。
