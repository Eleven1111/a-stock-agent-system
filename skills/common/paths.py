"""
共享路径解析模块 — 解耦 ~/.hermes 硬编码
========================================
所有运行时数据/缓存/配置路径统一走此模块，通过 A_STOCK_STATE_HOME 或
HERMES_HOME 环境变量可重定向，默认回退到 ~/.hermes。Hermes 与 OpenClaw
可显式共用同一份持仓、推荐和监控状态。

用法:
    from paths import data_file, hermes_home, env_file, hermes_python
    HISTORY = data_file("stock-triage", "signal_history.json")
    # → $HERMES_HOME/skills/stock-triage/data/signal_history.json
"""

import os


def hermes_home() -> str:
    """运行时状态根目录。A_STOCK_STATE_HOME 可跨 Agent 共享状态。"""
    return (
        os.environ.get("A_STOCK_STATE_HOME")
        or os.environ.get("HERMES_HOME")
        or os.path.expanduser("~/.hermes")
    )


def hermes_install_home() -> str:
    """Hermes 安装目录；与可共享的 A 股状态目录分离。"""
    return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def env_file() -> str:
    """.env 文件路径。"""
    return os.path.join(hermes_install_home(), ".env")


def hermes_python() -> str:
    """Hermes Python 路径。优先 $HERMES_PYTHON，否则随 $HERMES_HOME 重定向。"""
    return (
        os.environ.get("HERMES_PYTHON")
        or os.path.join(hermes_install_home(), "hermes-agent", "venv", "bin", "python3")
    )


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
