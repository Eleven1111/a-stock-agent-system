import sys
import os
import tempfile

import pytest

BASE = os.path.dirname(__file__)
PROJ = os.path.abspath(os.path.join(BASE, '..'))
sys.path.insert(0, os.path.join(PROJ, 'skills', 'stock-triage', 'scripts'))
sys.path.insert(0, os.path.join(PROJ, 'skills', 'common'))
sys.path.insert(0, PROJ)

# 必须在此处（模块导入期、任何业务模块被 collection 阶段导入之前）就设置
# 状态根目录环境变量，而不能放进 fixture。部分模块（如
# skills/common/signal_ledger.py 的 LEDGER_FILE）在 import 时就用
# paths.data_file(...) 固化了绝对路径常量；pytest collection 会在任何
# fixture 运行之前导入测试模块及其依赖，fixture 级别的 setenv 生效太晚，
# 无法阻止这些常量指向真实的 ~/.hermes。这里无条件覆盖（而非 setdefault），
# 因为本机开发环境可能已经 export 了指向真实生产目录的 A_STOCK_STATE_HOME。
_TEST_STATE_HOME = tempfile.mkdtemp(prefix="a-stock-test-state-")
os.environ["A_STOCK_STATE_HOME"] = _TEST_STATE_HOME

# backup_home() 优先读 A_STOCK_BACKUP_HOME；未配置时回退到
# "{state_root 同级目录}/{state_root basename}-backups"（相对 hermes_home()
# 动态计算）。这里不能像 A_STOCK_STATE_HOME 那样固定写死一个路径 ——
# 大量测试只 monkeypatch A_STOCK_STATE_HOME 到各自的 tmp_path，期望
# backup_home() 跟随该 tmp_path 动态回退；如果这里把 A_STOCK_BACKUP_HOME
# 固定钉死到会话级临时目录，会导致这些测试的 ledger 备份/恢复机制全部撞车到
# 同一个共享备份目录，产生跨测试数据污染。因此只清理本机 shell 里可能残留的
# 指向真实生产目录的 A_STOCK_BACKUP_HOME（同样不能用 setdefault 兜底判断，
# 必须直接 pop 掉），让 backup_home() 的动态回退逻辑接管 —— 该回退始终基于
# 当时的 hermes_home()，而 hermes_home() 已经被上面强制圈进临时目录，所以
# 默认情况下（没有测试单独覆盖）也不会跑到真实 ~/.hermes-backups。
os.environ.pop("A_STOCK_BACKUP_HOME", None)


@pytest.fixture(autouse=True)
def _reset_monitor_registry_verification_cache():
    """monitor_registry 的账本校验结果缓存在模块级（进程内只校验一次）。

    测试进程是长生命周期的，若不在每个用例前重置，前一个用例留下的「已校验」
    标志会让后一个用例跳过重放校验 —— 构造「投影被篡改 → 期望抛错」的
    fail-closed 用例会静默变绿（假绿）。因此这里把每个用例都还原成
    「新进程首次访问」的状态。
    """
    import monitor_registry

    monitor_registry.reset_verification_cache()
    yield
    monitor_registry.reset_verification_cache()


@pytest.fixture
def verified_gate_factory(tmp_path):
    """Create a gate result backed by a real, verifiable research artifact."""
    from research_artifact import write_artifact

    counter = 0

    def _build(strategy_id, *, allowed=True):
        nonlocal counter
        counter += 1
        source = tmp_path / f"research-input-{counter}.json"
        source.write_text('{"fixture":true}', encoding="utf-8")
        alpha = 0.02 if allowed else -0.01
        metrics = {
            "permutation_p": 0.01,
            "fdr_p": 0.02,
            "oos_alpha": alpha,
            "benchmark_alpha": 0.0,
            "oos_sample_count": 100,
        }
        artifact_path = tmp_path / f"research-artifact-{counter}.json"
        artifact = write_artifact(
            str(artifact_path),
            input_path=str(source),
            strategy_id=strategy_id,
            rules={"version": "fixture-v1"},
            result={"strategy_id": strategy_id, "metrics": metrics},
            gate_metrics=metrics,
            control_counts={"benchmark": 100},
        )
        return {
            "strategy_id": strategy_id,
            "decision": "passed_for_reference" if allowed else "failed",
            "allowed_in_live_agent": allowed,
            "asof": "2026-06-03",
            "stats": metrics,
            "evidence": {
                "verified": True,
                "artifact": str(artifact_path),
                "sha256": artifact["artifact_sha256"],
            },
        }

    return _build
