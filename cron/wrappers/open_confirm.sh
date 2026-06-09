#!/bin/bash
# 开盘确认 — 调用 open_confirmation.py
cd ~/.hermes
~/.hermes/hermes-agent/venv/bin/python3 skills/daban-stock-picker/scripts/open_confirmation.py \
  --codes sh600011,sh600310,sz002156,sh600584,sz002185,sz000021,sh600667,sz001696,sh603859,sh601225,sh601898 2>&1
