"""差分测试：chan_kline（生产实现）vs chan.py oracle（third_party/chan_py_reference）。

对齐口径
--------
- 两侧配置对齐：strict 分型检查、严格笔（跨度>=4）、gap_as_kl=False、bi_end_is_peak=True、
  bi_allow_sub_peak=True、逐K推进（trigger_step / cal_virtual=True）。
  即 chan.py `CChanConfig` 的默认笔档位（ChanConfig.py 第 21-28 行）。
- 比较对象：**确定笔（is_sure=True）** 的端点三元组 `(方向, 起点日期, 终点日期)`。
  日期取自笔端点对应的原始K线（chan.py 侧为 `bi.get_begin_klu()/get_end_klu()`，
  本侧为 start_idx/end_idx 回查 bars），因此比较的是"同一根原始K线"而非合并K线序号。
- 对齐率 = |A ∩ B| / |A ∪ B|（A=本实现，B=oracle）。分母用并集：任何一侧多出的笔都算不齐，
  不会因为一侧笔更少而虚高。逐组阈值 95%。

白名单
------
**当前白名单为空（0 条）。** 三组用例实测对齐率均为 1.0000，无需白名单化的规则差异。
若将来出现明确源于规则差异的不一致端点，在此处逐条登记（规则出处 + 差异原因），并加入
`_WHITELIST`：白名单端点会在计算前从 A、B 两侧同时剔除，**因此既不计入分子也不计入分母**
（分母是剔除后的并集），登记时必须写明为何该差异是规则性的而非实现缺陷。
"""

import datetime
import importlib.util
import math
import os
import random
import sys
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parents[1]
# chan.py 参考实现（差分 oracle）使用 typing.Self，要求 Python 3.11+；
# 生产侧 chan_* 模块不依赖它，仍支持 3.10。低版本上跳过差分校验而非 collection 报错。
if sys.version_info < (3, 11):  # pragma: no cover - 版本相关分支
    pytest.skip("chan.py 参考实现需要 Python 3.11+（typing.Self）", allow_module_level=True)

REFERENCE_ROOT = str(PROJ / "third_party" / "chan_py_reference")
if REFERENCE_ROOT not in sys.path:
    sys.path.insert(0, REFERENCE_ROOT)

from offline_driver import SyntheticBar, run_offline  # noqa: E402

SCRIPT = PROJ / "skills" / "chanlun-backtest" / "scripts" / "chan_kline.py"
SPEC = importlib.util.spec_from_file_location("chan_kline", SCRIPT)
ck = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ck)

ALIGN_THRESHOLD = 0.95
BARS_PER_GROUP = 320          # ≥300 根
START_DATE = datetime.date(2020, 1, 1)
_WHITELIST: set = set()       # 见文件头"白名单"一节；当前为空


# ========== 合成K线（固定 seed，三种形态）==========

def _bar(idx, price, spread, rng, gap=0.0):
    open_px = price + gap
    close_px = open_px + rng.uniform(-spread, spread)
    return {
        "date": (START_DATE + datetime.timedelta(days=idx)).isoformat(),
        "open": round(open_px, 2),
        "high": round(max(open_px, close_px) + abs(rng.gauss(0, spread)), 2),
        "low": round(min(open_px, close_px) - abs(rng.gauss(0, spread)), 2),
        "close": round(close_px, 2),
    }


def make_trend(n=BARS_PER_GROUP, seed=20260801):
    """趋势形态：正漂移几何随机游走。"""
    rng = random.Random(seed)
    bars, price = [], 20.0
    for i in range(n):
        price = max(2.0, price * (1 + rng.gauss(0.0025, 0.018)))
        bars.append(_bar(i, price, price * 0.012, rng))
    return bars


def make_range(n=BARS_PER_GROUP, seed=20260802):
    """震荡形态：双频正弦 + 噪声，制造大量重叠与包含关系。"""
    rng = random.Random(seed)
    return [_bar(i, 30.0 + 4.0 * math.sin(i / 9.0) + 1.5 * math.sin(i / 2.7) + rng.gauss(0, 0.35),
                 0.3, rng)
            for i in range(n)]


def make_gap(n=BARS_PER_GROUP, seed=20260803):
    """跳空形态：每 23 根注入一次 ±9% 跳空（缺口处理是两侧最易分歧的地方）。"""
    rng = random.Random(seed)
    bars, price = [], 50.0
    for i in range(n):
        price = max(5.0, price * (1 + rng.gauss(0.0, 0.02)))
        gap = price * rng.choice([-0.09, 0.09]) if (i and i % 23 == 0) else 0.0
        price += gap
        bars.append(_bar(i, price, price * 0.01, rng))
    return bars


GROUPS = (("trend", make_trend()), ("range", make_range()), ("gap", make_gap()))


# ========== 两侧执行与比较 ==========

def _mine(bars, config=None):
    return ck.build_bis(bars, config or ck.BiConfig(fx_check="strict"))


def _oracle(bars, overrides=None):
    syn = [SyntheticBar(date=b["date"], open=b["open"], high=b["high"],
                        low=b["low"], close=b["close"]) for b in bars]
    return run_offline(syn, overrides)[0]


def _mine_set(bars, bis, sure_only=True):
    return {(b["dir"], bars[b["start_idx"]]["date"], bars[b["end_idx"]]["date"])
            for b in bis if b["is_sure"] or not sure_only} - _WHITELIST


def _oracle_set(records, sure_only=True):
    # chan.py 的 CTime.__str__ 形如 "2020/01/01"，统一成 ISO 日期
    return {(r.dir.lower(), r.begin.replace("/", "-"), r.end.replace("/", "-"))
            for r in records if r.is_sure or not sure_only} - _WHITELIST


def _rate(mine, oracle):
    union = mine | oracle
    return (len(mine & oracle) / len(union) if union else 1.0), len(union)


def _report(name, mine, oracle, sure_only):
    rate, union = _rate(mine, oracle)
    scope = "确定笔" if sure_only else "全部笔(含虚笔)"
    return rate, (
        f"[{name}/{scope}] 对齐率={rate:.4f} 阈值={ALIGN_THRESHOLD} | "
        f"本实现={len(mine)} oracle={len(oracle)} 交集={len(mine & oracle)} 并集={union} | "
        f"仅本实现有={sorted(mine - oracle)} 仅oracle有={sorted(oracle - mine)} | "
        f"白名单条目={len(_WHITELIST)}（白名单端点在两侧同时剔除，不计入分子也不计入分母）"
    )


def test_sure_bi_endpoints_align_per_group():
    """逐组：确定笔端点对齐率 ≥95%。原始数字与对齐率写进断言消息并打印。"""
    lines = []
    for name, bars in GROUPS:
        assert len(bars) >= 300, f"{name} 组仅 {len(bars)} 根K线，少于 300 根"
        mine = _mine_set(bars, _mine(bars))
        oracle = _oracle_set(_oracle(bars))
        rate, msg = _report(name, mine, oracle, sure_only=True)
        lines.append(msg)
        print(msg)
        assert rate >= ALIGN_THRESHOLD, msg
    print("\n".join(lines))


def test_virtual_bi_state_also_aligns():
    """虚笔（is_sure=False）同样对齐：point-in-time 语义与 oracle 一致，而非只算对了确定笔。"""
    for name, bars in GROUPS:
        bis = _mine(bars)
        records = _oracle(bars)
        mine_all = _mine_set(bars, bis, sure_only=False)
        oracle_all = _oracle_set(records, sure_only=False)
        rate, msg = _report(name, mine_all, oracle_all, sure_only=False)
        print(msg)
        assert rate >= ALIGN_THRESHOLD, msg
        mine_virtual = sum(1 for b in bis if not b["is_sure"])
        oracle_virtual = sum(1 for r in records if not r.is_sure)
        assert mine_virtual == oracle_virtual, \
            f"[{name}] 虚笔条数不一致：本实现={mine_virtual} oracle={oracle_virtual}"


def test_fx_check_variants_align_on_range_group():
    """4 档分型有效性检查逐档与 oracle 对齐（震荡组，分歧最多）。"""
    bars = dict(GROUPS)["range"]
    for method in ck.FX_CHECK_METHODS:
        mine = _mine_set(bars, _mine(bars, ck.BiConfig(fx_check=method)))
        oracle = _oracle_set(_oracle(bars, {"bi_fx_check": method}))
        rate, msg = _report(f"range/fx_check={method}", mine, oracle, sure_only=True)
        print(msg)
        assert rate >= ALIGN_THRESHOLD, msg


def test_reference_oracle_is_test_only():
    """守卫：本文件（tests/）可以 import oracle，生产目录不行 —— 由
    tests/test_chan_reference_guard.py 静态断言，这里只确认 oracle 路径没被塞进生产包。"""
    assert os.path.isdir(REFERENCE_ROOT)
    assert not (PROJ / "skills" / "chanlun-backtest" / "scripts" / "chan_py_reference").exists()
