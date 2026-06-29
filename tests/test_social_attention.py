"""社会关注度契约、评分边界和缓存回流。"""

from datetime import datetime, timedelta

import social_attention as sa


def _rankings():
    return {
        "eastmoney": [
            {
                "code": "SZ002156",
                "name": "通富微电",
                "rank": 3,
                "rank_change": 18,
            },
            {
                "code": "SH600519",
                "name": "贵州茅台",
                "rank": 50,
                "rank_change": -2,
            },
        ],
        "xueqiu_discussion": [
            {
                "code": "SZ002156",
                "name": "通富微电",
                "rank": 8,
                "metric_value": 4200,
                "price_change_pct": 5.2,
            },
            {
                "code": "SH600519",
                "name": "贵州茅台",
                "rank": 1,
                "metric_value": 6800,
                "price_change_pct": -1.6,
            },
        ],
        "xueqiu_follow": [
            {
                "code": "SZ002156",
                "name": "通富微电",
                "rank": 12,
                "metric_value": 1200,
                "price_change_pct": 5.2,
            },
        ],
    }


def test_build_snapshot_requires_cross_source_confirmation_for_boost():
    snapshot = sa.build_social_attention_snapshot(
        _rankings(),
        trading_date="2026-06-15",
        captured_at="2026-06-15T07:04:00+00:00",
        stock_metadata={"002156": {"sector": "半导体"}},
    )

    record = snapshot["stocks"]["002156"]
    assert snapshot["schema"] == "social_attention_snapshot_v1"
    assert snapshot["status"] == "ready"
    assert record["cross_source_count"] == 2
    assert record["eligible_for_boost"] is True
    assert record["attention_score"] >= 70
    assert record["attention_velocity"] > 0
    assert snapshot["themes"]["半导体"]["stock_count"] == 1
    assert snapshot["source_versions"] == {
        "eastmoney_attention": "eastmoney-attention-v1",
        "xueqiu_attention": "xueqiu-attention-v1",
    }


def test_build_snapshot_filters_broad_industries_from_social_themes():
    snapshot = sa.build_social_attention_snapshot(
        _rankings(),
        trading_date="2026-06-15",
        captured_at="2026-06-15T07:04:00+00:00",
        stock_metadata={
            "002156": {"sector": "C 制造业", "industry": "半导体"},
            "600519": {"industry": "J 金融业"},
        },
    )

    assert snapshot["stocks"]["002156"]["sector"] == "半导体"
    assert snapshot["stocks"]["002156"]["sector_source"] == "industry"
    assert snapshot["stocks"]["600519"]["sector"] is None
    assert "半导体" in snapshot["themes"]
    assert snapshot["themes"]["半导体"]["confirmed"] is True
    assert "C 制造业" not in snapshot["themes"]
    assert "J 金融业" not in snapshot["themes"]


def test_theme_attention_evidence_requires_narrow_confirmed_theme():
    snapshot = sa.build_social_attention_snapshot(
        _rankings(),
        trading_date="2026-06-15",
        stock_metadata={"002156": {"sector": "半导体"}},
    )

    confirmed = sa.theme_attention_evidence("半导体", {"social_attention": snapshot})
    broad = sa.theme_attention_evidence("C 制造业", {"social_attention": snapshot})

    assert confirmed["available"] is True
    assert confirmed["confirmed"] is True
    assert confirmed["confirmed_stock_count"] == 1
    assert broad["available"] is False
    assert broad["confirmed"] is False


def test_single_source_is_display_only():
    snapshot = sa.build_social_attention_snapshot(
        {"eastmoney": _rankings()["eastmoney"][:1]},
        trading_date="2026-06-15",
    )
    ctx = {"social_attention": snapshot}

    record = snapshot["stocks"]["002156"]
    candidate = sa.candidate_attention_overlay("002156", ctx)
    sentiment = sa.sentiment_attention_overlay("002156", ctx)

    assert snapshot["status"] == "partial"
    assert record["cross_source_count"] == 1
    assert record["eligible_for_boost"] is False
    assert candidate["delta"] == 0.0
    assert sentiment["delta"] == 0.0
    assert candidate["display_only"] is True


def test_candidate_bonus_is_bounded_and_sentiment_adjustment_is_weak():
    snapshot = sa.build_social_attention_snapshot(
        _rankings(),
        trading_date="2026-06-15",
    )
    ctx = {"social_attention": snapshot}

    candidate = sa.candidate_attention_overlay("002156", ctx)
    sentiment = sa.sentiment_attention_overlay("002156", ctx)

    assert 0 < candidate["delta"] <= 3.0
    assert 0 < sentiment["delta"] <= 0.8
    assert candidate["record"]["cross_source_count"] == 2


def test_high_attention_price_divergence_becomes_crowding_warning():
    rankings = _rankings()
    rankings["eastmoney"][1]["rank"] = 1
    rankings["eastmoney"][1]["rank_change"] = 20
    snapshot = sa.build_social_attention_snapshot(
        rankings,
        trading_date="2026-06-15",
    )
    ctx = {"social_attention": snapshot}

    candidate = sa.candidate_attention_overlay("600519", ctx)
    sentiment = sa.sentiment_attention_overlay("600519", ctx)

    assert candidate["delta"] < 0
    assert sentiment["delta"] == -0.8
    assert any("背离" in note for note in sentiment["notes"])


def test_cache_round_trip_and_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    snapshot = sa.build_social_attention_snapshot(
        _rankings(),
        trading_date="2026-06-15",
        captured_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )

    sa.write_social_attention_cache(snapshot, {"snapshot_id": "snap-test"})

    cached = sa.read_social_attention_cache()
    assert cached["payload"]["schema"] == "social_attention_snapshot_v1"
    assert cached["snapshot_ref"]["snapshot_id"] == "snap-test"
    future = datetime.now().astimezone() + timedelta(hours=9)
    assert sa.read_social_attention_cache(max_age_hours=8, now=future) is None
