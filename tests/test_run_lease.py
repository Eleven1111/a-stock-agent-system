import run_lease


def test_only_one_runtime_can_hold_same_job_lease(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    with run_lease.claim(
        "open-confirmation",
        trading_date="2026-06-12",
        batch_id="a-share-20260612",
        run_id="hermes-run",
        runtime="hermes",
    ) as first:
        assert first["acquired"] is True
        with run_lease.claim(
            "open-confirmation",
            trading_date="2026-06-12",
            batch_id="a-share-20260612",
            run_id="openclaw-run",
            runtime="openclaw",
        ) as second:
            assert second["acquired"] is False
            assert second["holder"]["run_id"] == "hermes-run"

    with run_lease.claim(
        "open-confirmation",
        trading_date="2026-06-12",
        batch_id="a-share-20260612",
        run_id="openclaw-run",
        runtime="openclaw",
    ) as released:
        assert released["acquired"] is True
