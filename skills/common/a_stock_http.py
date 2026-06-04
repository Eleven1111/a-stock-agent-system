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

import json
import os
import sys
import urllib.request
import urllib.error
from typing import Dict, Any, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import env_file as _env_file


class DataSourceError(Exception):
    """数据源异常，携带源名称和错误信息"""
    def __init__(self, source: str, message: str, original: Exception = None):
        self.source = source
        self.message = message
        self.original = original
        super().__init__(f"[{source}] {message}")


def load_hermes_env() -> Dict[str, str]:
    """加载 $HERMES_HOME/.env（默认 ~/.hermes/.env）到 os.environ"""
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
    return dict(os.environ)


def http_get_json(url: str, headers: Dict = None, timeout: int = 10,
                  encoding: str = "utf-8", no_proxy: bool = True) -> Dict[str, Any]:
    """标准 HTTP GET → JSON，自动处理 NO_PROXY"""
    default_headers = {"User-Agent": "Mozilla/5.0 (Hermes A-Stock Agent)"}
    if headers:
        default_headers.update(headers)

    req = urllib.request.Request(url, headers=default_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            text = raw.decode(encoding)
            return json.loads(text)
    except urllib.error.HTTPError as e:
        raise DataSourceError(url, f"HTTP {e.code}: {e.reason}", e)
    except urllib.error.URLError as e:
        raise DataSourceError(url, f"网络错误: {e.reason}", e)
    except json.JSONDecodeError as e:
        raise DataSourceError(url, "JSON解析失败", e)
    except Exception as e:
        raise DataSourceError(url, str(e), e)


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
    形如: v_sz002156="1~通富微电~002156~...~"
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


def fetch_tencent_quote(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    腾讯实时行情
    codes: ["sh600011", "sz002156", "hk00700"]
    返回: {code: {price, change_pct, volume, amount, ...}}
    """
    url = "http://qt.gtimg.cn/q=" + ",".join(codes)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("gbk")
    except Exception as e:
        raise DataSourceError("tencent", f"行情请求失败: {e}", e)

    results = {}
    for line in raw.strip().split("\n"):
        parsed = parse_tencent_quote_line(line)
        if parsed:
            results[parsed["code"]] = parsed["fields"]
    return results


def fetch_tencent_snapshot(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    腾讯实时行情 + 五档盘口（一次请求合并），供集合竞价采集使用。
    返回: {code: {price, prev_close, volume, market_cap, name, ..., bids, asks}}
    """
    url = "http://qt.gtimg.cn/q=" + ",".join(codes)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("gbk")
    except Exception as e:
        raise DataSourceError("tencent", f"行情请求失败: {e}", e)

    results: Dict[str, Dict[str, Any]] = {}
    for line in raw.strip().split("\n"):
        quote = parse_tencent_quote_line(line)
        if not quote:
            continue
        book = parse_tencent_orderbook_line(line) or {"bids": [], "asks": []}
        results[quote["code"]] = {**quote["fields"], "bids": book["bids"], "asks": book["asks"]}
    return results


def fetch_eastmoney_json(path: str, params: Dict = None) -> Dict[str, Any]:
    """
    东方财富数据中心 API
    path: "/api/qt/kamt.kline/get"
    params: {"fields1": "...", "secid": "1.000001"}
    """
    base = "https://push2his.eastmoney.com"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{base}{path}?{qs}"
    else:
        url = f"{base}{path}"
    return http_get_json(url, timeout=10)


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


def fetch_tencent_hk_quote(code_hk: str) -> Dict[str, Any]:
    """港股实时行情"""
    code = code_hk.replace("hk", "")
    url = f"http://qt.gtimg.cn/q=hk{code}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("gbk")
    except Exception as e:
        raise DataSourceError("tencent_hk", f"港股行情失败: {e}", e)

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
