#!/usr/bin/env python3
"""
打板阈值读取器 — 单一事实源
============================
实盘候选闸门(daban_candidate_api)与回测引擎(daban_bt_engine)共读 config/daban_thresholds.yaml。
yaml 缺失或字段缺失时回退到 DEFAULTS（与历史硬编码完全一致），保证无配置时行为不变、
现有测试不破坏。

变更纪律：阈值只能在 daban_bt_run → research_gate 通过后改动；实盘表现差走门控停用。
"""

from typing import Any, Dict, Optional

from config_registry import config_path

try:
    import yaml  # type: ignore
except Exception:  # noqa: BLE001
    yaml = None

DEFAULTS: Dict[str, Dict[str, Any]] = {
    "cost": {"commission": 0.00025, "stamp": 0.0005, "slippage": 0.002},
    "auction": {"gap_window_low": -1.0, "gap_window_high": 3.0, "auction_seal_minute": 565},
    "universe": {
        "float_mktcap_min": 1.5e9, "float_mktcap_max": 12.0e9,
        "avg_turnover_20d_min": 2.0e8, "close_prev_min": 4.0, "close_prev_max": 35.0,
        "listed_days_min": 60,
    },
    "first_board_reseal": {
        "first_limitup_latest": "10:30", "open_board_max": 2, "reseal_minutes_max": 15,
        "seal_amount_ratio_min": 0.003, "active_buy_ratio_min": 0.60,
        "big_order_inflow_ratio_min": 0.08, "sector_limitup_min": 3,
    },
    "second_board_weak_to_strong": {
        "auction_gap_low": -1.0, "auction_gap_high": 3.0,
        "first_limitup_latest": "09:45", "sector_companion_min": 2,
    },
    "market_gate": {
        "yday_limitup_index_open_min": -2.0, "broken_rate_first20m_max": 35.0,
        "week_trades_max": 3, "day_loss_pct_stop": -2.0, "week_loss_pct_freeze": -5.0,
        "consecutive_losses_max": 3, "position_time_stop_trading_days": 2,
    },
    # P1 影子闸门（打板优化方案 §3 P1）：周期状态机记忆层 + 指数趋势闸门。
    # 默认 enabled:false —— 只做影子记录（emit shadow log），绝不影响实盘排序/评分/
    # 信号。启用（翻 enabled:true 或走 regime_gate 杠杆）必须先用 P1 影子日志 +
    # strategy_attribution_report 做"假想拦截 vs 实际结算"对照，证明拦截组显著更差。
    "emotion_cycle": {
        "enabled": False,
        # 分歧计数 >= 此值视为"二次及以上分歧"（手册 E-02：防退潮）。
        "second_divergence_min": 2,
        # 退潮状态（手册 E-05：退潮第1天清仓）——影子拦截全部新开仓。
        "weaken_states": ["S6"],
        # 分歧状态（手册 R-03：分歧日的弱转强不做）——影子降级二板弱转强。
        "divergence_states": ["S5"],
        # 主升/扩散确认状态：进入即把分歧计数清零（分歧转一致，重新起算）。
        "rise_reset_states": ["S3"],
    },
    "index_trend": {
        "enabled": False,
        "index_code": "000001",          # 上证指数
        "index_market": "sh",
        "ma_periods": [5, 10, 20],
        # 两日量能 < 20 日均量 * 此比例 视为缩量降仓（手册 T-05，相对口径不写死万亿）。
        "volume_shrink_ratio": 0.7,
        "defend_below_ma": 20,           # 收盘跌破 20 日线 → 转防守（手册 T-02）
        "reduce_below_ma": 5,            # 收盘跌破 5 日线 → 减仓（手册 T-02）
        "min_bars": 21,                  # 不足则 fail-closed
    },
    # P5(a) 回测成交约束模型（升级方案 §8.1）。不含入场/过滤阈值，只管「能不能成交」
    # 与滑点分档；缺数据一律 fail-closed。实现见 skills/common/execution_constraints.py。
    "execution_constraints": {
        "enabled": True,
        "order_amount": 200000.0,
        # 日线 volume 单位是「手」，1 手 = 100 股；缺 amount 时按此折算成交额。
        "volume_lot_shares": 100.0,
        "max_participation_rate": 0.01,
        "reseal_participation_rate": 0.01,
        "reseal_min_amount": 20000000.0,
        "limit_down_min_amount": 10000000.0,
        # 常态档 5-20bp / 高波动档 20-50bp，均保守取区间上沿。
        "slippage_bps_normal": 20.0,
        "slippage_bps_volatile": 50.0,
        "volatile_range_pct": 6.0,
        # 开启滑点分档会重定价全部历史回测结论，默认关闭，由 daban_bt_run
        # --slippage-tiering 显式打开做对照。
        "slippage_tiering": False,
    },
    # P3 S1 超预期（RankSurprise）研究策略参数（升级方案 §6.1）。NON-LIVE：
    # 未在 strategy_registry 注册前，本节只被回测/研究路径读取，绝不影响实盘排序。
    # β 未拟合（fitted=false）—— 预期基准必须先在 IS 段拟合并落盘才谈闸门。
    "rank_surprise": {
        # 预期基准 ExpectedGap = Median(Gap_peer) + β₁·昨日收益% + β₂·连板高度
        "beta_prior_return": 0.0,
        "beta_board_height": 0.0,
        "betas_fitted": False,
        # peer 样本下限：低于此值排名与中位数都无意义 → unavailable（不返回 0）
        "min_peer_count": 5,
        # 入场四条件阈值（方案 §6.1）
        "prior_rank_bottom_pct": 0.30,   # 昨日板块内强度排名后 30%
        "auction_rank_top_pct": 0.20,    # 今日竞价强度进前 20%
        "min_volume_ratio": 1.5,         # 09:45 前量比 > 1.5
        "volume_ratio_deadline": "09:45",
        # 题材退潮判定复用 market_cycle_state/market_temperature 的 S 状态口径
        "ebbing_states": ["S6"],
    },
    # 回测事件表 v4 构建口径（daban_bt_data）。只影响字段派生，不含入场/过滤阈值。
    "event_table_v4": {
        "one_word_seal_minute": 565,     # ≤09:25 一字板
        "fast_board_seal_minute": 571,   # ≤09:31 一字/快速板（含一字）
        "turnover_baseline_window": 20,
        "turnover_baseline_min_days": 15,
        "volume_ratio_checkpoint": "09:45",   # 量比时点（分钟线派生，2026-08）
        "volume_ratio_baseline_days": 5,      # 量比基准回看交易日数
    },
    # P3 S2 龙头分歧回封（DivergenceReseal）研究策略参数（升级方案 §6.1）。NON-LIVE：
    # 未在 strategy_registry 注册前，本节只被回测/研究路径读取，绝不影响实盘排序。
    "divergence_reseal": {
        "min_sector_limit_up_count": 3,
        "min_sector_fast_seal_count": 2,
        "reseal_rank_top_n": 2,
        "min_turnover_ratio": 1.5,
        "max_turnover_ratio": 3.0,
        "min_baseline_sample_days": 15,
        "preferred_baseline_sample_days": 20,
    },
    # P3 S3 最强助攻套利（AssistArbitrage）研究策略参数（升级方案 §6.1）。NON-LIVE：
    # 未在 strategy_registry 注册前，本节只被回测/研究路径读取，绝不影响实盘排序。
    # LeaderScore 复用 P2 已合入的 leader_score_shadow，本节不含任何龙头分权重副本。
    "assist_arbitrage": {
        "min_leader_score": 80.0,
        "min_sector_breadth_count": 3,
        "min_board_level_gap": 1,
        "relative_strength_field": "change_pct",
        "relative_strength_top_pct": 0.20,
        "min_theme_peer_count": 5,
        "breakout_rank_top_n": 1,
        # 退出条件阈值（与入场条件同等地位，见 skills/common/assist_arbitrage.py）
        "leader_weak_change_pct_max": -3.0,
        "breadth_decline_min_drop": 1.0,
        "min_rotation_gap": 15.0,
    },
    # P3 S4 先于龙头套利（PreleaderArbitrage）研究策略参数（升级方案 §6.1）。NON-LIVE：
    # 未在 strategy_registry 注册前，本节只被回测/研究路径读取，绝不影响实盘排序。
    # 盘前表（build_pretable）由 D-1 数据构建，本节只含 D0 确认阶段的运行期阈值。
    "preleader_arbitrage": {
        "max_reaction_minutes": 10.0,
        "min_candidate_amount": 20000000.0,
        "min_member_avg_turnover": 20000000.0,
    },
    # 反量比值/回撤区间来自单一历史案例（摩恩电气）的工程化取值，未经样本外验证，
    # 见 skills/common/reverse_volume.py 与 docs_private/reverse-volume-gate-evaluation-2026-08.md。
    "reverse_volume": {
        "min_drawdown_pct": 0.25,
        "max_drawdown_pct": 0.40,
        "max_volatility_contraction_ratio": 0.6,
        "max_volume_percentile_20d": 0.30,
        "reversal_volume_ratio_min": 1.3,
        "reversal_volume_ratio_second_min": 1.5,
        "entry_position_pct": 0.10,
        "second_confirmation_position_pct_min": 0.20,
        "second_confirmation_position_pct_max": 0.30,
    },
    # P4(e) 熔断阶梯 R 化（升级方案 §7.1(e)）。新增节，market_gate 一字未改——
    # 两套口径并存是刻意的，R 口径先在 paper 跑样本。实现见 position_risk.py。
    "circuit_ladder_r": {
        "day_loss_r_stop": -2.0,
        "week_loss_r_reduce": -4.0,
        "week_loss_r_freeze": -5.0,
        "drawdown_halve_pct": 8.0,
        "drawdown_stop_pct": 10.0,
        "theme_risk_r_max": 2.0,
        "off_system_streak_max": 3,
    },
    # §7b 三项调整机制，默认全部关闭；启用必须引用打板归因报告数据
    # （scripts/strategy_attribution_report.py），杠杆实现见 daban_adjustments.py。
    "adjustments": {
        "regime_gate": {
            "enabled": False,
            "blocked_theme_stages": ["diverging", "fading"],
            "min_temperature_score": 40.0,
        },
        "entry_mode_weights": {
            "enabled": False,
            "weights": {
                "first_board_reseal": 1.0,
                "second_board_weak_to_strong": 1.0,
            },
        },
        "auction_premium_exit": {
            "enabled": False,
            "min_premium_pct": 3.0,
            "full_exit_premium_pct": 6.0,
        },
    },
}


def _config_path() -> str:
    return str(config_path("daban_thresholds"))


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in base.items()}
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """加载阈值（yaml 覆盖 DEFAULTS）。yaml 缺失/解析失败 → DEFAULTS。"""
    if yaml is None:
        return _deep_merge(DEFAULTS, {})
    p = path or _config_path()
    try:
        with open(p, encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):  # type: ignore[attr-defined]
        loaded = {}
    return _deep_merge(DEFAULTS, loaded if isinstance(loaded, dict) else {})


def section(name: str, path: Optional[str] = None) -> Dict[str, Any]:
    """取某一节阈值（带默认）。"""
    return load_config(path).get(name, dict(DEFAULTS.get(name, {})))


if __name__ == "__main__":
    import json
    print(json.dumps(load_config(), ensure_ascii=False, indent=2))
