"""NSS 先验评分 + Bayes NSS 后验评分。

评分口径
--------
NSS 先验：命中的各二级分类先验分取算术平均，四舍五入取整（报告原文规则）。
          定期报告类（年报/半年报/季报/业绩预告/产销快报/ESG）不设先验，
          若一条公告只命中定期报告类，则 skipped=True，不参与后续召回。

Bayes NSS：
  * anchored（默认，无自有行情数据时）
      Bayes = round(clip(w*anchor_c + (1-w)*NSS + stage_adj + industry_adj, -10, 10))
      anchor_c 取自报告表14公开的分类历史均值；无 anchor 的类别 w=0，
      即层次收缩回先验。
  * latent（retrain_bayes.py 训练后启用；本仓库未装 pymc，暂不可用）

**两个分数都不是排序分。** 2026-08-04 实测：anchored 模式下 68~69% 的条目
``bayes == nss``，且 Top20 门槛分上并列 45 只 —— Top-N 名单没有唯一解。
分数在本仓库只用于三桶召回的阈值判定与分歧识别，见 ``radar.py``。

纯计算，无 I/O、无网络。
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(BASE, "assets")


def _logistic(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


class Scorer:
    def __init__(self, assets_dir: str = ASSETS, params_path: str | None = None):
        with open(os.path.join(assets_dir, "taxonomy.json"), encoding="utf-8") as fh:
            tax = json.load(fh)
        self.prior = {
            c["l2"]: c["nss_prior"] for c in tax["categories"]
        }
        self.is_periodic = {
            c["l2"]: (c["type"] == "定期报告") for c in tax["categories"]
        }
        self.l1_of = {c["l2"]: c["l1"] for c in tax["categories"]}

        pol = tax.get("polarity_rules", {})
        self.flip_patterns = [r["pattern"] for r in pol.get("flip", [])]
        self.neg_term_flip = bool(pol.get("negative_termination_flip", True))

        with open(os.path.join(assets_dir, "industry_map.json"), encoding="utf-8") as fh:
            ind = json.load(fh)
        # 申万一级优先（报告口径），东财/国民经济行业分类兜底：本仓库的
        # industry_map.load_cached() 返回的是后者，只查 sw_to_group 会让
        # 92% 的个股退化为「其他」。两表键无重叠冲突。
        self.ind_map = ind["sw_to_group"]
        self.ind_map_fallback = ind.get("em_to_group", {})

        path = params_path or os.path.join(assets_dir, "bayes_params.json")
        trained = os.path.join(assets_dir, "bayes_params_trained.json")
        if params_path is None and os.path.exists(trained):
            path = trained
        with open(path, encoding="utf-8") as fh:
            self.params = json.load(fh)
        self.params_path = path
        self.mode = self.params.get("_meta", {}).get("mode", "anchored")

        a = self.params["anchored"]
        self.anchor_weight = a["anchor_weight"]
        self.anchors = a["category_anchor"][a["category_anchor_set"]]
        self.stage_adj = {k: v for k, v in a["stage_adj"].items() if not k.startswith("_")}
        self.industry_adj = {k: v for k, v in a["industry_adj"].items() if not k.startswith("_")}

        self.latent = self.params.get("latent", {})
        self.calib = self.params.get("quantile_calibration", {})

    # ------------------------------------------------------------------ #
    def industry_group(self, industry: str | None) -> str:
        """行业名 → 科技/消费/周期/其他。接受申万一级或东财/国民经济分类名。"""
        if not industry:
            return "其他"
        name = industry.strip()
        if name in self.ind_map:
            return self.ind_map[name]
        return self.ind_map_fallback.get(name, "其他")

    def knows_industry(self, industry: str | None) -> bool:
        """行业名是否在映射表里。

        与 :meth:`industry_group` 分开，是因为「金融行业」等确实归入「其他」，
        用 ``group != '其他'`` 度量覆盖率会把它们误记成未识别，
        从而掩盖真正的映射缺口。
        """
        if not industry:
            return False
        name = industry.strip()
        return name in self.ind_map or name in self.ind_map_fallback

    def nss_prior(self, l2_list: list[str]) -> tuple[int | None, list[str]]:
        """返回 (先验分, 参与打分的类别)。全为定期报告则返回 (None, [])。"""
        scoring = [x for x in l2_list if x in self.prior and not self.is_periodic.get(x)]
        if not scoring:
            return None, []
        vals = [self.prior[x] for x in scoring if self.prior[x] is not None]
        if not vals:
            return None, []
        return int(round(sum(vals) / len(vals))), scoring

    # ------------------------------------------------------------------ #
    def _stage_effect(self, stage: str, nss: int, flipped: bool = False) -> float:
        adj = self.stage_adj.get(stage, 0.0)
        # 负面事项的『终止』是好事：终止破产重整、终止减持计划、撤回诉讼
        # flipped 标记为 True 意味着标题命中极性反转模式（如「终止减持」），
        # 此时即使 NSS=0（如股份增减持为中性），终止也应视为利好
        if self.neg_term_flip and stage == "终止" and (nss < 0 or flipped):
            adj = -adj
        return adj

    def _bayes_anchored(self, nss: int, l2_scoring: list[str], stage: str, grp: str, flipped: bool) -> float:
        anchored_vals, plain_vals = [], []
        sign = -1.0 if flipped else 1.0
        for l2 in l2_scoring:
            if l2 in self.anchors:
                anchored_vals.append(
                    sign
                    * (
                        self.anchor_weight * self.anchors[l2]
                        + (1 - self.anchor_weight) * self.prior[l2]
                    )
                )
            else:
                plain_vals.append(sign * float(self.prior[l2]))
        vals = anchored_vals + plain_vals
        base = sum(vals) / len(vals) if vals else float(nss)
        return base + self._stage_effect(stage, nss, flipped) + self.industry_adj.get(grp, 0.0)

    def _bayes_latent(self, nss: int, l2_scoring: list[str], stage: str, grp: str) -> float:
        L = self.latent
        theta_c = L.get("theta_category", {})
        theta_s = L.get("theta_stage", {})
        theta_i = L.get("theta_industry", {})
        tc = (
            sum(theta_c.get(x, 0.0) for x in l2_scoring) / len(l2_scoring)
            if l2_scoring
            else 0.0
        )
        mu = (
            L["beta"] * nss / 10.0
            + tc
            + theta_s.get(stage, 0.0)
            + theta_i.get(grp, 0.0)
        )
        p1 = _logistic(L["cut1"] - mu)
        p2 = _logistic(L["cut2"] - mu) - p1
        p3 = 1.0 - _logistic(L["cut2"] - mu)
        raw = 1 * p1 + 2 * p2 + 3 * p3  # ∈ [1,3]
        return self._calibrate(raw)

    def _calibrate(self, raw: float) -> float:
        """分位数校准：把 [1,3] 的期望档位映射回 NSS 的取值分布。"""
        rq = self.calib.get("raw_quantiles")
        nq = self.calib.get("nss_quantiles")
        if not rq or not nq:
            # 未训练校准表时退回线性放缩（报告指出这样分布过窄，仅作兜底）
            return 10.0 * (raw - 2.0)
        if raw <= rq[0]:
            return float(nq[0])
        if raw >= rq[-1]:
            return float(nq[-1])
        for i in range(1, len(rq)):
            if raw <= rq[i]:
                x0, x1 = rq[i - 1], rq[i]
                y0, y1 = nq[i - 1], nq[i]
                t = 0.0 if x1 == x0 else (raw - x0) / (x1 - x0)
                return y0 + t * (y1 - y0)
        return float(nq[-1])

    # ------------------------------------------------------------------ #
    def score(
        self,
        l2_list: list[str],
        stage: str = "初始",
        industry_group: str = "其他",
        title: str = "",
    ) -> dict[str, Any]:
        nss, scoring = self.nss_prior(l2_list)
        if nss is None:
            return {
                "nss_prior": None,
                "bayes_nss": None,
                "skipped": True,
                "skip_reason": "仅命中定期报告类，报告口径不参与 NSS/Bayes 评分",
            }

        flipped = bool(title) and any(p in title for p in self.flip_patterns)
        if flipped:
            nss = -nss

        if self.mode == "latent" and self.latent.get("theta_category"):
            raw = self._bayes_latent(nss, scoring, stage, industry_group)
            if flipped:
                raw = -raw
        else:
            raw = self._bayes_anchored(nss, scoring, stage, industry_group, flipped)

        bayes = int(round(_clip(raw, -10, 10)))
        return {
            "nss_prior": nss,
            "bayes_nss": bayes,
            "bayes_raw": round(raw, 3),
            "divergence": bayes - nss,
            "polarity_flipped": flipped,
            "skipped": False,
            "scoring_categories": scoring,
        }


__all__ = ["ASSETS", "Scorer"]
