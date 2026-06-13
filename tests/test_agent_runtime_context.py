from scripts.agent_runtime_context import build_runtime_context


def test_runtime_context_refreshes_canonical_agent_state(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))

    context = build_runtime_context(refresh=True)

    assert context["schema"] == "a_stock_agent_runtime_context_v1"
    assert context["runtime_contract"]["source_of_truth"] == "signal_ledger.jsonl"
    assert context["state_path"].endswith("agent_state/agent_state_latest.json")
    assert context["serenity_refresh_requests"] == []
