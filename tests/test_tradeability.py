"""可成交性检查测试"""

from tradeability import limit_pct, round_limit, assess_tradeability


def test_limit_pct_main_board():
    assert limit_pct("600011", "华能国际") == 10.0
    assert limit_pct("002156", "通富微电") == 10.0


def test_limit_pct_chinext_star():
    assert limit_pct("300750", "宁德时代") == 20.0
    assert limit_pct("688981", "中芯国际") == 20.0


def test_limit_pct_st():
    assert limit_pct("600220", "ST阳光") == 5.0
    assert limit_pct("000584", "*ST工智") == 5.0


def test_limit_pct_bse():
    assert limit_pct("830799", "艾融软件") == 30.0


def test_round_limit_half_up():
    # 10 元主板：涨停 11.00 跌停 9.00
    assert round_limit(10.0, 10.0, up=True) == 11.0
    assert round_limit(10.0, 10.0, up=False) == 9.0
    # round half up 到分
    assert round_limit(9.99, 10.0, up=True) == 10.99


def test_assess_normal():
    q = {"price": 10.5, "prev_close": 10.0, "open": 10.1, "high": 10.6, "low": 10.0, "volume": 1000}
    r = assess_tradeability(q, "600011", "华能国际")
    assert r["tradeable"] is True
    assert r["status"] == "normal"
    assert r["limit_up"] == 11.0


def test_assess_yiziban_sealed():
    # 一字涨停：开=高=低=现价=涨停价
    q = {"price": 11.0, "prev_close": 10.0, "open": 11.0, "high": 11.0, "low": 11.0, "volume": 100}
    r = assess_tradeability(q, "600011", "测试")
    assert r["tradeable"] is False
    assert r["status"] == "limit_up_sealed"


def test_assess_limit_up_risky():
    # 盘中封板但有实体（低点不等于涨停价）→ 可打但有风险
    q = {"price": 11.0, "prev_close": 10.0, "open": 10.2, "high": 11.0, "low": 10.1, "volume": 5000}
    r = assess_tradeability(q, "600011", "测试")
    assert r["tradeable"] == "risky"
    assert r["status"] == "limit_up"


def test_assess_halted_zero_volume():
    q = {"price": 10.0, "prev_close": 10.0, "volume": 0}
    r = assess_tradeability(q, "600011", "测试")
    assert r["tradeable"] is False
    assert r["status"] == "halted"


def test_assess_missing_price():
    r = assess_tradeability({"price": None, "prev_close": None}, "600011", "测试")
    assert r["tradeable"] is False
