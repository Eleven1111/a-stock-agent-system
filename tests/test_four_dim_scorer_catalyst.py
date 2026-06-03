"""催化面评分 — 区分"数据源不可用"与"可用但无新闻"。

数据源不可用（无 API key / 请求出错）→ available=False，上层应排除催化维度并重归一化。
数据源可用但无新闻 → available=True 且中性 5.0，应作为中性信息纳入，而非抬高其他维度权重。
"""

import four_dim_scorer as fds


def test_catalyst_source_unavailable(monkeypatch):
    monkeypatch.setattr(fds, "fetch_serpapi_news", lambda q, num=5: None)
    c = fds.score_catalyst("600001", "测试")
    assert c["available"] is False
    assert c["news_count"] == 0


def test_catalyst_available_but_no_news_is_neutral(monkeypatch):
    monkeypatch.setattr(fds, "fetch_serpapi_news", lambda q, num=5: [])
    c = fds.score_catalyst("600001", "测试")
    assert c["available"] is True
    assert c["news_count"] == 0
    assert c["score"] == 5.0   # 无新闻=中性，不偏多也不偏空


def test_catalyst_available_with_bullish_news(monkeypatch):
    monkeypatch.setattr(
        fds, "fetch_serpapi_news",
        lambda q, num=5: [{"title": "公司中标重大订单", "snippet": "", "source": {}, "date": ""}],
    )
    c = fds.score_catalyst("600001", "测试")
    assert c["available"] is True
    assert c["news_count"] == 1
    assert c["score"] > 5.0   # 命中利好关键词 → 加分


def test_fetch_serpapi_news_no_key_returns_none(monkeypatch):
    """无 API key 应返回 None（数据源不可用），而非 []（可用但无新闻）。"""
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    assert fds.fetch_serpapi_news("任意查询") is None
