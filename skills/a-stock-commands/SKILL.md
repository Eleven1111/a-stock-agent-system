---
name: a-stock-commands
description: >
  A-share command router for deep research, scanning, monitoring, reports,
  comparison, and global-market impact analysis.
version: 1.1.0
author: Luna
metadata:
  hermes:
    tags: [A股, 快捷指令, 编排]
    category: finance
---

# A股指令路由

All commands route through `stock-triage`. Command arguments describe the
current request; they must not be copied into static Skill or AGENTS files as a
persistent watchlist.

## Commands

```text
/deep <code-or-name>
/scan <sector-or-full-market>
/alert <code-or-name> <condition>
/report <scope> <period>
/compare <security-a> <security-b> [...]
/global [--news]
/push
```

Routing:

| Command | Action |
| --- | --- |
| `/deep` | Queue or run Serenity research plus technical and risk evidence |
| `/scan` | Run a named-sector or true full-market scan |
| `/alert` | Create a monitor-registry entry and structured trigger |
| `/report` | Read bounded run artifacts for the requested scope |
| `/compare` | Compare explicitly supplied securities |
| `/global` | Map global events to A-share sectors and candidate securities |
| `/push` | Deliver already validated pending reports |

## State Rules

- Resolve names to codes with a current security master.
- Store active stock, sector, and theme subscriptions in
  `monitor_registry.json`.
- Store manual cancellation as a tombstone.
- Link alerts and recommendations through `signal_ledger.jsonl`.
- Sold positions and cancelled subscriptions must disappear from scheduled
  monitoring after lifecycle synchronization.
- Do not maintain a second hardcoded alert list in cron prompts or Skill docs.

## Safety

- `/scan full-market` must enumerate the full eligible universe.
- `/deep` and `/compare` do not imply a buy recommendation.
- `/alert` creates monitoring, not order execution.
- Directional output still requires announcement, data-quality, tradeability,
  portfolio, strategy, and T+1 checks.
- `/push` must not deliver blocked or stale artifacts as current conclusions.
