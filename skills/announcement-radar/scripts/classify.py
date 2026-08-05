"""公告分类（8 大类 / 43 二级分类）与事件阶段识别。

规则（沿用银河金工报告口径）：

* 多标签：一条公告可命中多个二级分类，NSS 先验对命中类别取均值。
  为控制噪声，只保留匹配强度最高的 top-3 类。
* 匹配强度 = 命中关键词的最大长度（更长 = 更具体 = 更可信）。
* exclude 词命中即否决该分类。
* 阶段按 stage_rules.json 的 order 优先级匹配：终止 > 获批 > 完成 > 进展 > 初始（默认）。

纯计算，无 I/O、无网络。上游取数见 ``skills/common/cninfo_client.py``。
"""

from __future__ import annotations

import json
import os
from typing import Any

ASSETS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets"
)

TOP_K = 3


def _dedupe(values: list[str]) -> list[str]:
    """Order-preserving de-duplication.

    Two different level-2 categories can share a level-1 parent, which
    otherwise yields labels like ``公司治理|公司治理`` (92 such rows on
    2026-08-01) and makes level-1 aggregation double-count.
    """
    return list(dict.fromkeys(values))


class Classifier:
    def __init__(self, assets_dir: str = ASSETS, top_k: int = TOP_K):
        with open(os.path.join(assets_dir, "taxonomy.json"), encoding="utf-8") as fh:
            self.tax = json.load(fh)
        with open(os.path.join(assets_dir, "stage_rules.json"), encoding="utf-8") as fh:
            self.stage_rules = json.load(fh)
        self.categories = self.tax["categories"]
        self.stages = sorted(self.stage_rules["stages"], key=lambda s: s["order"])
        self.default_stage = self.stage_rules.get("default", "初始")
        self.top_k = top_k
        self._by_l2 = {c["l2"]: c for c in self.categories}

    def category_of(self, l2: str) -> dict[str, Any] | None:
        return self._by_l2.get(l2)

    def classify(self, title: str) -> dict[str, Any]:
        title = (title or "").strip()
        hits: list[tuple[int, str, str, str]] = []  # (strength, l1, l2, kw)

        for cat in self.categories:
            if any(x and x in title for x in cat.get("exclude", [])):
                continue
            best_kw, best_len = "", 0
            for kw in cat["keywords"]:
                if kw and kw in title and len(kw) > best_len:
                    best_kw, best_len = kw, len(kw)
            if best_len:
                hits.append((best_len, cat["l1"], cat["l2"], best_kw))

        hits.sort(key=lambda x: -x[0])
        hits = hits[: self.top_k]

        return {
            "l1": _dedupe([h[1] for h in hits]),
            "l2": _dedupe([h[2] for h in hits]),
            "matched_kw": _dedupe([h[3] for h in hits]),
            "stage": self.detect_stage(title),
            "unclassified": len(hits) == 0,
        }

    def detect_stage(self, title: str) -> str:
        for st in self.stages:
            if any(x and x in title for x in st.get("exclude", [])):
                continue
            if any(kw and kw in title for kw in st["keywords"]):
                return st["stage"]
        return self.default_stage


__all__ = ["ASSETS", "TOP_K", "Classifier"]
