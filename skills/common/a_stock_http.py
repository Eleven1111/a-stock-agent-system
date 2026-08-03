"""
共享 HTTP 模块 — 数据访问层
==========================
核心评分链路（four_dim_scorer 等）统一走此模块，提供：
- 环境变量加载
- 标准 HTTP GET（自动 NO_PROXY）
- 腾讯/新浪/东财专用封装
- DataSourceError 统一异常

其他业务脚本可能仍使用自己的 urllib 实现，欢迎逐步收敛至此模块。
"""

import os
import sys
import threading
import time
from typing import Dict, Any, List, Optional
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from data_access_config import provider_settings
from http_client import (
    DataSourceError,
    ErrorType,
    HttpClient,
    HttpResult,
    build_request,
    request_json,
    request_text,
)
from paths import env_file as _env_file
from provider_contract import transport_contract


TENCENT_QUOTE_ADAPTER_VERSION = "tencent-adapter-v2"


def load_hermes_env() -> Dict[str, str]:
    """加载 $HERMES_HOME/.env（默认 ~/.hermes/.env）到 os.environ，并确保代理绕过生效"""
    env_file = _env_file()
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k not in os.environ:
                        os.environ[k] = v
    # 确保 NO_PROXY 生效（urllib 在 import 时缓存代理设置，http_client.py
    # 模块加载时已处理；这里再兜底一次，防止 .env 中有 NO_PROXY 覆盖）
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy", "")
    if no_proxy:
        os.environ["NO_PROXY"] = no_proxy
        os.environ["no_proxy"] = no_proxy
    return dict(os.environ)


def http_get_json(url: str, headers: Dict = None, timeout: int = 10,
                  encoding: str = "utf-8", no_proxy: bool = True) -> Dict[str, Any]:
    """标准 HTTP GET → JSON，自动处理 NO_PROXY"""
    default_headers = {"User-Agent": "Mozilla/5.0 (Hermes A-Stock Agent)"}
    if headers:
        default_headers.update(headers)
    result = request_json(
        url,
        source=url,
        timeout=timeout,
        max_attempts=2,
        encoding=encoding,
        headers=default_headers,
    )
    if not isinstance(result.data, dict):
        raise DataSourceError(
            url,
            "expected a JSON object",
            error_type=ErrorType.INVALID_RESPONSE,
            attempts=result.attempts,
            timestamp=result.fetched_at,
        )
    return result.data


# 腾讯行情字段下标（GBK，~ 分隔）。集中定义，便于在腾讯改版时单点维护。
# 测试 test_a_stock_http_parse.py 用合成报文锁定这些下标，防止静默漂移。
_TENCENT_FIELDS = {
    "name": 1, "price": 3, "prev_close": 4, "open": 5, "volume": 6,
    "change_pct": 32, "high": 33, "low": 34, "amount": 37,
    "turnover": 38, "pe": 39, "market_cap": 45,
}
_TENCENT_SCALE = {"amount": 10000}  # 成交额单位：万元 → 元


def _f(parts: List[str], idx: int) -> Optional[float]:
    """安全取 float，越界/空值返回 None。"""
    if idx >= len(parts) or parts[idx] == "":
        return None
    try:
        return float(parts[idx])
    except ValueError:
        return None


def parse_tencent_quote_line(line: str) -> Optional[Dict[str, Any]]:
    """
    解析单行腾讯行情报文（纯函数，不触网，可单测）。
    形如: v_sh600519="1~贵州茅台~600519~...~"
    返回 (code, fields) 失败返回 None。
    """
    if "=" not in line:
        return None
    code_raw, data = line.split("=", 1)
    code = code_raw.replace("v_", "").strip()
    parts = data.strip().strip('"').split("~")
    if len(parts) < 40:
        return None
    fields: Dict[str, Any] = {}
    for key, idx in _TENCENT_FIELDS.items():
        val = parts[idx] if (key == "name" and idx < len(parts)) else _f(parts, idx)
        if key in _TENCENT_SCALE and val is not None:
            val = val * _TENCENT_SCALE[key]
        fields[key] = val
    return {"code": code, "fields": fields}


# 腾讯五档盘口下标（parts[9..28]）。data_sources.md 原字段表停在 45，漏掉这段——
# 集合竞价(9:15-9:25)期间这五档反映累积委买/委卖，是免费源最接近 L2 的竞价信号。
# 顺序：买一价/量, 买二价/量 … 买五；卖一价/量 … 卖五。
_TENCENT_BID_BASE = 9   # 买一价下标，之后每档 +2
_TENCENT_ASK_BASE = 19  # 卖一价下标，之后每档 +2


def parse_tencent_orderbook_line(line: str) -> Optional[Dict[str, Any]]:
    """
    解析单行腾讯报文的五档盘口（纯函数，不触网，可单测）。
    返回 {"code", "bids": [(price, vol)*5], "asks": [(price, vol)*5]}，失败返回 None。
    价或量缺失的档位以 (None, None) 占位，不丢弃整行。
    """
    if "=" not in line:
        return None
    code_raw, data = line.split("=", 1)
    code = code_raw.replace("v_", "").strip()
    parts = data.strip().strip('"').split("~")
    if len(parts) < _TENCENT_ASK_BASE + 10:
        return None
    bids = [(_f(parts, _TENCENT_BID_BASE + 2 * i), _f(parts, _TENCENT_BID_BASE + 2 * i + 1)) for i in range(5)]
    asks = [(_f(parts, _TENCENT_ASK_BASE + 2 * i), _f(parts, _TENCENT_ASK_BASE + 2 * i + 1)) for i in range(5)]
    return {"code": code, "bids": bids, "asks": asks}


def tencent_symbol(code: str) -> str:
    normalized = str(code).strip().lower()
    if normalized.startswith(("sh", "sz", "hk")):
        return normalized
    return ("sh" if normalized.startswith("6") else "sz") + normalized.zfill(6)


def fetch_tencent_quotes_result(
    codes: List[str],
    *,
    client: Optional[HttpClient] = None,
) -> HttpResult[Dict[str, Dict[str, Any]]]:
    """Canonical Tencent quote transport with provenance metadata."""
    symbols = [tencent_symbol(code) for code in codes]
    request = build_request(
        "http://qt.gtimg.cn/q=" + ",".join(symbols),
        headers={"User-Agent": "Mozilla/5.0"},
    )
    if client is None:
        settings = provider_settings("tencent")
        client = HttpClient(
            "tencent",
            timeout=float(settings.get("timeout_seconds", 10)),
            max_attempts=int(settings.get("max_attempts", 2)),
        )
    response = client.request_text(request, encoding="gbk")
    transport = transport_contract("http://qt.gtimg.cn/")
    quotes: Dict[str, Dict[str, Any]] = {}
    for line in response.data.strip().splitlines():
        parsed = parse_tencent_quote_line(line)
        if not parsed:
            continue
        quotes[parsed["code"]] = {
            **parsed["fields"],
            "provider": "tencent",
            "provider_version": TENCENT_QUOTE_ADAPTER_VERSION,
            "fetched_at": response.fetched_at,
            "transport_trust": transport["trust"],
            "transport_reason": transport["reason"],
            "directional_eligible": transport["directional_eligible"],
        }
    if not quotes:
        raise DataSourceError(
            "tencent",
            "no valid quote records",
            error_type=ErrorType.INVALID_RESPONSE,
            attempts=response.attempts,
            timestamp=response.fetched_at,
        )
    return HttpResult(quotes, response.fetched_at, response.attempts)


def fetch_tencent_quote(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    腾讯实时行情
    codes: ["sh600011", "sz002156", "hk00700"]
    返回: {code: {price, change_pct, volume, amount, ...}}
    """
    try:
        result = fetch_tencent_quotes_result(codes)
    except DataSourceError as exc:
        raise DataSourceError(
            "tencent",
            f"行情请求失败: {exc.message}",
            exc,
            error_type=exc.error_type,
            attempts=exc.attempts,
            timestamp=exc.timestamp,
            status_code=exc.status_code,
        ) from exc

    return {code: dict(quote) for code, quote in result.data.items()}


def fetch_tencent_snapshot(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    腾讯实时行情 + 五档盘口（一次请求合并），供集合竞价采集使用。
    返回: {code: {price, prev_close, volume, market_cap, name, ..., bids, asks}}
    """
    url = "http://qt.gtimg.cn/q=" + ",".join(codes)
    try:
        response = request_text(
            url,
            source="tencent",
            timeout=10,
            max_attempts=2,
            encoding="gbk",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        raw = response.data
    except DataSourceError as exc:
        raise DataSourceError(
            "tencent",
            f"行情请求失败: {exc.message}",
            exc,
            error_type=exc.error_type,
            attempts=exc.attempts,
            timestamp=exc.timestamp,
            status_code=exc.status_code,
        ) from exc

    results: Dict[str, Dict[str, Any]] = {}
    transport = transport_contract(url)
    for line in raw.strip().split("\n"):
        quote = parse_tencent_quote_line(line)
        if not quote:
            continue
        book = parse_tencent_orderbook_line(line) or {"bids": [], "asks": []}
        results[quote["code"]] = {
            **quote["fields"],
            "bids": book["bids"],
            "asks": book["asks"],
            "provider": "tencent",
            "provider_version": TENCENT_QUOTE_ADAPTER_VERSION,
            "fetched_at": response.fetched_at,
            "transport_trust": transport["trust"],
            "transport_reason": transport["reason"],
            "directional_eligible": transport["directional_eligible"],
        }
    return results


def fetch_eastmoney_json(path: str, params: Dict = None) -> Dict[str, Any]:
    """
    东方财富数据中心 API
    path: "/api/qt/kamt.kline/get"
    params: {"fields1": "...", "secid": "1.000001"}
    """
    from eastmoney_intelligence import eastmoney_json

    base = "https://push2his.eastmoney.com"
    if params:
        qs = urlencode(params)
        url = f"{base}{path}?{qs}"
    else:
        url = f"{base}{path}"
    return eastmoney_json(url, required_path=("data",), required_type=dict)


def fetch_tencent_kline(code: str, market: str = "sz", days: int = 60,
                        ktype: str = "day") -> List[Dict[str, Any]]:
    """腾讯历史K线"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={market}{code},{ktype},,,{days},qfq"
    try:
        data = http_get_json(url, timeout=10)
    except DataSourceError:
        return []

    key_map = {"day": "qfqday", "week": "qfqweek", "month": "qfqmonth",
               "60": "qfq60", "30": "qfq30"}
    key = key_map.get(ktype, "qfqday")
    fallback = ktype if ktype in ("day", "week", "month") else "day"

    stock_data = data.get("data", {}).get(f"{market}{code}", {})
    klines = stock_data.get(key, []) or stock_data.get(fallback, [])

    result = []
    for k in klines[-days:]:
        result.append({
            "date": k[0], "open": float(k[1]), "close": float(k[2]),
            "high": float(k[3]), "low": float(k[4]), "volume": float(k[5]),
        })
    return result


def parse_tencent_minute_response(data: Dict[str, Any], code: str, market: str) -> List[Dict[str, Any]]:
    """从 minute/query 的原始 JSON 提取逐分钟累计量价（纯函数，不触网，可单测）。

    每行形如 "0930 10.20 17257 17602140.00" = 时间 现价 累计成交量(手) 累计成交额(元)。
    """
    rows = (
        (data or {}).get("data", {})
        .get(f"{market}{code}", {})
        .get("data", {})
        .get("data", [])
    )
    result = []
    for row in rows:
        parts = str(row).split()
        if len(parts) < 4:
            continue
        try:
            result.append({
                "time": parts[0],
                "price": float(parts[1]),
                "cum_volume": float(parts[2]),
                "cum_amount": float(parts[3]),
            })
        except ValueError:
            continue
    return result


# ======================== 腾讯分时节流 ========================
# 2026-08-03: proxy.finance.qq.com 分时接口在高频并发下会触发 WAF 限流
# （eod_anomaly_scanner 全量 2843 只曾 0 命中）。模块级锁保证同一进程内
# 两次分时请求间隔 >= _TENCENT_MINUTE_INTERVAL 秒，实测 8 并发 ~8/s 稳定。
_TENCENT_MINUTE_INTERVAL = 0.05  # 秒
_tencent_minute_lock = threading.Lock()
_tencent_minute_last_ts = 0.0


def _minute_throttle() -> None:
    global _tencent_minute_last_ts
    with _tencent_minute_lock:
        now = time.monotonic()
        wait = _TENCENT_MINUTE_INTERVAL - (now - _tencent_minute_last_ts)
        if wait > 0:
            time.sleep(wait)
        _tencent_minute_last_ts = time.monotonic()


def fetch_tencent_minute(code: str, market: str = "sz") -> List[Dict[str, Any]]:
    """腾讯当日分时数据。盘中调用只返回截至当前分钟的数据；收盘后返回全天 9:30-15:00。

    2026-08-03: web.ifzq.gtimg.cn 分时接口被 WAF 拦截(501)，改用备用域名
    proxy.finance.qq.com（返回结构一致，见 parse_tencent_minute_response）。
    备用域名高频并发会触发限流（全量扫描曾 0 命中），所以加了模块级节流：
    每只股票请求间隔 >= _TENCENT_MINUTE_INTERVAL，配合 eod_anomaly_scanner
    的 8 并发，实测 ~8/s 可稳定跑完全市场。
    """
    _minute_throttle()
    hosts = (
        "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/minute/query",
        "https://web.ifzq.gtimg.cn/appstock/app/minute/query",
    )
    for host in hosts:
        url = f"{host}?code={market}{code}"
        try:
            data = http_get_json(url, timeout=10)
        except DataSourceError:
            continue
        return parse_tencent_minute_response(data, code, market)
    return []


def fetch_tencent_hk_quote(code_hk: str) -> Dict[str, Any]:
    """港股实时行情"""
    code = code_hk.replace("hk", "")
    url = f"http://qt.gtimg.cn/q=hk{code}"
    try:
        raw = request_text(
            url,
            source="tencent_hk",
            timeout=10,
            max_attempts=2,
            encoding="gbk",
            headers={"User-Agent": "Mozilla/5.0"},
        ).data
    except DataSourceError as exc:
        raise DataSourceError(
            "tencent_hk",
            f"港股行情失败: {exc.message}",
            exc,
            error_type=exc.error_type,
            attempts=exc.attempts,
            timestamp=exc.timestamp,
            status_code=exc.status_code,
        ) from exc

    parts = raw.split("=")[1].strip().strip('"').split("~")
    if len(parts) < 30:
        raise DataSourceError("tencent_hk", "数据不完整")
    return {
        "price": float(parts[3]) if parts[3] else None,
        "prev_close": float(parts[4]) if parts[4] else None,
        "change_pct": float(parts[32]) if parts[32] else None,
        "amount": float(parts[37]) * 10000 if parts[37] else None,
        "pe": float(parts[39]) if parts[39] else None,
        "market_cap": float(parts[44]) if parts[44] else None,
    }
