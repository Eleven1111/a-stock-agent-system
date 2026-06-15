"""社会关注度外部源解析和故障隔离。"""

import pytest

import social_attention_adapters as adapters
from http_client import DataSourceError


def test_parse_eastmoney_rank_payload():
    rows = adapters.parse_eastmoney_rank_payload({
        "status": 0,
        "code": 0,
        "data": [
            {"sc": "SZ002156", "rk": 3, "hisRc": 18},
            {"sc": "SH600584", "rk": 9, "hisRc": -2},
        ],
    })

    assert rows[0] == {
        "code": "SZ002156",
        "name": None,
        "rank": 3,
        "rank_change": 18.0,
    }
    assert rows[1]["code"] == "SH600584"


def test_parse_eastmoney_rising_payload_uses_rising_rank():
    rows = adapters.parse_eastmoney_rising_payload({
        "status": 0,
        "code": 0,
        "data": [
            {"sc": "SZ300588", "rk": 362, "hrc": 4449, "hrcrk": 1},
        ],
    })

    assert rows == [{
        "code": "SZ300588",
        "name": None,
        "rank": 1,
        "rank_change": 4449.0,
        "current_rank": 362,
    }]


def test_parse_xueqiu_rank_payload():
    rows = adapters.parse_xueqiu_rank_payload(
        {
            "error_code": 0,
            "data": {
                "list": [
                    {
                        "symbol": "SZ002156",
                        "name": "通富微电",
                        "tweet7d": 4200,
                        "pct": 5.2,
                    }
                ]
            },
        },
        metric="tweet7d",
    )

    assert rows == [{
        "code": "SZ002156",
        "name": "通富微电",
        "rank": 1,
        "metric_value": 4200.0,
        "price_change_pct": 5.2,
    }]


def test_baidu_403_shape_is_typed_failure_not_empty_success():
    with pytest.raises(DataSourceError) as exc:
        adapters.parse_baidu_hot_payload({
            "ResultCode": "403",
            "Result": [],
        })

    assert exc.value.source == "baidu_attention"
    assert exc.value.error_type == "invalid_response"


def test_collect_sources_keeps_healthy_providers_when_one_drifts():
    rankings, health = adapters.collect_social_rankings(
        eastmoney_fetcher=lambda: [{
            "code": "SZ002156",
            "name": None,
            "rank": 3,
            "rank_change": 18,
        }],
        xueqiu_fetcher=lambda: {
            "xueqiu_discussion": [{
                "code": "SZ002156",
                "name": "通富微电",
                "rank": 8,
                "metric_value": 4200,
                "price_change_pct": 5.2,
            }],
            "xueqiu_follow": [],
        },
        baidu_fetcher=lambda: (_ for _ in ()).throw(
            DataSourceError("baidu_attention", "shape drift")
        ),
        baidu_enabled=True,
    )

    assert set(rankings) == {
        "eastmoney",
        "xueqiu_discussion",
        "xueqiu_follow",
    }
    assert health["eastmoney"]["status"] == "ok"
    assert health["xueqiu"]["status"] == "ok"
    assert health["baidu"]["status"] == "failed"
