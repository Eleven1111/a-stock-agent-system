"""龙虎榜只能是确认/拥挤/复盘变量，永远不得成为实盘排序因子（AGENTS.md 红线）。

研究报告的依据：交易公开信息是**滞后**的结果数据，买入席位次日也可能卖出，且
高关注度本身会抬高拥挤与反转风险；学术上涨停日买入-次日卖出伴随更长期反转。
所以它可以用来解释和复盘，不能用来排序。

这里守的是**行为**而不是文档措辞：只断言 AGENTS.md 里写了这句话，等于没守——
死配置能靠"配置断言"活好几个月（仓内 auction-finalize 那条）。
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

import four_dim_scorer as fds


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _score(quote):
    return fds.score_technical("002156", "通富微电", quote=quote, klines=[])


def test_dragon_tiger_value_does_not_move_the_live_score():
    """同一标的，仅龙虎榜机构净买额不同 → 实盘技术分必须逐字段一致。"""
    base = {"price": 10.0, "change_pct": 1.0}
    hot = {**base, "institution_lhb_net_buy": 3.5e8, "institution_lhb_net_wan": 35000.0}
    cold = {**base, "institution_lhb_net_buy": -3.5e8, "institution_lhb_net_wan": -35000.0}

    neutral, bullish, bearish = _score(base), _score(hot), _score(cold)

    assert neutral["score"] == bullish["score"] == bearish["score"], (
        "龙虎榜净买额改变了实盘分数——它已经变成排序因子，违反 AGENTS.md 红线"
    )
    for key in ("signals", "notes"):
        assert bullish.get(key) == bearish.get(key) == neutral.get(key)


def test_scoring_config_declares_no_dragon_tiger_weight():
    """配置侧的第二道闸：评分权重表里不得出现龙虎榜条目。"""
    config = yaml.safe_load((ROOT / "config" / "scoring.yaml").read_text(encoding="utf-8"))
    flattened = yaml.safe_dump(config, allow_unicode=True)
    for banned in ("lhb", "dragon_tiger", "龙虎"):
        assert banned not in flattened, f"scoring.yaml 出现龙虎榜权重条目: {banned}"


def _contract_text() -> str:
    """折行不该是断言的一部分——把空白归一化再匹配。"""
    raw = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    return " ".join(raw.split())


def test_agents_contract_states_the_red_line():
    """文档与实现同步：行为守住了，契约也要写明，否则下一个人不知道这是红线。"""
    contract = _contract_text()
    assert "Dragon-tiger" in contract
    assert "never enter live ranking weights" in contract


@pytest.mark.parametrize("banned", ("ignition", "quote-and-cancel", "induce"))
def test_agents_contract_bans_price_influencing_order_logic(banned):
    """《证券法》第五十五条边界：只取势借势跟势，不得以影响价格/诱导跟随为目的下单。"""
    assert banned in _contract_text()
