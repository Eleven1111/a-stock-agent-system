# 共享模块参考

## a_stock_http.py — 统一数据访问层

所有金融数据抓取应走此模块，不再裸写 `urllib.request`。

```python
from skills.common.a_stock_http import (
    load_hermes_env,        # 加载 ~/.hermes/.env
    http_get_json,          # 通用 HTTP GET → JSON
    fetch_tencent_quote,    # 腾讯实时行情 (批量)
    fetch_tencent_kline,    # 腾讯历史K线 (日/周/月/60/30)
    fetch_tencent_hk_quote, # 港股实时行情 (单个)
    fetch_eastmoney_json,   # 东方财富 JSON API
    DataSourceError,        # 统一数据源异常
)

# 示例
load_hermes_env()
quotes = fetch_tencent_quote(["sh600519", "sz000001"])
kline = fetch_tencent_kline("000001", "sz", 60, "day")
```

**设计原则：**
- 始终返回 UTF-8，内部处理 GBK 解码
- 东财 API 依赖 NO_PROXY（需 `load_hermes_env()` 或 Hermes Agent 环境）
- 异常统一抛 `DataSourceError`，绝不静默吞掉

## state_store.py — 原子状态存储

所有 JSON 状态文件写入必须走此模块。裸 `json.dump` 在多 cron 并发时可能丢数据。

```python
from skills.common.state_store import (
    read_json,              # 读取 JSON，损坏时从 .bak 恢复
    atomic_write_json,      # 原子写入 (tmp → replace)
    update_json_list,       # 原子追加列表项（支持去重/截断）
    file_lock,              # 上下文管理器，fcntl.flock
    mark_failed,            # 标记最后一次操作失败
)

# 示例
data = read_json("/path/to/file.json", default={})
atomic_write_json("/path/to/file.json", data)
```

**已接入的脚本：**
- `portfolio_manager.py` — 4处替换 ✅
- `performance_tracker.py` — history读写 ✅
- `intraday_monitor.py` — alert_cache ✅
