"""差分测试：chan_segment（生产实现）vs chan.py oracle（third_party/chan_py_reference）。

对齐口径
--------
- 笔层输入两侧同源同配置（strict 分型检查、严格笔、逐K推进），见 test_chan_kline_diff；
  本文件直接复用它的三组合成K线生成器（trend/range/gap，同 seed），只把长度调大：
  320 根K线只产出 13~23 笔 / 0~2 条**确定线段**，确定线段对齐率会退化成空集恒 1.0 的
  假绿；1000 根K线产出 41~71 笔 / 4~14 条确定线段，指标才有分辨力。
- 比较对象：**确定线段（is_sure=True）** 的端点三元组 `(方向, 起点日期, 终点日期)`，
  日期取自线段起止笔端点对应的原始K线（oracle 侧为 `seg.get_begin_klu()/get_end_klu()`）。
- 对齐率 = |A ∩ B| / |A ∪ B|（分母用并集，任何一侧多出的线段都算不齐）。逐组阈值 90%。

白名单
------
**当前白名单为空（0 条）。** 三组用例实测对齐率均为 1.0000（确定线段与全部线段皆然），
无需白名单化的规则差异。若将来出现明确源于规则差异的不一致端点，在此处逐条登记
（规则出处 + 差异原因）并加入 `_WHITELIST`：白名单端点在 A、B 两侧同时剔除，
**既不计入分子也不计入分母**。
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

SCRIPT = PROJ / "skills" / "chanlun-backtest" / "scripts" / "chan_segment.py"
SPEC = importlib.util.spec_from_file_location("chan_segment", SCRIPT)
cs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cs)

ALIGN_THRESHOLD = 0.90
BARS_PER_GROUP = 1000         # 见文件头：320 根时确定线段样本过少，指标失去分辨力
_WHITELIST: set = set()       # 见文件头"白名单"一节；当前为空

GROUPS = (("trend", make_trend(n=BARS_PER_GROUP)),
          ("range", make_range(n=BARS_PER_GROUP)),
          ("gap", make_gap(n=BARS_PER_GROUP)))


# ========== 两侧执行与比较 ==========

def _mine(bars):
    return cs.build_segs(ck.build_bis(bars, ck.BiConfig(fx_check="strict")))


def _oracle(bars):
    syn = [SyntheticBar(date=b["date"], open=b["open"], high=b["high"],
                        low=b["low"], close=b["close"]) for b in bars]
    return run_offline_structure(syn).seg_records


def _mine_set(bars, segs, sure_only=True):
    return {(s["dir"], bars[s["start_idx"]]["date"], bars[s["end_idx"]]["date"])
            for s in segs if s["is_sure"] or not sure_only} - _WHITELIST


def _oracle_set(records, sure_only=True):
    # chan.py 的 CTime.__str__ 形如 "2020/01/01"，统一成 ISO 日期
    return {(r.dir.lower(), r.begin.replace("/", "-"), r.end.replace("/", "-"))
            for r in records if r.is_sure or not sure_only} - _WHITELIST


def _report(name, mine, oracle, sure_only):
    union = mine | oracle
    rate = len(mine & oracle) / len(union) if union else 1.0
    scope = "确定线段" if sure_only else "全部线段(含未确认)"
    return rate, (
        f"[{name}/{scope}] 对齐率={rate:.4f} 阈值={ALIGN_THRESHOLD} | "
        f"本实现={len(mine)} oracle={len(oracle)} 交集={len(mine & oracle)} 并集={len(union)} | "
        f"仅本实现有={sorted(mine - oracle)} 仅oracle有={sorted(oracle - mine)} | "
        f"白名单条目={len(_WHITELIST)}（白名单端点在两侧同时剔除，不计入分子也不计入分母）"
    )


def test_sure_seg_endpoints_align_per_group():
    """逐组：确定线段端点对齐率 ≥90%。原始数字与对齐率写进断言消息并打印。"""
    for name, bars in GROUPS:
        mine = _mine_set(bars, _mine(bars))
        oracle = _oracle_set(_oracle(bars))
        rate, msg = _report(name, mine, oracle, sure_only=True)
        print(msg)
        assert oracle, f"[{name}] oracle 没有确定线段，对齐率无意义：{msg}"
        assert rate >= ALIGN_THRESHOLD, msg


def test_all_seg_endpoints_align_per_group():
    """未确认线段（左侧收尾段）同样对齐：point-in-time 语义一致，而非只算对了确定段。"""
    for name, bars in GROUPS:
        segs = _mine(bars)
        records = _oracle(bars)
        mine = _mine_set(bars, segs, sure_only=False)
        oracle = _oracle_set(records, sure_only=False)
        rate, msg = _report(name, mine, oracle, sure_only=False)
        print(msg)
        assert rate >= ALIGN_THRESHOLD, msg
        mine_unsure = sum(1 for s in segs if not s["is_sure"])
        oracle_unsure = sum(1 for r in records if not r.is_sure)
        assert mine_unsure == oracle_unsure, \
            f"[{name}] 未确认线段条数不一致：本实现={mine_unsure} oracle={oracle_unsure}"


def test_seg_bi_index_ranges_match_oracle():
    """线段的笔区间（start_bi_idx/end_bi_idx）逐条与 oracle 一致——端点日期相同但
    切在不同笔上会被这条抓住。"""
    for name, bars in GROUPS:
        mine = [(s["dir"], s["start_bi_idx"], s["end_bi_idx"], s["is_sure"]) for s in _mine(bars)]
        oracle = [(r.dir.lower(), r.start_bi_idx, r.end_bi_idx, r.is_sure) for r in _oracle(bars)]
        assert mine == oracle, f"[{name}] 线段笔区间不一致：\n本实现={mine}\noracle={oracle}"
