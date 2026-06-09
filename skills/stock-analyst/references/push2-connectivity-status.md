# Push2 连通性状态（2026-06-09 更新）

## 背景
`push2.eastmoney.com` 是东方财富的实时行情 API 后端，AkShare 的大量函数依赖它。
之前长期认为"TUN 模式下 DNS 劫持导致 push2 永久不可用"。
2026-06-09 关闭 TUN 模式后发现 push2 实为 **CDN 间歇性 Empty reply**，并非永久封禁。

## 当前状态

| 子域名 | 内网端口 | 状态 | 备注 |
|--------|---------|------|------|
| `push2.eastmoney.com` | 80/443 | ⚠️ CDN 抽风 | 单次请求约 70% 成功率 |
| `push2his.eastmoney.com` | 80/443 | ⚠️ CDN 抽风 | 同上，资金流向可用 |
| `82.push2.eastmoney.com` | 80/443 | ⚠️ CDN 抽风 | 板块行情列表 |
| `17.push2.eastmoney.com` | 80/443 | ⚠️ CDN 抽风 | 行业板块 |
| `push2ex.eastmoney.com` | 80/443 | ✅ 稳定 | 涨停板池专用 |

## CDN 特征（实测）

- **TCP 连接**: 始终成功 ✅
- **TLS 握手**: 始终成功 ✅
- **HTTP 请求**: 约 30% 返回 empty reply，curl err 52
- **重试**: 重试 1-2 次（间隔 1-2s）几乎 100% 恢复
- **所有 CDN IP**: 14.103.191.91 / 47.112.165.11 / 61.129.129.196 / 43.144.251.121 — 行为完全一致
- **根因**: 东方财富 Azure Traffic Manager CDN 节点对外部 IP 段不稳定，非本地网络问题

## 修复方案（已在 data_cache.py 中实现）

### `_run_python_with_retry()`
所有 AkShare 调用已替换为带重试版本：
- 最多 3 次重试
- 指数退避 + 随机抖动（1s → 2s → 4s）
- subprocess 超时从 30s 提升到 60s（分页请求需要）

### 特殊处理：`stock_zh_a_spot_em()`
全A实时行情函数因分页请求（~46页），单次失败概率高。
**建议用 `stock_zh_a_spot()`（新浪版）替代**，单次请求，CDN 稳定。

### 最佳实践
```python
# 推荐（单请求，稳定）
df = ak.stock_zh_a_spot()

# 不推荐（分页，高失败率）
df = ak.stock_zh_a_spot_em()
```

## 诊断 Quick Reference

```bash
# 快速测试 push2 连通性
curl -v --noproxy "*" --connect-timeout 5 -m 10 \
  "http://82.push2.eastmoney.com/api/qt/clist/get?pn=1&pz=3&fields=f12&fs=m:0+t:6+f:!2"

# 如果返回 empty reply，重试 1-2 次
# 如果持续失败，检查 TUN 模式是否开启
# 正常响应：HTTP 200 + {"rc":0,...}
```
