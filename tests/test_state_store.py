"""state_store 并发安全测试"""
import os
import tempfile
from state_store import atomic_write_json, read_json, run_concurrent_test, run_concurrent_append_test, update_json_list


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


def test_update_json_list_basic():
    """基础追加测试"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        path = f.name
    try:
        update_json_list(path, {"id": 1, "val": "a"})
        update_json_list(path, {"id": 2, "val": "b"})
        data = read_json(path)
        assert len(data) == 2
        assert data[0]["id"] == 1
        assert data[1]["id"] == 2
    finally:
        try:
            os.unlink(path)
            os.unlink(path + ".lock")
        except OSError:
            pass


def test_update_json_list_dedup():
    """去重测试"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        path = f.name
    try:
        update_json_list(path, {"id": 1, "val": "a"}, unique_key="id")
        update_json_list(path, {"id": 1, "val": "b"}, unique_key="id")
        data = read_json(path)
        assert len(data) == 1
        assert data[0]["val"] == "b"
    finally:
        try:
            os.unlink(path)
            os.unlink(path + ".lock")
        except OSError:
            pass


def test_concurrent_append_no_data_loss():
    """
    并发追加测试：30 个线程同时追加，最终列表必须有 30 条。
    如果 update_json_list 的读改写不在同一把锁内，此测试会丢数据。
    """
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        path = f.name
    try:
        result = run_concurrent_append_test(path, num_workers=30)
        assert result["all_completed"] is True, f"Not all completed: {result}"
        assert result["no_data_loss"] is True, (
            f"Data loss detected: expected {30}, got {result['final_list_length']}. "
            f"Details: {result['worker_details']}"
        )
    finally:
        try:
            os.unlink(path)
            os.unlink(path + ".lock")
        except OSError:
            pass
