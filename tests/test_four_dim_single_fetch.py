"""四维打分单次抓取 — 同票实时行情 4→1 次，注入则零抓取。"""

import four_dim_scorer as fds

_FAKE_QUOTE = {"price": 10.0, "change_pct": 1.0, "pe": 20.0,
               "market_cap": 300.0, "turnover": 5.0}


def _klines():
    return [{"close": 10.0, "high": 10.2, "low": 9.8, "volume": 1000} for _ in range(60)]


def test_score_stock_single_realtime_fetch(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    rt_calls, kl_calls = [], []
    monkeypatch.setattr(fds, "fetch_tencent_realtime",
                        lambda c, m="sz": (rt_calls.append(c) or dict(_FAKE_QUOTE)))
    monkeypatch.setattr(fds, "fetch_tencent_kline",
                        lambda *a, **k: (kl_calls.append(1) or _klines()))

    fds.score_stock("600011", "华能国际")

    # 技术/情绪/深度/可成交性共用一次抓取，而非各抓一次
    assert len(rt_calls) == 1
    assert len(kl_calls) == 1


def test_score_stock_zero_fetch_when_injected(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    rt_calls, kl_calls = [], []
    monkeypatch.setattr(fds, "fetch_tencent_realtime",
                        lambda c, m="sz": (rt_calls.append(c) or dict(_FAKE_QUOTE)))
    monkeypatch.setattr(fds, "fetch_tencent_kline",
                        lambda *a, **k: (kl_calls.append(1) or _klines()))

    res = fds.score_stock("600011", "华能国际", quote=dict(_FAKE_QUOTE), klines=_klines())

    assert rt_calls == []   # 注入 quote → 0 次实时抓取
    assert kl_calls == []   # 注入 klines → 0 次K线抓取
    assert res["grade"] in {"S", "A", "B", "C", "D"}
