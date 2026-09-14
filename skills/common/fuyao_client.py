#!/usr/bin/env python3
"""同花顺金融数据 API（fuyao.aicubes.cn）客户端 — X-api-key 鉴权。

契约：https://fuyao.aicubes.cn/llms-full.txt
- 统一 ApiResponse 信封：code=0 成功；HTTP 429 / code=4001 限流（退避一次重试）
- 鉴权：X-api-key 请求头，密钥经 FUYAO_API_KEY 环境变量注入
  （由 A_STOCK_ENV_FILE 加载或调用方预先注入；不落代码/日志）
- fail-closed：任何失败抛 DataSourceError，由调用方的兜底链处理
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List

from http_client import DataSourceError, request_json

BASE_URL = "https://fuyao.aicubes.cn"
PROVIDER = "fuyao_ths_api"


def _api_key() -> str:
    return os.environ.get("FUYAO_API_KEY", "").strip()


def _get(path: str, params: Dict[str, str], *, timeout: int = 20) -> Dict[str, Any]:
    key = _api_key()
    if not key:
        raise DataSourceError(PROVIDER, "FUYAO_API_KEY missing")
    query = "&".join(f"{name}={value}" for name, value in params.items())
    url = f"{BASE_URL}{path}?{query}"
    payload: Any = None
    for attempt in (1, 2):  # 限流退避一次
        try:
            result = request_json(
                url,
                source=PROVIDER,
                headers={"X-api-key": key},
                timeout=timeout,
            )
            payload = result.data
        except DataSourceError as exc:
            detail = str(getattr(exc, "message", "") or exc)
            if attempt == 1 and ("429" in detail or "4001" in detail):
                time.sleep(2.0)
                continue
            raise DataSourceError(
                PROVIDER, f"fuyao request failed: {detail}"
            ) from exc
        if isinstance(payload, dict) and payload.get("code") == 4001 and attempt == 1:
            time.sleep(2.0)
            continue
        break
    if not isinstance(payload, dict):
        raise DataSourceError(PROVIDER, "fuyao response invalid")
    if payload.get("code") != 0:
        raise DataSourceError(
            PROVIDER, f"fuyao code={payload.get('code')}: {payload.get('message')}"
        )
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def limit_up_pool(trade_date: str) -> List[Dict[str, Any]]:
    """涨停/连板股票池（按交易日，trade_date: yyyyMMdd）。"""
    data = _get("/api/a-share/special-data/limit-up-pool", {"trade_date": trade_date})
    item = data.get("item")
    return item if isinstance(item, list) else []


def limit_up_ladder(trade_date: str) -> Dict[str, Any]:
    """连板天梯矩阵（近 30 个交易日）。"""
    return _get("/api/a-share/special-data/limit-up-ladder", {"trade_date": trade_date})


def dragon_tiger_list(trade_date: str) -> List[Dict[str, Any]]:
    """龙虎榜榜单（按交易日）。"""
    data = _get("/api/a-share/special-data/dragon-tiger-list", {"trade_date": trade_date})
    item = data.get("item")
    return item if isinstance(item, list) else []
