"""行业板块目录：NaN 代码与「同花顺码当东财 BK 码用」两个静默缺陷的回归。

2026-08-05 实测：fetch_industry_boards() 返回 361 条里 271 条 code 是字符串
"nan"，其余是同花顺 885xxx，东财 BK 码零条 —— 而唯一消费者
capital_flow_monitor.resolve_sector_code 要拿它当 BK 码去查东财。
"""

import math

import pytest

import market_adapters as ma


# ------------------------------------------------------------- NaN 代码

def test_first_code_skips_nan_because_nan_is_truthy():
    """`a or b` 在 a 是 NaN 时不会 fall through —— NaN 为真值。

    这正是 271 行 code 变成 "nan" 的成因。
    """
    assert (float("nan") or "308614") != "308614"  # 记录被修的语义
    assert ma._first_code(float("nan"), "308614") == "308614"


@pytest.mark.parametrize("values,expected", [
    ((None, "BK0477"), "BK0477"),
    (("", "BK0477"), "BK0477"),
    (("   ", "BK0477"), "BK0477"),
    (("nan", "BK0477"), "BK0477"),
    (("NaN", "BK0477"), "BK0477"),
    ((float("nan"), None, ""), ""),
    (("BK0477", "BK9999"), "BK0477"),
    ((123456,), "123456"),
])
def test_first_code_picks_first_real_value(values, expected):
    assert ma._first_code(*values) == expected


def test_first_code_never_returns_the_string_nan():
    assert ma._first_code(float("nan")) == ""
    assert math.isnan(float("nan"))  # 固定住前提，避免测试自身失真


# ------------------------------------------------- 只放行东财 BK 代码

def _board(code, name, **extra):
    row = {"f12": code, "f14": name}
    row.update(extra)
    return row


def test_industry_boards_keep_only_eastmoney_bk_codes(monkeypatch):
    monkeypatch.setattr(ma, "fetch_board_quotes", lambda: [
        _board("BK0477", "半导体"),
        _board("885652", "钛白粉概念"),       # 同花顺概念指数
        _board("308614", "阿尔茨海默概念"),   # 同花顺概念
        _board(float("nan"), "缺代码的板块"),
        _board("BK0719", "AI算力"),
    ])

    assert ma.fetch_industry_boards() == [("BK0477", "半导体"), ("BK0719", "AI算力")]


def test_industry_boards_return_empty_rather_than_wrong_codes(monkeypatch):
    """上游只有同花顺码时必须返回空 —— 下游会记进 unmapped_sectors，缺口可见。

    返回同花顺码会让 resolve_sector_code 把它当 BK 码喂给东财，静默错。
    """
    monkeypatch.setattr(ma, "fetch_board_quotes", lambda: [
        _board("885652", "钛白粉概念"),
        _board("885650", "碳纤维"),
    ])

    assert ma.fetch_industry_boards() == []


def test_industry_boards_warn_when_dropping(monkeypatch, caplog):
    monkeypatch.setattr(ma, "fetch_board_quotes", lambda: [_board("885652", "钛白粉概念")])

    with caplog.at_level("WARNING"):
        ma.fetch_industry_boards()

    assert any("非东财 BK 代码" in record.getMessage() for record in caplog.records)


def test_industry_boards_skip_rows_without_a_name(monkeypatch):
    monkeypatch.setattr(ma, "fetch_board_quotes", lambda: [
        _board("BK0477", ""),
        _board("BK0719", None),
        _board("BK0710", "军工航天"),
    ])

    assert ma.fetch_industry_boards() == [("BK0710", "军工航天")]


def test_industry_boards_fall_back_to_nested_raw_code(monkeypatch):
    monkeypatch.setattr(ma, "fetch_board_quotes", lambda: [
        {"f12": float("nan"), "f14": "半导体", "raw": {"code": "BK0477"}},
    ])

    assert ma.fetch_industry_boards() == [("BK0477", "半导体")]
