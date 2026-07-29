"""Repository configuration has one validated path and schema registry."""

from __future__ import annotations

import json

import pytest

from config_registry import ConfigError, load_registered, validate_registered_configs


def test_repository_configs_pass_registry_validation():
    report = validate_registered_configs()
    assert report["status"] == "ok"
    assert set(report["configs"]) == {
        "calendar",
        "candidate_selection",
        "daban_thresholds",
        "data_access",
        "nl_screening",
        "reflexivity_strategy",
        "paper_trading",
        "scoring",
        "tail_close_strategy",
    }


def test_registered_loader_rejects_missing_required_root(tmp_path):
    path = tmp_path / "candidate_selection.json"
    path.write_text(json.dumps({"network": {}}), encoding="utf-8")

    with pytest.raises(ConfigError, match="required root"):
        load_registered("candidate_selection", path=path)
