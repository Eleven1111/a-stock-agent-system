"""Cron Manifest 校验测试"""

import json, os, tempfile
from scripts.validate_cron_manifest import validate


def test_valid_manifest():
    manifest = {
        "jobs": [{
            "id": "test-job",
            "name": "Test Job",
            "schedule": "0 9 * * 1-5",
            "timezone": "Asia/Shanghai",
            "command": "echo hello",
            "cwd": ".",
            "enabled": True,
            "silent_when_no_signal": True,
            "expected_output": "text",
            "external": True,
        }]
    }
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is True
    finally:
        os.unlink(path)


def test_missing_field():
    manifest = {"jobs": [{"id": "bad", "name": "Bad"}]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_duplicate_ids():
    manifest = {
        "jobs": [
            {"id": "dup", "name": "A", "schedule": "0 9 * * 1", "timezone": "Asia/Shanghai",
             "enabled": True, "external": True},
            {"id": "dup", "name": "B", "schedule": "0 10 * * 1", "timezone": "Asia/Shanghai",
             "enabled": True, "external": True},
        ]
    }
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)
