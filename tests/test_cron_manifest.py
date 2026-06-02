"""Cron Manifest 校验测试"""

import json, os, tempfile
from scripts.validate_cron_manifest import validate

VALID_JOB = {
    "id": "test-job",
    "name": "Test",
    "schedule": "0 9 * * 1-5",
    "timezone": "Asia/Shanghai",
    "command": "echo hello",
    "cwd": ".",
    "enabled": True,
    "silent_when_no_signal": True,
    "expected_output": "text",
    "external": True,
}


def test_valid_manifest():
    manifest = {"jobs": [VALID_JOB]}
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
    j1 = dict(VALID_JOB)
    j2 = dict(VALID_JOB)
    manifest = {"jobs": [j1, j2]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_placeholder_without_vars():
    j = dict(VALID_JOB)
    j["command"] = "python script.py {code} {name}"
    manifest = {"jobs": [j]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is False
    finally:
        os.unlink(path)


def test_placeholder_with_vars():
    j = dict(VALID_JOB)
    j["command"] = "python script.py {code}"
    j["template_vars"] = ["code"]
    manifest = {"jobs": [j]}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        path = f.name
    try:
        assert validate(path) is True
    finally:
        os.unlink(path)
