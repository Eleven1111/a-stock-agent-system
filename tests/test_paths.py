"""路径解耦测试 — HERMES_HOME 重定向"""

import os
import importlib


def test_default_home(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    import paths
    importlib.reload(paths)
    assert paths.hermes_home() == os.path.expanduser("~/.hermes")


def test_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import paths
    importlib.reload(paths)
    assert paths.hermes_home() == str(tmp_path)
    assert paths.data_file("stock-triage", "x.json") == \
        os.path.join(str(tmp_path), "skills", "stock-triage", "data", "x.json")
    assert paths.env_file() == os.path.join(str(tmp_path), ".env")


def test_cache_and_cron_dirs(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import paths
    importlib.reload(paths)
    assert paths.cache_dir("global-market-monitor").startswith(str(tmp_path))
    assert paths.cron_output_dir().endswith(os.path.join("cron", "output"))
