#!/bin/bash
# 集合竞价收口 — 调用 auction_collector.py --finalize
cd ~/.hermes
~/.hermes/hermes-agent/venv/bin/python3 skills/daban-stock-picker/scripts/auction_collector.py \
  --codes sh600011,sh600310,sz002156,sh600584,sz002185,sz000021,sh600667,sz001696,sh603859,sh601225,sh601898 \
  --finalize 2>&1
