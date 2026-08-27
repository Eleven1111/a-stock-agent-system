import limitup_event_reconstruction as reconstruction


def _row(time, close):
    minute = int(time[:2]) * 60 + int(time[2:])
    return {
        "minute": minute,
        "time": f"{time[:2]}:{time[2:]}",
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume_shares": 1000.0,
        "amount": 10000.0,
    }


def test_close_state_reconstruction_is_explicitly_approximate_and_non_live():
    rows = [
        _row("0935", 10.8),
        _row("0940", 11.0),
        _row("0945", 11.0),
        _row("0950", 10.9),
        _row("0955", 11.0),
        _row("1000", 11.0),
    ]

    result = reconstruction.infer_5m_close_state(rows, limit_price=11.0)

    assert result["event_source"] == reconstruction.SOURCE_RECONSTRUCTED_5M
    assert result["first_seal_time"] == "094000"
    assert result["last_seal_time"] == "095500"
    assert result["reseal_time"] == "095500"
    assert result["open_board_count"] == 1
    assert result["field_availability"] == {
        "first_seal_time": reconstruction.APPROXIMATE_CLOSE_STATE,
        "last_seal_time": reconstruction.APPROXIMATE_CLOSE_STATE,
        "reseal_time": reconstruction.APPROXIMATE_CLOSE_STATE,
        "open_board_count": reconstruction.APPROXIMATE_CLOSE_STATE,
    }
    assert result["eligible_for_divergence_reseal"] is False
    assert "intrabar" in result["ineligibility_reason"]


def test_missing_prices_fail_closed_instead_of_becoming_no_event():
    result = reconstruction.infer_5m_close_state(
        [{"time": "09:35", "volume_shares": 1000.0}], limit_price=11.0
    )

    assert result["status"] == "unavailable"
    assert result["open_board_count"] is None


def test_bias_report_quantifies_open_board_and_fast_board_misses():
    truth = [
        {
            "date": "2026-08-20", "code": "600001",
            "first_seal_time": "093000", "last_seal_time": "095600",
            "reseal_time": "095600", "open_board_count": 2,
        },
        {
            "date": "2026-08-20", "code": "600002",
            "first_seal_time": "100000", "last_seal_time": "100000",
            "reseal_time": None, "open_board_count": 0,
        },
    ]
    reconstructed = {
        ("2026-08-20", "600001"): {
            "status": "ok", "first_seal_time": "094000",
            "last_seal_time": "095500", "reseal_time": "095500",
            "open_board_count": 1,
        },
    }

    report = reconstruction.build_bias_report(truth, reconstructed)

    assert report["truth_events"] == 2
    assert report["covered_events"] == 1
    assert report["coverage_ratio"] == 0.5
    assert report["open_board_count"]["comparable"] == 1
    assert report["open_board_count"]["exact_match_rate"] == 0.0
    assert report["open_board_count"]["mean_absolute_error"] == 1.0
    assert report["fast_board_recall"]["truth_positive"] == 1
    assert report["fast_board_recall"]["true_positive"] == 0
    assert report["fast_board_recall"]["recall"] == 0.0
    assert report["first_seal_time"]["mean_absolute_error_minutes"] == 10.0
    assert report["last_seal_time"]["mean_absolute_error_minutes"] == 1.0
    assert report["eligible_for_divergence_reseal"] is False


def test_bias_report_empty_sample_never_claims_safety():
    report = reconstruction.build_bias_report([], {})

    assert report["status"] == "blocked"
    assert report["eligible_for_divergence_reseal"] is False
