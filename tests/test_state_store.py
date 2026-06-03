"""state_store 并发安全测试"""
import os, json, tempfile
from state_store import atomic_write_json, read_json, run_concurrent_test


def test_basic_write_read():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        path = f.name
    try:
        data = {"key": "value", "num": 42}
        atomic_write_json(path, data)
        result = read_json(path)
        assert result == data
    finally:
        os.unlink(path)


def test_read_nonexistent():
    result = read_json("/tmp/_nonexistent_test_file_12345.json")
    assert result is None


def test_concurrent_writes():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        path = f.name
    try:
        result = run_concurrent_test(path, num_writers=10)
        assert result["all_completed"] is True, f"Not all writers completed: {result}"
        assert result["final_json_valid"] is True, "Final JSON is not valid"
        assert result["total_workers"] == 10
    finally:
        try:
            os.unlink(path)
            os.unlink(path + ".lock")
        except OSError:
            pass
