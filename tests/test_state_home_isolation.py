"""回归测试 — pytest 运行不得污染真实 ~/.hermes 状态目录。

背景：skills/common/paths.py 的 hermes_home() 按
A_STOCK_STATE_HOME -> HERMES_HOME -> ~/.hermes 解析状态根目录。部分模块在
*import 时*就用 paths.data_file(...) 等函数固化了绝对路径常量（例如
signal_ledger.LEDGER_FILE），这些常量在 tests/conftest.py 顶层设置环境变量
之前如果被解析，就会指向真实生产目录。conftest.py 已经在模块导入期（sys.path
注入之后、任何业务模块被 collection 阶段导入之前）无条件覆盖了
A_STOCK_STATE_HOME（并清理了可能残留的 A_STOCK_BACKUP_HOME），本文件验证该
隔离对所有已知的 import 期路径常量都生效。
"""

import os

import paths


def _real_home_candidates():
    """用户真实 home 目录及其常见状态根，隔离后的路径不得落在这些目录下。"""
    return {
        os.path.expanduser("~"),
        os.path.expanduser("~/.hermes"),
    }


def test_state_home_env_is_overridden_to_temp_dir():
    """conftest 必须已经把 A_STOCK_STATE_HOME 指向临时目录。

    A_STOCK_BACKUP_HOME 本身不强制设成固定值 —— conftest 只清理了本机 shell
    可能残留的真实生产路径（pop 掉），交给 paths.backup_home() 基于当前
    hermes_home() 动态回退，这样才不会和逐测试 monkeypatch
    A_STOCK_STATE_HOME 的用例互相冲突（见 test_backup_home_resolves_inside_temp_dir）。
    """
    state_home = os.environ.get("A_STOCK_STATE_HOME")
    assert state_home, "conftest 应在模块导入期设置 A_STOCK_STATE_HOME"
    assert "a-stock-test-state-" in state_home


def test_hermes_home_resolves_inside_temp_dir():
    home = paths.hermes_home()
    state_home = os.environ["A_STOCK_STATE_HOME"]
    assert home == state_home
    assert home not in _real_home_candidates()
    assert not home.startswith(os.path.expanduser("~/.hermes"))


def test_backup_home_resolves_inside_temp_dir():
    """未显式配置 A_STOCK_BACKUP_HOME 时，backup_home() 相对 hermes_home()
    动态回退为同级的 "{basename}-backups" 目录，因此断言它落在 state_home
    的父目录（系统临时目录）下，而不是 state_home 本身的子目录。
    """
    backup = paths.backup_home()
    state_home = os.environ["A_STOCK_STATE_HOME"]
    assert os.path.dirname(backup) == os.path.dirname(state_home)
    assert os.path.basename(backup) == f"{os.path.basename(state_home)}-backups"
    for real in _real_home_candidates():
        assert not backup.startswith(real + os.sep)
        assert backup != real


def test_signal_ledger_file_constant_is_isolated():
    """核心断言：import 时固化的 LEDGER_FILE 常量必须落在临时目录内。"""
    import signal_ledger

    state_home = os.environ["A_STOCK_STATE_HOME"]
    assert signal_ledger.LEDGER_FILE.startswith(state_home)
    assert "/.hermes/" not in signal_ledger.LEDGER_FILE
    assert not signal_ledger.LEDGER_FILE.startswith(
        os.path.expanduser("~/.hermes")
    )


def test_monitor_registry_module_constants_are_isolated():
    """monitor_registry.py 顶层的 REGISTRY_FILE 与别名 LEDGER_FILE。"""
    import monitor_registry

    state_home = os.environ["A_STOCK_STATE_HOME"]
    assert monitor_registry.REGISTRY_FILE.startswith(state_home)
    assert monitor_registry.LEDGER_FILE.startswith(state_home)


def test_serenity_refresh_queue_constant_is_isolated():
    """serenity_refresh_queue.py 顶层的 QUEUE_FILE（虽已标记 deprecated 仍会固化）。"""
    import serenity_refresh_queue

    state_home = os.environ["A_STOCK_STATE_HOME"]
    assert serenity_refresh_queue.QUEUE_FILE.startswith(state_home)


def test_theme_registry_constant_is_isolated():
    """theme_registry.py 顶层的 REGISTRY_FILE。"""
    import theme_registry

    state_home = os.environ["A_STOCK_STATE_HOME"]
    assert theme_registry.REGISTRY_FILE.startswith(state_home)


def test_no_module_level_path_constant_leaks_to_real_home():
    """遍历本次修复中排查到的全部 import 期路径常量，统一断言不落在真实 home。

    grep 排查方法：在 skills/common/*.py 中搜索形如
    ``= data_file(`` / ``= os.path.join(hermes_home()`` / ``= cache_dir(`` /
    ``= skill_data_dir(`` 的顶层赋值，逐一收集使用 paths 模块在 import
    时构造绝对路径的模块级常量。
    """
    import signal_ledger
    import monitor_registry
    import serenity_refresh_queue
    import theme_registry

    real_hermes = os.path.expanduser("~/.hermes")
    constants = {
        "signal_ledger.LEDGER_FILE": signal_ledger.LEDGER_FILE,
        "monitor_registry.REGISTRY_FILE": monitor_registry.REGISTRY_FILE,
        "monitor_registry.LEDGER_FILE": monitor_registry.LEDGER_FILE,
        "serenity_refresh_queue.QUEUE_FILE": serenity_refresh_queue.QUEUE_FILE,
        "theme_registry.REGISTRY_FILE": theme_registry.REGISTRY_FILE,
    }
    for name, value in constants.items():
        assert not value.startswith(real_hermes), (
            f"{name} 仍然指向真实 ~/.hermes: {value}"
        )
