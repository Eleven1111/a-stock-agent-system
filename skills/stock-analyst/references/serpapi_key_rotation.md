# SerpAPI 多 Key 轮询管理

## 背景

SerpAPI 按 key 独立计费，每个 key 每月有免费额度上限（通常 100 次/月）。当频繁调用新闻搜索（高温主题跟踪、个股分析等），单 key 的额度会迅速耗尽。

**用户偏好：** 消耗量大时主动提供备用 key，而非降低查询频率。

## 配置方式

```env
# ~/.hermes/.env
SERPAPI_KEYS=key1,key2,key3
```

用逗号分隔，数量不限。当前配置 3 个 key。

## 实现（scripts/news.py）

```python
# 加载
SERPAPI_KEYS = [k.strip() for k in keys_str.split(",") if k.strip()]

# 自动轮询
_KEY_INDEX = 0

def _get_next_key() -> str:
    global _KEY_INDEX
    key = SERPAPI_KEYS[_KEY_INDEX % len(SERPAPI_KEYS)]
    _KEY_INDEX += 1
    return key
```

**轮询策略：** 循环有序（round-robin），每次 `_serpapi_request()` 调用自动取下一个 key。

## 扩容

当再拿到新 key 时：
1. 追加到 `SERPAPI_KEYS` 末尾
2. 不用改代码，轮询逻辑自动覆盖新 key

## 冷启动

首次 import news.py 时从环境变量和 `.env` 文件两级读取。如果环境变量没有，会回退到文件读取。

同时自动从 `.env` 加载 `NO_PROXY` 环境变量（绕过 Clash 代理 DNS 劫持），确保 urllib 请求不走代理直接到达目标服务器。

```python
if not os.environ.get("NO_PROXY"):
    # 从 .env 查找 NO_PROXY= 行
    for line in open(os.path.expanduser("~/.hermes/.env")):
        if line.startswith("NO_PROXY="):
            os.environ["NO_PROXY"] = line.split("=", 1)[1].strip().strip("'").strip('"')
            break
```

## 验证

```bash
cd ~/.hermes/skills/stock-analyst
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from news import SERPAPI_KEYS, _get_next_key
print(f'{len(SERPAPI_KEYS)} keys loaded')
for i in range(len(SERPAPI_KEYS)):
    k = _get_next_key()
    print(f'  Key #{i+1}: {k[:12]}...{k[-8:]}')
"
```
