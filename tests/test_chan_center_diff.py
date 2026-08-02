"""差分测试：chan_center（生产实现）vs chan.py oracle（third_party/chan_py_reference）。

对齐口径
--------
- 笔/线段输入两侧同源同配置（strict 分型检查、严格笔、逐K推进），见 test_chan_kline_diff /
  test_chan_segment_diff；本文件复用后者同一批三组 1000 根合成K线（trend/range/gap，同 seed）。
- 比较对象：**确定中枢（is_sure=True）** 的 `(起点日期, 终点日期)` 为键、`(zg, zd)` 为值。
  日期取中枢首笔起点 / 末笔终点对应的原始K线（oracle 侧为 `zs.begin.time` / `zs.end.time`）。
- 价格用相对误差 1e-6 比较（两侧都是同一批合成价的浮点组合，容差只吸收求 min/max 的运算差）。
- 对齐率 = |两侧同键且价格相等| / |键的并集|（分母用并集，任何一侧多出的中枢都算不齐）。逐组阈值 90%。
- 另断言每组 oracle 确定中枢数 > 0：空集会让对齐率恒为 1.0，是假绿。

白名单
------
**当前白名单为空（0 条）。** 三组用例实测对齐率均为 1.0000。若将来出现明确源于规则差异的
不一致中枢，在此处逐条登记（规则出处 + 差异原因）并加入 `_WHITELIST`：白名单键在 A、B 两侧
同时剔除，**既不计入分子也不计入分母**。
"""

import importlib.util
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

from offline_driver import SyntheticBar, run_offline_structure  # noqa: E402

from test_chan_kline_diff import ck, make_gap, make_range, make_trend  # noqa: E402

SCRIPTS = PROJ / "skills" / "chanlun-backtest" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cc = _load("chan_center")
cs = _load("chan_segment")

ALIGN_THRESHOLD = 0.90
PRICE_RTOL = 1e-6
BARS_PER_GROUP = 1000         # 与 test_chan_segment_diff 同口径：320 根时确定结构样本过少
_WHITELIST: set = set()       # 见文件头"白名单"一节；当前为空

GROUPS = (("trend", make_trend(n=BARS_PER_GROUP)),
          ("range", make_range(n=BARS_PER_GROUP)),
          ("gap", make_gap(n=BARS_PER_GROUP)))


# ========== 两侧执行与比较 ==========

def _mine(bars):
    bis = ck.build_bis(bars, ck.BiConfig(fx_check="strict"))
    return bis, cc.build_centers(bis, cs.build_segs(bis))


def _oracle(bars):
    syn = [SyntheticBar(date=b["date"], open=b["open"], high=b["high"],
                        low=b["low"], close=b["close"]) for b in bars]
    return run_offline_structure(syn).zs_records


def _mine_map(bars, centers, sure_only=True):
    return {(bars[c["start_idx"]]["date"], bars[c["end_idx"]]["date"]): (c["zg"], c["zd"])
            for c in centers if c["is_sure"] or not sure_only
            if (bars[c["start_idx"]]["date"], bars[c["end_idx"]]["date"]) not in _WHITELIST}


def _oracle_map(records, sure_only=True):
    # chan.py 的 CTime.__str__ 形如 "2020/01/01"，统一成 ISO 日期
    return {(r.begin.replace("/", "-"), r.end.replace("/", "-")): (r.zg, r.zd)
            for r in records if r.is_sure or not sure_only
            if (r.begin.replace("/", "-"), r.end.replace("/", "-")) not in _WHITELIST}


def _price_eq(a, b):
    return all(abs(x - y) <= PRICE_RTOL * max(abs(x), abs(y), 1.0) for x, y in zip(a, b))


def _report(name, mine, oracle, scope):
    matched = {k for k in mine.keys() & oracle.keys() if _price_eq(mine[k], oracle[k])}
    union = mine.keys() | oracle.keys()
    rate = len(matched) / len(union) if union else 1.0
    price_mismatch = sorted(mine.keys() & oracle.keys() - matched)
    return rate, (
        f"[{name}/{scope}] 对齐率={rate:.4f} 阈值={ALIGN_THRESHOLD} | "
        f"本实现={len(mine)} oracle={len(oracle)} 命中={len(matched)} 并集={len(union)} | "
        f"仅本实现有={sorted(mine.keys() - oracle.keys())} "
        f"仅oracle有={sorted(oracle.keys() - mine.keys())} 同区间但zg/zd不等={price_mismatch} | "
        f"白名单条目={len(_WHITELIST)}（白名单键在两侧同时剔除，不计入分子也不计入分母）"
    )


def test_sure_center_ranges_align_per_group():
    """逐组：确定中枢 (zg, zd, 起止日期) 对齐率 ≥90%，且 oracle 确定中枢数 >0（防空集假绿）。"""
    for name, bars in GROUPS:
        bis, centers = _mine(bars)
        oracle = _oracle_map(_oracle(bars))
        mine = _mine_map(bars, centers)
        rate, msg = _report(name, mine, oracle, "确定中枢")
        print(msg)
        assert oracle, f"[{name}] oracle 没有确定中枢，对齐率无意义：{msg}"
        assert rate >= ALIGN_THRESHOLD, msg


def test_all_center_ranges_align_per_group():
    """含未确认中枢（尾部未成段区间）同样对齐：point-in-time 语义一致，而非只算对了确定中枢。"""
    for name, bars in GROUPS:
        bis, centers = _mine(bars)
        records = _oracle(bars)
        mine = _mine_map(bars, centers, sure_only=False)
        oracle = _oracle_map(records, sure_only=False)
        rate, msg = _report(name, mine, oracle, "全部中枢(含未确认)")
        print(msg)
        assert oracle, f"[{name}] oracle 没有中枢，对齐率无意义：{msg}"
        assert rate >= ALIGN_THRESHOLD, msg


def test_center_bi_index_ranges_match_oracle():
    """中枢的笔区间（start_bi_idx/end_bi_idx）逐条与 oracle 一致——区间日期相同但
    起止笔不同会被这条抓住。"""
    for name, bars in GROUPS:
        _, centers = _mine(bars)
        mine = [(c["start_bi_idx"], c["end_bi_idx"], c["is_sure"]) for c in centers]
        oracle = [(r.start_bi_idx, r.end_bi_idx, r.is_sure) for r in _oracle(bars)]
        assert mine == oracle, f"[{name}] 中枢笔区间不一致：\n本实现={mine}\noracle={oracle}"
