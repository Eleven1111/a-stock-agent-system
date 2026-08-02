"""差分测试：chan_bsp（生产实现）vs chan.py oracle（third_party/chan_py_reference）。

对齐口径
--------
- 笔/线段/中枢输入两侧同源同配置（strict 分型检查、严格笔、逐K推进），见
  test_chan_kline_diff / test_chan_segment_diff / test_chan_center_diff；本文件复用同一批
  三组 1000 根合成K线（trend/range/gap，同 seed）。
- 买卖点配置两侧都用默认档：oracle 的 `bs_type` 默认 `"1,1p,2,2s,3a,3b"` 全开，
  本实现 `BspConfig.bs_type` 默认同样是六类全开，其余 13 个参数逐项对齐
  `ChanConfig.py::set_bsp_config` 的 para_dict。
- 比较对象：**确定笔上的买卖点**（锚定笔 is_sure=True），键为
  `(bsp_type, is_buy, 锚定笔终点日期)`。oracle 侧一个买卖点可挂多个类型，逐类型展开。
- 对齐率 = |A ∩ B| / |A ∪ B|（分母用并集，任何一侧多出的买卖点都算不齐）。逐组阈值 90%。
- 另断言每组 oracle 确定买卖点数 > 0：空集会让对齐率恒为 1.0，是假绿。

潜在口径差异（阈值留 10% 余量的原因）
-----------------------------------
chan.py 的 `CBSPointList.cal` 是**增量**计算：`last_sure_pos`（最后一条确定线段末笔的起点
K线）之前的买卖点由历史步骤沉淀、之后不再重算（`clear_store_end` + `seg_need_cal`）；
本实现是纯函数，对最终的笔/线段/中枢列表整体重算。确定结构上两者等价，但理论上存在这样
的点：某一步的笔列表比最终列表短，二类/三类这类"要看后续几笔"的判据当时因笔不存在而直接
return（`bsp1_bi.idx+2 >= len(bi_list)`），其后该线段落到 last_sure_pos 之前不再重算——
这类点会只在 oracle 侧缺失。三组用例实测未出现（对齐率均 1.0000），阈值保留余量以覆盖它。

白名单
------
**当前白名单为空（0 条）。** 三组用例实测对齐率均为 1.0000。若将来出现明确源于规则差异的
不一致买卖点，在此处逐条登记（规则出处 + 差异原因）并加入 `_WHITELIST`：白名单键在 A、B
两侧同时剔除，**既不计入分子也不计入分母**。
"""

import importlib.util
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
REFERENCE_ROOT = str(PROJ / "third_party" / "chan_py_reference")
if REFERENCE_ROOT not in sys.path:
    sys.path.insert(0, REFERENCE_ROOT)

from indicators import macd_hist  # noqa: E402  conftest 已把 skills/common 放进 sys.path
from offline_driver import SyntheticBar, run_offline_structure  # noqa: E402

from test_chan_kline_diff import ck, make_gap, make_range, make_trend  # noqa: E402

SCRIPTS = PROJ / "skills" / "chanlun-backtest" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cs = _load("chan_segment")
cc = _load("chan_center")
cb = _load("chan_bsp")

ALIGN_THRESHOLD = 0.90
BARS_PER_GROUP = 1000         # 与前序差分测试同口径：320 根时确定结构样本过少
_WHITELIST: set = set()       # 见文件头"白名单"一节；当前为空

GROUPS = (("trend", make_trend(n=BARS_PER_GROUP)),
          ("range", make_range(n=BARS_PER_GROUP)),
          ("gap", make_gap(n=BARS_PER_GROUP)))


# ========== 两侧执行与比较 ==========

def _mine(bars):
    bis = ck.build_bis(bars, ck.BiConfig(fx_check="strict"))
    segs = cs.build_segs(bis)
    centers = cc.build_centers(bis, segs)
    hist = macd_hist([b["close"] for b in bars])
    return bis, cb.build_bsps(bis, segs, centers, bars, hist)


def _oracle(bars):
    syn = [SyntheticBar(date=b["date"], open=b["open"], high=b["high"],
                        low=b["low"], close=b["close"]) for b in bars]
    return run_offline_structure(syn).bsp_records


def _mine_set(bars, bsps, sure_only=True):
    return {(t, b["is_buy"], bars[b["idx"]]["date"])
            for b in bsps if b["is_sure"] or not sure_only
            for t in b["types"]} - _WHITELIST


def _oracle_set(records, sure_only=True):
    # chan.py 的 CTime.__str__ 形如 "2020/01/01"，统一成 ISO 日期
    return {(t, r.is_buy, r.time.replace("/", "-"))
            for r in records if r.is_sure or not sure_only
            for t in r.types} - _WHITELIST


def _report(name, mine, oracle, scope):
    union = mine | oracle
    rate = len(mine & oracle) / len(union) if union else 1.0
    return rate, (
        f"[{name}/{scope}] 对齐率={rate:.4f} 阈值={ALIGN_THRESHOLD} | "
        f"本实现={len(mine)} oracle={len(oracle)} 交集={len(mine & oracle)} 并集={len(union)} | "
        f"仅本实现有={sorted(mine - oracle)} 仅oracle有={sorted(oracle - mine)} | "
        f"白名单条目={len(_WHITELIST)}（白名单键在两侧同时剔除，不计入分子也不计入分母）"
    )


def test_sure_bsp_align_per_group():
    """逐组：确定笔上的买卖点 (类型, 买卖方向, 端点日期) 对齐率 ≥90%，
    且 oracle 确定买卖点数 >0（防空集假绿）。"""
    for name, bars in GROUPS:
        _, bsps = _mine(bars)
        mine = _mine_set(bars, bsps)
        oracle = _oracle_set(_oracle(bars))
        rate, msg = _report(name, mine, oracle, "确定笔买卖点")
        print(msg)
        assert oracle, f"[{name}] oracle 没有确定笔买卖点，对齐率无意义：{msg}"
        assert rate >= ALIGN_THRESHOLD, msg


def test_bsp_type_coverage_matches_oracle():
    """六类买卖点的类型覆盖面一致：本实现产出的类型集合不应少于 oracle 在确定笔上产出的类型。"""
    for name, bars in GROUPS:
        _, bsps = _mine(bars)
        mine_types = {t for _, _, t in ((None, None, k[0]) for k in _mine_set(bars, bsps))}
        oracle_types = {k[0] for k in _oracle_set(_oracle(bars))}
        print(f"[{name}] 本实现类型={sorted(mine_types)} oracle类型={sorted(oracle_types)}")
        assert oracle_types <= mine_types, \
            f"[{name}] 本实现缺失 oracle 有的买卖点类型：{sorted(oracle_types - mine_types)}"


def test_feature_keys_match_oracle():
    """feature_dict 键名与参考实现同名：逐类型比对本实现与 oracle 的特征键集合。"""
    for name, bars in GROUPS:
        _, bsps = _mine(bars)
        oracle_keys = {}
        for rec in _oracle(bars):
            for t in rec.types:
                oracle_keys.setdefault(t, set()).update(rec.features.keys())
        mine_keys = {}
        for bsp in bsps:
            for t in bsp["types"]:
                mine_keys.setdefault(t, set()).update(bsp["feature_dict"].keys())
        for bsp_type, keys in oracle_keys.items():
            if bsp_type not in mine_keys:
                continue
            missing = keys - mine_keys[bsp_type]
            print(f"[{name}/{bsp_type}] oracle键={sorted(keys)} 本实现键={sorted(mine_keys[bsp_type])}")
            assert not missing, f"[{name}/{bsp_type}] feature_dict 缺少 oracle 的键：{sorted(missing)}"
