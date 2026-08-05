"""mootdx (通达信TCP port 7709) adapter for the A-share market.

Provides a clean abstraction over the mootdx library for real-time quotes,
daily K-line bars, and all-stock listing.  Designed as a drop-in supplement
to market_adapters.py — inserted before degraded push2 fallback chains.

Usage (standalone)::

    from mootdx_adapter import fetch_mootdx_bars, fetch_mootdx_quote
    bars = fetch_mootdx_bars("000001", days=10)
    quote = fetch_mootdx_quote("600519")

The functions return empty containers on failure (connectivity, import, parse)
so callers can treat them as transparent fallback sources. "Empty" is therefore
ambiguous by design — call :func:`mootdx_available` when the caller needs to tell
"the package is not installed here" apart from "the TCP source had no data".
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

LOGGER = logging.getLogger(__name__)

MOOTDX_MISSING_HINT = (
    "mootdx 未安装，通达信深历史源不可用（回测将只剩 akshare 的最近 3-4 周）；"
    "修复：pip install -e '.[deephistory]'"
)

_MOOTDX_CLIENT = None
_MOOTDX_CLIENT_LOCK = False


@lru_cache(maxsize=1)
def mootdx_available() -> bool:
    """Whether the mootdx package is importable in this environment.

    ``mootdx`` ships in the optional ``deephistory`` extra, so CI and the default
    production install do not have it. Without this probe a missing package looks
    exactly like an empty upstream response.
    """
    try:
        from mootdx.quotes import Quotes  # noqa: F401
    except ImportError:
        LOGGER.warning(MOOTDX_MISSING_HINT)
        return False
    return True


def _normal_code(code: Any) -> str:
    """Normalize a stock code to bare 6 digits."""
    text = str(code or "").strip().lower()
    if text.startswith(("sh", "sz", "bj")):
        text = text[2:]
    if "." in text:
        text = text.split(".", 1)[0]
    return text.zfill(6) if text else ""


def _get_client():
    """Lazy singleton for the mootdx Quotes client (通达信TCP 7709)."""
    global _MOOTDX_CLIENT, _MOOTDX_CLIENT_LOCK
    if _MOOTDX_CLIENT is not None:
        return _MOOTDX_CLIENT
    if not mootdx_available():
        return None
    from mootdx.quotes import Quotes
    if _MOOTDX_CLIENT_LOCK:
        return None
    _MOOTDX_CLIENT_LOCK = True
    try:
        client = Quotes.factory()
        # Quick connectivity probe
        _ = client.quotes(symbol="000001")
        _MOOTDX_CLIENT = client
    except Exception as exc:
        LOGGER.warning("mootdx 连接失败（通达信 TCP 7709）err=%s", exc)
        _MOOTDX_CLIENT = None
    finally:
        _MOOTDX_CLIENT_LOCK = False
    return _MOOTDX_CLIENT


def fetch_mootdx_bars(code: str, *, days: int = 120) -> list[dict[str, Any]]:
    """Fetch daily K-line bars via mootdx (通达信TCP).

    Returns a list of dicts compatible with the normalized bar format
    used by market_adapters.py::

        [{"date", "open", "close", "high", "low", "volume", "amount"}, ...]

    Returns [] on any failure (connectivity, import, parse).
    """
    client = _get_client()
    if client is None:
        return []

    symbol = _normal_code(code)
    if not symbol:
        return []

    try:
        df = client.bars(symbol=symbol, frequency=9, start=0, count=days)
    except Exception:
        return []

    if not hasattr(df, "columns") or df.empty:
        return []

    bars: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        try:
            ts = row.get("datetime")
            date_str = str(ts)[:10] if ts else ""
            if not date_str:
                continue
            bars.append({
                "date": date_str,
                "open": float(row.get("open", 0)),
                "close": float(row.get("close", 0)),
                "high": float(row.get("high", 0)),
                "low": float(row.get("low", 0)),
                "volume": int(float(row.get("vol", 0))),
                "amount": float(row.get("amount", 0)),
            })
        except (TypeError, ValueError):
            continue
    return bars[-days:]


def fetch_mootdx_quote(code: str) -> dict[str, Any]:
    """Fetch a single-stock real-time quote via mootdx TCP.

    Returns::

        {"code", "name", "price", "open", "high", "low",
         "prev_close", "volume", "amount", "change_pct", "provider": "mootdx"}

    Returns {} on failure.
    """
    client = _get_client()
    if client is None:
        return {}

    symbol = _normal_code(code)
    if not symbol:
        return {}

    try:
        df = client.quotes(symbol=symbol)
    except Exception:
        return {}

    if not hasattr(df, "columns") or df.empty:
        return {}

    row = df.iloc[0]
    try:
        price = float(row.get("price", 0))
        prev_close = float(row.get("last_close", 0))
        change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0.0
        return {
            "code": symbol,
            "name": str(row.get("code", symbol)),
            "price": price,
            "open": float(row.get("open", 0)),
            "high": float(row.get("high", 0)),
            "low": float(row.get("low", 0)),
            "prev_close": prev_close,
            "volume": int(float(row.get("vol", 0))),
            "amount": float(row.get("amount", 0)),
            "change_pct": round(change_pct, 2),
            "provider": "mootdx",
        }
    except (TypeError, ValueError):
        return {}


def fetch_mootdx_quotes(codes: list[str]) -> dict[str, dict[str, Any]]:
    """Batch quotes.  mootdx is single-stock, so we loop."""
    result: dict[str, dict[str, Any]] = {}
    for code in codes:
        quote = fetch_mootdx_quote(code)
        if quote:
            result[code] = quote
    return result


def fetch_mootdx_spot() -> list[dict[str, Any]]:
    """Fetch all-stock listing from mootdx.

    Returns records with {"代码", "名称", "昨收"} keys, compatible
    with market_adapters.py _normalize_spot_records.
    Returns [] on failure.
    """
    client = _get_client()
    if client is None:
        return []

    try:
        df = client.stock_all()
        if not hasattr(df, "columns") or df.empty:
            return []
        records: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            try:
                code = str(row.get("code", "")).strip()
                if not code or len(code) < 6:
                    continue
                records.append({
                    "代码": code,
                    "名称": str(row.get("name", "")).replace("\x00", "").strip() or code,
                    "昨收": float(row.get("pre_close", 0)),
                })
            except (TypeError, ValueError):
                continue
        return records
    except Exception:
        return []
