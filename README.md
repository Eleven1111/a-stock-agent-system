<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://img.shields.io/badge/A--Stock-Agent_System-1a1a2e?style=for-the-badge">
  <img alt="A-Stock Agent System" src="https://img.shields.io/badge/A--Stock-Agent_System-ffffff?style=for-the-badge">
</picture>

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-87%20passed-brightgreen)](tests/)
[![Smoke](https://img.shields.io/badge/smoke-8%2F8%20passed-brightgreen)](scripts/smoke_test.py)

> Smoke badge reflects the latest connected validation. Offline runs may still
> time out on `global_monitor` or `hk_a_linkage` because they depend on live market data.

A multi-agent research system for China's A-share market. Eleven repository skills, a four-dimensional scoring engine, and a full decision pipeline — from global macro surveillance to portfolio risk management, limit-up candidate gating, and offline strategy validation.

**Not a trading bot.** This system analyzes data and produces graded recommendations. It never places orders.

---

## Architecture

```mermaid
graph TD
    TRIAGE[stock-triage<br/>Orchestrator] --> ANALYST[stock-analyst<br/>Technical Engine]
    TRIAGE --> HOTMONEY[hot-money-tactics<br/>Sentiment]
    TRIAGE --> GLOBAL[global-market-monitor<br/>Macro Surveillance]
    TRIAGE --> NEWS[news-to-sector<br/>Catalyst Mapping]
    TRIAGE --> SERENITY[serenity-investment-research<br/>Deep Research]
    TRIAGE --> DABAN[daban-stock-picker<br/>Limit-up Candidate Gate]
    TRIAGE --> CHANLUN[chanlun-backtest<br/>Offline Research Gate]

    ANALYST --> FLOW[capital_flow_monitor<br/>Fund Flows]
    ANALYST --> PORT[portfolio_manager<br/>Risk Control]
    ANALYST --> INTRA[intraday_monitor<br/>5-min Alerts]
    HOTMONEY --> FLOW
    GLOBAL --> FLOW
```

## Capabilities

| Module | What it does | Data Sources |
|--------|-------------|--------------|
| **stock-analyst** | Multi-timeframe technical analysis (day/week/60m/30m), sector scanning, screener | Tencent, Sina, yfinance |
| **hot-money-tactics** | Limit-up board analysis, sentiment cycles, sector rotation tracking | AkShare |
| **daban-stock-picker** | Main-board 10cm limit-up candidate gate: first-board reseal, second-board weak-to-strong, six-question veto, tradeability | Structured quote/sector/portfolio JSON |
| **chanlun-backtest** | Offline research gate for Chan Theory and strategy rules: IS/OOS wall, costs, controls, statistical tests | Local research-state JSON |
| **global-market-monitor** | US indices, VIX, Treasuries, commodities, FX, natural disasters → A-share impact | yfinance, USGS, GDACS |
| **news-to-sector** | Real-time news → 18 supply-chain impact maps with divergence analysis | SerpAPI |
| **serenity-investment-research** | Deep-dive: supply chain, financials, valuation scenarios, bear-case audit | cninfo, pypdf |
| **four-dim scorer** | Weighted S/A/B/C grading: technical(30%) × sentiment(25%) × catalyst(25%) × deep(20%) | All above |
| **hk-a-linkage** | AH premium spreads, HSI divergence, key HK stock movements | Tencent, yfinance |
| **capital-flow-monitor** | Northbound flows, institutional/retail flows, sector-level flows | Eastmoney |
| **portfolio-manager** | P&L tracking, stop-loss, trailing stops, concentration checks | Tencent |
| **intraday-monitor** | 5-minute alerts: limit-up/down, volume spikes, sudden moves | Tencent |
| **institution-tracker** | Research visits, analyst reports, insider trades | Eastmoney |
| **event-calendar** | Lockup expirations, dividends, policy windows | Eastmoney |
| **performance-tracker** | Signal accuracy tracking with grade-level breakdown | Tencent |

## Quick Start

### Prerequisites

Python **3.10 or higher** is required (macOS default Python 3.9 won't work).

```bash
# Check your Python version
python3 --version   # must be 3.10+
```

### Install

```bash
git clone https://github.com/Eleven1111/a-stock-agent-system.git
cd a-stock-agent-system

# Create and activate a virtual environment with Python 3.10+
python3.12 -m venv .venv        # or python3.10 / python3.11
source .venv/bin/activate

# Install the package with all extras
python -m pip install -e ".[charts,fundamentals,research,dev]"
```

> **macOS users**: If your system `python3` is still 3.9, install a newer version via
> `brew install python@3.12`, then use `python3.12` explicitly as shown above.

### Verify

```bash
python scripts/smoke_test.py
python -m pytest -q tests/
```

### Run

```bash
# Grade a stock
python skills/stock-triage/scripts/four_dim_scorer.py 002156 通富微电 --json

# Global market scan
python skills/global-market-monitor/scripts/monitor.py --summary

# HK-A linkage
python skills/stock-triage/scripts/hk_a_linkage.py

# News → sector mapping
python skills/news-to-sector/scripts/main.py "焦煤期货主力合约触及涨停"

# Portfolio check
python skills/stock-triage/scripts/portfolio_manager.py --check

# 60-minute entry timing
python skills/stock-triage/scripts/four_dim_scorer.py 002156 通富微电 --timeframe 60

# Limit-up candidate gate
python skills/daban-stock-picker/scripts/daban_candidate_api.py --example --json

# Offline strategy research gate
python skills/chanlun-backtest/scripts/research_gate.py --example --json
```

## Configuration

```bash
# Optional: relocate runtime data/cache/state (default: ~/.hermes)
export HERMES_HOME=/path/to/hermes

# Optional: override the Hermes Python used by BaoStock fallback scripts
export HERMES_PYTHON=/path/to/python3

# Optional: enable Eastmoney APIs (fund flows, institutional data)
export NO_PROXY=.eastmoney.com,.gtimg.cn,.sinajs.cn

# Optional: enable news search
export SERPAPI_API_KEY=your_key
```

All runtime paths resolve through `skills/common/paths.py` and honor `HERMES_HOME`, so the
system can run inside the repo, a sandbox, or CI without writing to the deploy machine's home.

Source health tracking is built-in. When critical data is missing (e.g., yfinance unavailable), the system emits `"status": "insufficient_data"` and refuses to output directional advice. When a scoring dimension lacks data (e.g., no `SERPAPI_API_KEY` → no catalyst), its weight is **renormalized away** instead of silently contributing a neutral 5.0; the dropped dimensions are listed in `excluded_dims`.

## Cron Schedule

All jobs are defined in [`cron/hermes-cron-manifest.json`](cron/hermes-cron-manifest.json). Scheduling requires an external [Hermes Agent](https://hermes-agent.nousresearch.com) runtime — this repo provides the scripts; Hermes provides the clock.

```bash
python scripts/validate_cron_manifest.py cron/hermes-cron-manifest.json
```

| Time (CST) | Job | Frequency |
|------------|-----|-----------|
| 08:15 | Global pre-market scan | Workdays |
| 09:30–11:30, 13:00–15:00 | Intraday alerts | Every 5 min (session-guarded) |
| 09:45, 13:45, 14:45 | HK-A linkage | Workdays |
| 10:30, 14:30 | Capital flow monitor | Workdays |
| 15:08 | Four-dim batch scoring | Workdays |
| 15:10 | Portfolio risk check | Workdays |
| 15:25 | Triage → Kanban dispatch | Workdays |
| 22:30 | Global evening scan | Workdays |
| Sat 10:00 | Institution weekly | Weekly |
| Sun 10:00 | Performance weekly | Weekly |

## Output Format

Every scoring script returns structured JSON:

```json
{
  "code": "002156",
  "name": "通富微电",
  "confidence": "high",
  "data_coverage": {"realtime": true, "kline": true, "news": true, "valuation": true},
  "weighted": 7.2,
  "grade": "A",
  "emoji": "🟢🟢",
  "advice": "推荐 — 技术面偏多，有催化支撑",
  "scores": {
    "technical": {"score": 7.5, "ma5": 58.3, "rsi6": 55.1, "detail": "..."},
    "sentiment": {"score": 6.0, "turnover": 4.2, "detail": "..."},
    "catalyst": {"score": 8.0, "news_count": 3, "detail": "..."},
    "deep": {"score": 7.0, "pe": 35.2, "detail": "..."}
  }
}
```

The global monitor emits `source_health` and gates impact analysis on minimum data coverage:

```json
{
  "source_health": {
    "yfinance": {"status": "ok"},
    "serpapi": {"status": "ok"},
    "usgs": {"status": "ok"}
  },
  "impact": {
    "status": "ok",
    "alerts": [...],
    "summary": "利空：AI算力(-3), 半导体(-2)；利好：电力(+1)"
  }
}
```

If critical data is missing:

```json
{
  "source_health": {"yfinance": {"status": "failed", "error": "yfinance not installed"}},
  "impact": {
    "status": "insufficient_data",
    "alerts": [],
    "summary": "关键市场数据不足，禁止输出方向性A股判断"
  }
}
```

## Project Structure

```
a-stock-agent-system/
├── pyproject.toml              # Dependencies
├── config/scoring.yaml         # Scoring weights & risk parameters
├── cron/hermes-cron-manifest.json  # 11 scheduled jobs
├── scripts/
│   ├── smoke_test.py           # 8-test validation suite
│   └── validate_cron_manifest.py
├── tests/                      # 87 unit tests
├── skills/
│   ├── common/                 # Shared HTTP + atomic state store
│   ├── stock-triage/           # Orchestrator hub
│   ├── stock-analyst/          # Technical analysis engine
│   ├── hot-money-tactics/      # Sentiment & limit-up analysis
│   ├── daban-stock-picker/     # Main-board 10cm limit-up candidate gate
│   ├── chanlun-backtest/       # Offline strategy research gate
│   ├── global-market-monitor/  # Macro → A-share impact
│   ├── news-to-sector/         # Supply-chain catalyst mapping
│   ├── serenity-investment-research/  # Deep fundamental research
│   ├── a-stock-commands/       # Discord slash commands
│   ├── a-stock-data/           # Data source reference
│   └── a-stock-daily-report/   # Daily briefing template
└── AGENTS.md                   # Project constitution
```

## Design Principles

**Fail closed.** When critical data is missing, the system emits `insufficient_data` — never guesses.

**Confidence before conviction.** Every analysis carries a `confidence` field. Low confidence blocks directional advice.

**Scripts over services.** Every module is a standalone CLI script. No servers, no databases, no daemons. Pipe them together however you want.

**State is atomic.** All JSON writes go through `state_store.atomic_write_json()` with backup and crash recovery.

## Two Scoring Engines — Don't Confuse Them

The system separates **general stock health** from **hot-money board-hitting (打板)** by design:

| Engine | Module | Use for | Holding horizon |
|--------|--------|---------|-----------------|
| **Four-dim scorer** | `stock-triage/four_dim_scorer.py` | General health check (trend / valuation / catalyst) | Swing / mid-term |
| **Hot-money tactics** | `hot-money-tactics/analyze.py` | 打板 leader selection (连板 ladder, seal quality, auction seal, sentiment cycle) | T+1 (next-day) |

A 打板 leader is typically in its ignition phase — moving averages not yet bullish-aligned, PE
high or meaningless. The four-dim scorer's technical/valuation dimensions would **underrate** it.
**Route 打板 decisions through `hot-money-tactics`, not the four-dim scorer.**

### Win-rate is measured in 打板 terms

`performance_tracker.py` is the system's only feedback loop, so it uses **board-hitting-native
metrics** rather than 30/60-day swing returns:

- **隔日溢价 / 隔日收益** (T+1 open premium / close return) — where a 打板 trade actually exits
- **连板晋级率** (did T+1 limit-up again?)
- **Expectancy** = win-rate × avg-win − loss-rate × avg-loss, plus payoff ratio
- **Alpha vs CSI 300** — strips market beta so a number means *excess*, not "everything rose"

Returns are computed from **forward-adjusted (qfq) K-lines** so splits/dividends don't corrupt
them, and outcomes are resolved deterministically at the horizon — no "first touch +3% locks a
win forever" upward bias.

## Tradeability Gate

Before emitting directional advice, the four-dim scorer runs `tradeability.assess_tradeability()`:
a sealed 一字 limit-up board (`limit_up_sealed`) or a halted name (`halted`) is flagged as
**un-buyable** and the advice is prefixed accordingly — a high score on a board you can't actually
get filled on is not actionable.

## Data Sources

| Source | Coverage | Requirements |
|--------|----------|-------------|
| Tencent `qt.gtimg.cn` | A-share/HK real-time quotes, K-lines | None |
| Yahoo Finance `yfinance` | US/global indices, VIX, commodities, FX | `pip install yfinance` |
| Eastmoney | Fund flows, institutional data, events | `NO_PROXY=.eastmoney.com` |
| Sina `hq.sinajs.cn` | A-share real-time (fallback) | None |
| SerpAPI | Global news search | `SERPAPI_API_KEY` |
| USGS | Earthquake monitoring | None |
| GDACS | Cyclone/flood/volcano alerts | None |

## Testing

```bash
pip install -e ".[dev]"
python -m pytest -q tests/        # 87 tests
python scripts/smoke_test.py      # 8 integration checks
python scripts/validate_cron_manifest.py
```

## Disclaimer

This system is for research and educational purposes only. It does **not** constitute investment advice. All outputs are derived from public data and quantitative rules. Past performance does not guarantee future results. The system never places trades or accesses brokerage accounts.

## License

MIT
