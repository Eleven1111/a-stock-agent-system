"""K线 adapter import 契约回归锁。

事故背景：PR #90 让 candidate_discovery 顶层 import 了 market_adapters 中
不存在的 fetch_eastmoney_kline，测试收集直接中断，而 CI 处于 startup_failure
静默期无人发现。PR #92 重构为 fetch_a_share_daily_kline 韧性链并保留
fetch_eastmoney_kline 兼容别名。本文件锁死：candidate_discovery 顶层依赖的
adapter 必须存在且可调用，避免再次出现"引用未实现函数"级别的破坏。
"""

import market_adapters as ma


def test_candidate_discovery_import_contract():
    assert callable(ma.fetch_a_share_daily_kline)
    assert callable(ma.fetch_tencent_kline)
    assert callable(ma.fetch_tencent_quote)


def test_legacy_eastmoney_kline_alias_still_routed():
    # 兼容别名必须继续存在（历史调用方/回滚安全），且路由到韧性链
    assert callable(ma.fetch_eastmoney_kline)


def test_push2_kline_parser_matches_tencent_bar_shape(monkeypatch):
    # 东财降级路径的 bar 结构必须与腾讯主路径同构（date/open/close/high/low/volume）
    class _Result:
        data = (
            b'{"data":{"klines":['
            b'"2026-07-08,10.00,10.20,10.30,9.90,123456,1234567.0",'
            b'"2026-07-09,10.20,10.50,10.60,10.10,234567,2345678.0"]}}'
        )

    monkeypatch.setattr(ma, "request_bytes", lambda *a, **k: _Result())
    bars = ma._fetch_eastmoney_push2_kline("600519", market="sh", days=70)
    assert bars == [
        {"date": "2026-07-08", "open": 10.0, "close": 10.2,
         "high": 10.3, "low": 9.9, "volume": 123456, "amount": 1234567.0},
        {"date": "2026-07-09", "open": 10.2, "close": 10.5,
         "high": 10.6, "low": 10.1, "volume": 234567, "amount": 2345678.0},
    ]
