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


def test_tencent_hist_tx_amount_column_is_volume_not_turnover():
    """腾讯 hist_tx 只返回 date/open/close/high/low/amount，没有 volume 列，
    而那个 amount 是**成交量（手）**不是成交额。

    实证（2026-08-07，600011 华能国际）：hist_tx 的 amount = 1,160,900，与当日
    实时行情的 volume 字段完全相同；实时 amount/volume = 695.22 ≈ 收盘 6.99×100，
    即单位为手。直接交给 _normalize_bar_records 会让 volume 恒为 0 ——
    volume_ratio_5d 因此全库恒 0，而它占 trend_score 8% / daban_score 10% 权重
    （2026-08-08 用部署机 8 个交易日 × 1000 只 feature_ready 记录实测：0 条 >0）。
    """
    rows = [
        {"date": "2026-08-06", "open": 7.09, "close": 7.01, "high": 7.12,
         "low": 6.97, "amount": 1358443.0},
        {"date": "2026-08-07", "open": 7.02, "close": 6.99, "high": 7.04,
         "low": 6.92, "amount": 1160900.0},
    ]

    bars = ma._tx_bar_records(rows)

    assert [bar["volume"] for bar in bars] == [1358443.0, 1160900.0]
    # 该源不提供真实成交额，不得把成交量冒充成成交额
    assert [bar["amount"] for bar in bars] == [0, 0]


def test_volume_ratio_is_computable_from_tencent_bars():
    """端到端：修复前这批 bar 的 volume 全为 0 → volume_ratio_5d 恒 0。"""
    import candidate_pipeline

    rows = [
        {"date": f"2026-06-{day:02d}", "open": 10.0, "close": 10.0,
         "high": 10.1, "low": 9.9, "amount": 1000.0}
        for day in range(1, 25)
    ]
    rows.append({"date": "2026-06-25", "open": 10.0, "close": 10.0,
                 "high": 10.1, "low": 9.9, "amount": 3000.0})

    features = candidate_pipeline.compute_price_features(ma._tx_bar_records(rows))

    assert features["volume_ratio_5d"] == 3.0
