# Push2.eastmoney.com CDN 诊断工作流

> 最后验证：2026-06-09 | 场景：ClashX TUN 关闭后 push2 间歇性 Empty reply

## 问题特征

- TUN 关闭，DNS 解析到真实 IP，TCP 80/443 通，ping 正常
- 但约 30% 的 HTTP/HTTPS 请求返回 `Empty reply from server`（curl err 52 / RemoteDisconnected）
- 重试 1-2 次后恢复 → 说明不是永久封锁，是 CDN 不稳定

## 诊断步骤

```bash
# 1. 确认 TUN 已关
curl -s http://127.0.0.1:7897/version  # 不通说明 Clash API 不在 → TUN 关了

# 2. 检查 DNS 是否解析到真实 IP（不是 198.18.x.x 假IP）
nslookup push2.eastmoney.com

# 3. 检查网络层
ping 43.144.251.121              # 应 <30ms
nc -zv -G 5 43.144.251.121 443   # TCP 应通

# 4. 逐个 IP 验证（用 --resolve 绕过 DNS 轮询）
for ip in 14.103.191.91 47.112.165.11 61.129.129.196 43.144.251.121; do
  curl -s --noproxy "*" --connect-timeout 3 -m 5 --resolve "82.push2.eastmoney.com:80:$ip" \
    "http://82.push2.eastmoney.com/api/qt/clist/get?pn=1&pz=3&fields=f12&fs=m:0+t:6" -o /dev/null -w "$ip → %{http_code}\n"
done

# 5. 测稳定度（连发10次）
for i in $(seq 10); do
  curl -s --noproxy "*" --connect-timeout 3 -m 5 -o /dev/null -w "%{http_code} " \
    "http://push2.eastmoney.com/api/qt/stock/get?secid=1.600519&fields=f43"
done
echo  # 统计 200 的次数 /10
```

## 关键发现

| 子域名 | CDN 后端 | 单次成功率 | 重试恢复 |
|--------|---------|-----------|---------|
| push2 | Azure Traffic Manager | ~70% | ✅ 1-2次 |
| 82.push2 | 同上 | ~60%（分页多更低） | ⚠️ 建议用新浪替代 |
| 17.push2 | 同上 | ~80% | ✅ 1-2次 |
| push2ex | 不同后端 | ~99% | ✅ 稳定 |

## 结论

Akshare 的 `request_with_retry`（3 次指数退避）+ `NO_PROXY=.eastmoney.com` 可使大多数单次请求函数稳定工作。仅 `stock_zh_a_spot_em()` 因分页多不适合重试，用 `stock_zh_a_spot()` 替代。
