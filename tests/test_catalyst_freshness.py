"""催化面结构化 — 分级关键词权重 × 新鲜度衰减。"""

from datetime import datetime

import four_dim_scorer as fds

NOW = datetime(2026, 6, 10, 12, 0)


def _n(title, date=""):
    return {"title": title, "snippet": "", "source": {}, "date": date}


def test_age_parse_relative_and_absolute():
    assert fds._news_age_days("2 days ago", NOW) == 2
    assert fds._news_age_days("3 hours ago", NOW) == 0.125
    assert fds._news_age_days("06/08/2026, 07:00 AM, +0000 UTC", NOW) == 2.5
    assert fds._news_age_days("garbage", NOW) is None
    assert fds._news_age_days("", NOW) is None


def test_freshness_bands():
    assert fds.freshness_factor(1) == 1.0
    assert fds.freshness_factor(5) == 0.7
    assert fds.freshness_factor(20) == 0.4
    assert fds.freshness_factor(60) == 0.2
    assert fds.freshness_factor(None) == 0.6


def test_tier1_beats_tier3():
    t1 = fds.news_catalyst_score([_n("国产替代加速推进", "1 day ago")], NOW)["delta"]
    t3 = fds.news_catalyst_score([_n("行业迎来利好", "1 day ago")], NOW)["delta"]
    assert t1 == 1.2 and t3 == 0.4
    assert t1 > t3


def test_stale_news_decayed():
    fresh = fds.news_catalyst_score([_n("公司中标大额订单", "1 day ago")], NOW)["delta"]
    stale = fds.news_catalyst_score([_n("公司中标大额订单", "2 months ago")], NOW)["delta"]
    assert fresh == 0.8
    assert stale == round(0.8 * 0.2, 2)


def test_bearish_tier1_full_negative():
    out = fds.news_catalyst_score([_n("公司被立案调查", "1 day ago")], NOW)
    assert out["delta"] == -1.2
    assert any("⚠️" in s for s in out["signals"])


def test_per_news_takes_highest_tier_only():
    # 同条新闻同时含 T1"国家战略"与 T2"订单" → 只计 T1 一次
    out = fds.news_catalyst_score([_n("国家战略带动订单增长", "1 day ago")], NOW)
    assert out["delta"] == 1.2
