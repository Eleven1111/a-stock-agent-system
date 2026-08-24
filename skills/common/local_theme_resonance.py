"""Local sector-theme resonance gate (issue #260).

Pure functions only: callers supply already-captured stage evidence (sector
cross-section from ``hot_money_selection``, per-stage risk review outcome).
Nothing here fetches data or mutates the market/leader gates it is layered on
top of — it only decides whether a *sector-local* theme has earned an
observation or conditional-execution admission that is independent of the
market-wide new-risk gate (``hot_money_selection.build_market_gate``).

Evidence taxonomy (issue #260 §3.2):
- structural (at least one required): ``breadth``, ``limitup_cluster``
- independent secondary (at least one required, in addition to structural):
  ``sector_flow``, ``theme_member_confirmed``
- ``social_theme`` is tracked for transparency but never counts as the
  secondary evidence type on its own — a social-attention single source must
  not manufacture a false resonance signal.

``capital_concentration`` (turnover/amount diffusion) from the plan's example
schema is intentionally not implemented yet: the codebase has no existing,
tested amount/turnover-diffusion signal to build it from, and fabricating one
without backtested support would violate the "no invented conclusions"
discipline. Evidence types are additive, so it can be wired in later without
a schema change.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

SCHEMA = "local_theme_gate_v1"

STRUCTURAL_EVIDENCE_TYPES = frozenset({"breadth", "limitup_cluster"})
SECONDARY_EVIDENCE_TYPES = frozenset({"sector_flow", "theme_member_confirmed"})
CONFIRMATION_LEVELS = ("preopen", "auction", "open", "intraday")

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "local_theme_conditional_trade_enabled": False,
    "min_strong_members": 3,
    "min_strong_members_after_core": 2,
    "leader_top_n": 2,
    "local_theme_position_cap": 0.0,
    "local_trial_budget": 0.0,
}


def _config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_CONFIG)
    merged.update({key: value for key, value in dict(config or {}).items()})
    return merged


def _structural_breadth(
    strong_codes: Sequence[str],
    core_code: str | None,
    cfg: Mapping[str, Any],
) -> tuple[bool, bool, bool, str | None]:
    """结构条件 1：广度 + 消融。返回 (structural_breadth_ok, single_pulse,
    leader_isolated, reason_code)。"""
    strong_member_count = len(strong_codes)
    single_pulse = strong_member_count <= 1
    remaining_after_core = [
        code for code in strong_codes if core_code is None or code != str(core_code)
    ]
    leader_isolated = len(remaining_after_core) < int(cfg["min_strong_members_after_core"])
    structural_breadth_ok = (
        not single_pulse
        and strong_member_count >= int(cfg["min_strong_members"])
        and not leader_isolated
    )
    if single_pulse:
        reason = "single_stock_pulse"
    elif leader_isolated:
        reason = "leader_isolated"
    elif strong_member_count < int(cfg["min_strong_members"]):
        reason = "insufficient_strong_members"
    else:
        reason = None
    return structural_breadth_ok, single_pulse, leader_isolated, reason


def _core_strength(
    core_sector_rank: int | None,
    core_decayed: bool,
    cfg: Mapping[str, Any],
) -> tuple[bool, str | None]:
    """结构条件 2：核心股位于板块前二，且未在后续阶段显著衰减。"""
    top_n = int(cfg["leader_top_n"])
    if core_sector_rank is None or core_sector_rank > top_n:
        return False, "core_not_top_ranked"
    if core_decayed:
        return False, "core_decayed"
    return True, None


def _diffusion_evidence(evidence_types: Sequence[str]) -> tuple[bool, set[str], str | None]:
    """结构条件 3：breadth/limitup_cluster 必备结构证据 + 至少一类独立扩散证据；
    社媒单源不得单独构成第二类证据。"""
    evidence_set = {str(item) for item in evidence_types}
    structural_present = bool(evidence_set & STRUCTURAL_EVIDENCE_TYPES)
    secondary = evidence_set & SECONDARY_EVIDENCE_TYPES
    diffusion_ok = structural_present and bool(secondary)
    if not structural_present:
        reason = "missing_structural_evidence"
    elif not secondary:
        reason = "social_source_only" if "social_theme" in evidence_set else "insufficient_diffusion_evidence"
    else:
        reason = None
    return diffusion_ok, evidence_set, reason


def _resolve_resonance_status(
    *,
    confirmation_level: str,
    data_quality_ok: bool,
    data_quality_reason: str | None,
    single_pulse: bool,
    strong_member_count: int,
    structural_breadth_ok: bool,
    core_strength_ok: bool,
    diffusion_ok: bool,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if not data_quality_ok:
        status = "blocked"
        reasons.append(f"data_quality:{data_quality_reason or 'unknown'}")
    elif single_pulse:
        status = "none"
    elif structural_breadth_ok and core_strength_ok and diffusion_ok:
        status = "confirmed"
    elif strong_member_count > 0:
        status = "observed"
    else:
        status = "none"
    if status == "confirmed" and confirmation_level == "preopen":
        # §3.4：盘前/D0 只能识别初步结构，不得输出 resonance confirmed。
        status = "observed"
        reasons.append("preopen_cannot_confirm")
    return status, reasons


def _resolve_execution_risk(risk_reviewed: bool, risk_hard_block: bool) -> tuple[str, str | None]:
    if not risk_reviewed:
        return "pending", None
    if risk_hard_block:
        return "blocked", "risk_hard_block"
    return "clear", None


def build_local_theme_gate(
    sector: str,
    *,
    confirmation_level: str,
    strong_member_codes: Sequence[str],
    observed_member_count: int,
    core_code: str | None,
    core_sector_rank: int | None,
    core_decayed: bool,
    evidence_types: Sequence[str],
    data_quality_ok: bool,
    data_quality_reason: str | None = None,
    risk_reviewed: bool = False,
    risk_hard_block: bool = False,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """板块局部共振门禁（issue #260 §3.2）。

    ``resonance_status``：
    - 数据质量任一项缺失 → ``blocked``（不是 ``none`` —— 区分"查过没有"和
      "查不了"）；
    - 单票脉冲固定为 ``none``；
    - 结构广度 + 核心强度 + 扩散证据三者同时满足 → ``confirmed``（盘前阶段
      封顶为 ``observed``，见下）；
    - 有强势成员但未满足 confirmed 条件 → ``observed``；
    - 否则 ``none``。

    ``execution_risk_status`` 与 resonance 完全独立：未完成风险复核
    （``risk_reviewed=False``，盘前/09:25 恒为 False）时恒为 ``pending``；
    完成复核后按 ``risk_hard_block`` 决定 ``clear``/``blocked``。
    """
    if confirmation_level not in CONFIRMATION_LEVELS:
        raise ValueError(f"unknown confirmation_level: {confirmation_level!r}")
    cfg = _config(config)
    strong_codes = [str(code) for code in strong_member_codes]

    structural_breadth_ok, single_pulse, leader_isolated, breadth_reason = _structural_breadth(
        strong_codes, core_code, cfg
    )
    core_strength_ok, core_reason = _core_strength(core_sector_rank, core_decayed, cfg)
    diffusion_ok, evidence_set, diffusion_reason = _diffusion_evidence(evidence_types)

    resonance_status, status_reasons = _resolve_resonance_status(
        confirmation_level=confirmation_level,
        data_quality_ok=data_quality_ok,
        data_quality_reason=data_quality_reason,
        single_pulse=single_pulse,
        strong_member_count=len(strong_codes),
        structural_breadth_ok=structural_breadth_ok,
        core_strength_ok=core_strength_ok,
        diffusion_ok=diffusion_ok,
    )
    execution_risk_status, risk_reason = _resolve_execution_risk(risk_reviewed, risk_hard_block)

    reason_codes = [
        reason for reason in (breadth_reason, core_reason, diffusion_reason) if reason
    ] + status_reasons + ([risk_reason] if risk_reason else [])

    participation_scope = (
        "local_theme_only" if resonance_status in ("observed", "confirmed") else "research_only"
    )

    return {
        "schema": SCHEMA,
        "sector": sector,
        "resonance_status": resonance_status,
        "execution_risk_status": execution_risk_status,
        "participation_scope": participation_scope,
        "confirmation_level": confirmation_level,
        "strong_member_count": len(strong_codes),
        "observed_member_count": int(observed_member_count),
        "leader_isolated": leader_isolated,
        "evidence_types": sorted(evidence_set),
        "data_quality": "ok" if data_quality_ok else "degraded",
        "reason_codes": reason_codes,
    }


def can_upgrade(prior_gate: Mapping[str, Any] | None, candidate_gate: Mapping[str, Any]) -> bool:
    """§3.4 合法状态迁移：只能在结构证据更充分时升级，否则维持/降级。

    Never upgrades on a coarser/older confirmation_level than the prior gate,
    and never upgrades resonance past ``confirmed`` -> further stages only
    re-confirm or downgrade.
    """
    if prior_gate is None:
        return candidate_gate.get("resonance_status") in ("observed", "confirmed")
    order = {"none": 0, "observed": 1, "confirmed": 2, "blocked": -1}
    prior_rank = order.get(str(prior_gate.get("resonance_status")), -1)
    candidate_rank = order.get(str(candidate_gate.get("resonance_status")), -1)
    prior_level_index = CONFIRMATION_LEVELS.index(str(prior_gate.get("confirmation_level")))
    candidate_level_index = CONFIRMATION_LEVELS.index(str(candidate_gate.get("confirmation_level")))
    if candidate_level_index <= prior_level_index:
        return False
    return candidate_rank >= max(prior_rank, 0)
