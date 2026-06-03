"""
共享路径解析模块 — 解耦 ~/.hermes 硬编码
========================================
所有运行时数据/缓存/配置路径统一走此模块，通过 HERMES_HOME 环境变量可重定向，
默认回退到 ~/.hermes。这样脚本可在仓库内、测试沙箱、CI 中独立运行，
不再强绑定到部署机的 home 目录。

用法:
    from paths import data_file, hermes_home, env_file
    HISTORY = data_file("stock-triage", "signal_history.json")
    # → $HERMES_HOME/skills/stock-triage/data/signal_history.json
"""

import os


def hermes_home() -> str:
    """Hermes 根目录。优先 $HERMES_HOME，否则 ~/.hermes。"""
    return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def env_file() -> str:
    """.env 文件路径。"""
    return os.path.join(hermes_home(), ".env")


def skill_data_dir(skill: str) -> str:
    """某个 skill 的运行时数据目录。"""
    return os.path.join(hermes_home(), "skills", skill, "data")


def data_file(skill: str, filename: str) -> str:
    """某个 skill 的运行时数据文件路径。"""
    return os.path.join(skill_data_dir(skill), filename)


def cache_dir(skill: str) -> str:
    """某个 skill 的缓存目录。"""
    return os.path.join(hermes_home(), "skills", skill, "cache")


def cron_output_dir() -> str:
    """cron 输出目录。"""
    return os.path.join(hermes_home(), "cron", "output")
