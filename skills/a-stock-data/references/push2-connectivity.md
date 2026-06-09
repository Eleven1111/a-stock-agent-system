# Push2.eastmoney.com CDN 连接性诊断

> 最后更新：2026-06-09 | 结论：CDN 间歇性 Empty reply，非永久封禁，retry 可恢复

## 根因

TUN 模式关闭后，push2 的 DNS 解析到真实 IP（Azure Traffic Manager），TCP 80/443 通，ping 正常。但约 30% 的 HTTP 请求返回 `Empty reply from server`（curl err 52 / urllib RemoteDisconnected）。所有 CDN 节点行为一致，说明是 CDN 层面的限流/调度问题，非本地配置。

## 可用性速查

| 子域名 | 状态 | 说明 |
|--------|------|------|
| `push2.eastmoney.com` | ⚠️ CDN 间歇性 | 单次请求，重试1-2次恢复 |
| `push2his.eastmoney.com` | ⚠️ CDN 间歇性 | 同上，资金流向/历史K线 |
| `82.push2.eastmoney.com` | ⚠️ 失败率较高 | `stock_zh_a_spot_em` 分页请求，某页失败则全挂 |
| `17.push2.eastmoney.com` | ⚠️ CDN 间歇性 | 行业板块，单次请求成功率高 |
| `push2ex.eastmoney.com` | ✅ 稳定 | 涨停板池，不受影响 |

## 重试方案

```python
import time
for attempt in range(3):
    try:
        df = ak.stock_board_industry_name_em()
        break
    except Exception:
        if attempt < 2:
            time.sleep(2 ** attempt)  # 指数退避: 1s → 2s
        else:
            raise
```

如有 AkShare 内置 `request_with_retry`（3 次指数退避），大多数单次函数可用。

## 不建议用 `stock_zh_a_spot_em()`

因分页多（全A ~4600 只分 ~46 页），任一页失败则全挂，重试后仍需全部重来。优先用 `stock_zh_a_spot()`（新浪 API）替代。

## 已验证可用的 EM 函数

| 函数 | 稳定度 | 备注 |
|------|--------|------|
| `stock_board_industry_name_em()` | ⚠️ 良 | 单次请求，重试1-2次即可 |
| `stock_individual_info_em("600519")` | ⚠️ 良 | 同上 |
| `stock_zh_a_hist("600519")` | ⚠️ 良 | 同上 |
| `stock_individual_fund_flow("600519", "sh")` | ⚠️ 良 | 同上 |
| `stock_zt_pool_em("20260608")` | ✅ 优 | 走 push2ex，稳定 |
