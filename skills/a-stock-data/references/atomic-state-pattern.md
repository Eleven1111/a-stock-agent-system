# 原子状态管理模式 — state_store 并发安全

## 问题

文件级状态存储的"读-改-写"（read-modify-write）操作不能用两次独立加锁实现：

```python
# ❌ 错误：两次独立加锁，锁间窗口并发丢更新
existing = read_json(filepath, [])    # 锁A：读
existing.append(item)                  # 改（无锁！）
atomic_write_json(filepath, existing)  # 锁B：写
```

30 个进程并发追加时，最终只剩 2~3 条记录——因为锁 A 释放后锁 B 获取前，其他进程覆盖了中间状态。

## 正确模式：内部 _unlocked 辅助函数

拆出不带锁的读写版本，让业务方法用一把锁包住全程：

```python
def _read_json_unlocked(filepath, default=None):
    """无锁读取（调用者必须已持有 file_lock）"""
    if not os.path.exists(filepath):
        return default
    with open(filepath, "r") as f:
        return json.load(f)

def _write_json_unlocked(filepath, data):
    """无锁写入（调用者必须已持有 file_lock），含备份+临时写+os.replace"""
    _ensure_dir(filepath)
    # 备份
    if os.path.exists(filepath):
        shutil.copy2(filepath, filepath + ".bak")
    # 临时写 + 原子替换
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(filepath), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    os.replace(tmp, filepath)

def update_json_list(filepath, item, unique_key=None, max_items=None):
    """✅ 原子追加：读-改-写 全程在同一把锁内"""
    with file_lock(filepath):
        existing = _read_json_unlocked(filepath, [])
        if not isinstance(existing, list):
            existing = []
        if unique_key:
            existing = [e for e in existing
                        if not (isinstance(e, dict) and e.get(unique_key) == item.get(unique_key))]
        existing.append(item)
        if max_items and len(existing) > max_items:
            existing = existing[-max_items:]
        _write_json_unlocked(filepath, existing)
    return existing
```

## 锁文件规则

- **创建后永不删除**。删除锁文件后新建，等待旧 inode 的进程（已持有 fd）与新建 lockfile 的进程同时进入临界区。
- `os.open(lockfile, O_CREAT | O_RDWR)` 确保锁文件存在，`fcntl.flock(fd, LOCK_EX)` 阻塞等待。

## 公开 API vs 内部 API 分工

| 函数 | 锁 | 用途 |
|------|----|------|
| `atomic_write_json()` | 自带 | 外部调用：直接覆写 |
| `read_json()` | 自带 | 外部调用：读取+损坏恢复 |
| `update_json_list()` | 自带（全程） | **外部调用**：并发安全追加 |
| `_read_json_unlocked()` | **无**（调用者持锁） | 内部组合用 |
| `_write_json_unlocked()` | **无**（调用者持锁） | 内部组合用 |

**规则：** 公开 API 自动加锁，内部组合操作（如 update_json_list）复用 _unlocked 版本。
**禁止：** 公开 API 内部再调其他公开 API（会二次加锁，虽不死锁但锁间窗口丢数据）。

## 并发回归测试

```python
def test_concurrent_append_no_data_loss():
    """30 线程并发追加 → 必须精确 = 30 条"""
    result = run_concurrent_append_test(path, num_workers=30)
    assert result["no_data_loss"] is True
    assert result["final_list_length"] == 30
```

用 `run_concurrent_append_test()` 验证（已在 `state_store.py` 中实现）。
