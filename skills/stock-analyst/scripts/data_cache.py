"""
股票数据缓存层
数据源链：
- K线: 腾讯 ifzq → 新浪 → BaoStock (通过 AkShare stock_zh_a_hist_tx)
- 实时: 腾讯 qt.gtimg.cn (主) + AkShare stock_zh_a_spot (新浪备)
- 涨停板: AkShare stock_zt_pool_em (push2ex，通)
- 指数: 腾讯 qt.gtimg.cn
- 板块列表: AkShare stock_board_industry_name_ths (同花顺)
- 个股信息: AkShare stock_individual_spot_xq (雪球)
"""
import sqlite3
import os
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict

COMMON_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "common"))
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

from http_client import DataSourceError, request_bytes, request_json, request_text
from paths import cache_dir
from provider_contract import transport_contract

CACHE_DIR = cache_dir("stock-analyst")
CACHE_DB = os.path.join(CACHE_DIR, "stock_cache.db")
CACHE_TTL = 3600  # 1小时，盘中高频刷新

os.makedirs(CACHE_DIR, exist_ok=True)

def get_db():
    conn = sqlite3.connect(CACHE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    _init_schema(conn)
    return conn

def _init_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS daily_kline (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            amount REAL,
            source TEXT DEFAULT 'tencent',
            cached_at INTEGER DEFAULT (strftime('%s','now')),
            PRIMARY KEY (code, date)
        );
        CREATE INDEX IF NOT EXISTS idx_kline_code ON daily_kline(code);
        CREATE INDEX IF NOT EXISTS idx_kline_date ON daily_kline(date);

        CREATE TABLE IF NOT EXISTS kline_cache_v2 (
            code TEXT NOT NULL,
            provider TEXT NOT NULL,
            adjustment TEXT NOT NULL,
            event_asof TEXT NOT NULL,
            payload TEXT NOT NULL,
            cached_at INTEGER NOT NULL,
            schema_version TEXT NOT NULL,
            PRIMARY KEY (code, provider, adjustment, event_asof)
        );
        CREATE INDEX IF NOT EXISTS idx_kline_v2_lookup
            ON kline_cache_v2(code, adjustment, event_asof, cached_at);

        CREATE TABLE IF NOT EXISTS realtime_quotes (
            code TEXT PRIMARY KEY,
            name TEXT,
            price REAL,
            pct_change REAL,
            volume REAL,
            amount REAL,
            turnover_rate REAL,
            pe REAL,
            high REAL,
            low REAL,
            open REAL,
            pre_close REAL,
            total_mv REAL,
            cached_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS stock_list (
            code TEXT PRIMARY KEY,
            name TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_index (
            code TEXT,
            date TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            amount REAL,
            PRIMARY KEY (code, date)
        );

        CREATE TABLE IF NOT EXISTS sector_list (
            code TEXT PRIMARY KEY,
            name TEXT,
            source TEXT DEFAULT 'ths'
        );
    """)
    conn.commit()

def clear_cache(code=None, days=0):
    """清理缓存。code为None时清理所有，days>0清理N天前的数据"""
    conn = get_db()
    if code:
        conn.execute("DELETE FROM daily_kline WHERE code=?", (code,))
        conn.execute("DELETE FROM kline_cache_v2 WHERE code=?", (code,))
    elif days > 0:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        conn.execute("DELETE FROM daily_kline WHERE date<?", (cutoff,))
        conn.execute("DELETE FROM kline_cache_v2 WHERE event_asof<?", (cutoff,))
    else:
        conn.execute("DELETE FROM kline_cache_v2")
    conn.commit()
    conn.close()

def read_kline_cache(
    code: str,
    *,
    provider: Optional[str] = None,
    adjustment: str = "qfq",
    event_asof: Optional[str] = None,
    now_epoch: Optional[int] = None,
) -> Dict:
    """Read a provenance-bound K-line cache record with explicit degradation."""
    event_asof = event_asof or datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    if provider:
        row = conn.execute(
            """SELECT * FROM kline_cache_v2
               WHERE code=? AND provider=? AND adjustment=? AND event_asof=?""",
            (code, provider, adjustment, event_asof),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT * FROM kline_cache_v2
               WHERE code=? AND adjustment=? AND event_asof=?
               ORDER BY cached_at DESC LIMIT 1""",
            (code, adjustment, event_asof),
        ).fetchone()
    conn.close()
    if row is None:
        return {"status": "cache_miss", "data": None}
    try:
        payload = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError):
        return {"status": "cache_corrupt", "data": None, "provider": row["provider"]}
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        return {"status": "cache_corrupt", "data": None, "provider": row["provider"]}
    age = int(now_epoch or time.time()) - int(row["cached_at"])
    if age < 0 or age > CACHE_TTL:
        return {
            "status": "cache_stale",
            "data": None,
            "provider": row["provider"],
            "age_seconds": age,
        }
    return {
        "status": "ok",
        "data": payload,
        "provider": row["provider"],
        "adjustment": row["adjustment"],
        "event_asof": row["event_asof"],
        "cached_at": row["cached_at"],
        "schema_version": row["schema_version"],
    }


def get_kline_cache(
    code: str,
    *,
    provider: Optional[str] = None,
    adjustment: str = "qfq",
    event_asof: Optional[str] = None,
) -> Optional[List[Dict]]:
    """Compatibility reader: only fresh, valid cache data is returned."""
    result = read_kline_cache(
        code,
        provider=provider,
        adjustment=adjustment,
        event_asof=event_asof,
    )
    return result["data"] if result["status"] == "ok" else None


def save_kline_cache(
    code: str,
    data: List[Dict],
    source: str = "tencent",
    *,
    adjustment: str = "qfq",
    event_asof: Optional[str] = None,
    cached_at: Optional[int] = None,
):
    """Save K-lines under provider/adjustment/asof identity."""
    if not source or adjustment not in {"qfq", "hfq", "unadjusted", "none"}:
        raise ValueError("provider and known adjustment are required")
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValueError("K-line cache payload must be a list of objects")
    conn = get_db()
    now = int(cached_at or time.time())
    event_asof = event_asof or datetime.now().strftime("%Y-%m-%d")
    conn.execute(
        """INSERT OR REPLACE INTO kline_cache_v2
           (code, provider, adjustment, event_asof, payload, cached_at, schema_version)
           VALUES (?,?,?,?,?,?,?)""",
        (
            code,
            source,
            adjustment,
            event_asof,
            json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            now,
            "kline_cache_v2",
        ),
    )
    conn.commit()
    conn.close()


# ─── 数据源层 ───

def _run_python(code_str: str) -> str:
    """在 Hermes venv 中执行 Python 代码"""
    result = subprocess.run(
        [sys.executable, "-c", code_str],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(f"Python error: {result.stderr[:200]}")
    return result.stdout.strip()

def _run_python_with_retry(code_str: str, max_retries=2, base_delay=1.0, timeout=60) -> str:
    """带重试的 _run_python（CDN 间歇性 Empty reply 时自动重试）"""
    import time
    import random
    last_error = None
    attempts = min(max(int(max_retries), 1), 2)
    for attempt in range(attempts):
        try:
            result = subprocess.run(
                [sys.executable, "-c", code_str],
                capture_output=True, text=True, timeout=timeout
            )
            if result.returncode != 0:
                raise RuntimeError(f"Python error: {result.stderr[:200]}")
            return result.stdout.strip()
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            last_error = e
            if attempt < attempts - 1:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                time.sleep(delay)
    raise last_error


# ─── 历史K线 ───

def fetch_kline_from_tencent(code: str, days=120, period="day") -> Optional[List[Dict]]:
    """腾讯 ifzq 历史K线 (已验证可用)
    period: "day"=日线, "week"=周线, "month"=月线"""
    api_code = code if code.startswith(('sh','sz')) else f"sh{code}" if code.startswith('6') else f"sz{code}"
    period_map = {"day": "day", "week": "week", "month": "month"}
    tz_period = period_map.get(period, "day")

    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={api_code},{tz_period},,,{days},qfq"

    try:
        data = request_json(
            url,
            source="tencent_kline",
            timeout=10,
            max_attempts=2,
            headers={"User-Agent": "Mozilla/5.0"},
        ).data

        stock_data = data.get('data', {}).get(api_code, {})
        klines = stock_data.get('qfqday', []) or stock_data.get('day', [])

        results = []
        for k in klines:
            results.append({
                'date': str(k[0]),
                'open': float(k[1]),
                'close': float(k[2]),
                'high': float(k[3]),
                'low': float(k[4]),
                'volume': float(k[5]) if len(k) > 5 else 0,
                'amount': float(k[6]) if len(k) > 6 else 0,
            })
        return results
    except (DataSourceError, AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None  # fall through

def fetch_kline_from_sina(code: str, days=120) -> Optional[List[Dict]]:
    """新浪历史K线"""
    api_code = code if code.startswith(('sh','sz')) else f"sh{code}" if code.startswith('6') else f"sz{code}"
    url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={api_code}&scale=240&ma=5&datalen={days}"

    try:
        data = request_json(
            url,
            source="sina_kline",
            timeout=10,
            max_attempts=2,
            encoding="gbk",
            headers={"User-Agent": "Mozilla/5.0"},
        ).data
        results = []
        for k in data:
            results.append({
                'date': k['day'],
                'open': float(k['open']),
                'high': float(k['high']),
                'low': float(k['low']),
                'close': float(k['close']),
                'volume': float(k.get('volume', 0)) / 100,
                'amount': 0,
            })
        return results
    except (DataSourceError, AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None

def fetch_kline_from_akshare_tx(code: str, days=120) -> Optional[List[Dict]]:
    """AkShare 腾讯版历史K线 (stock_zh_a_hist_tx)"""
    # 格式化代码为 ak 需要的格式
    prefix = "sh" if code.startswith('6') else "sz"
    api_code = f"{prefix}{code}"
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days + 20)).strftime("%Y%m%d")

    py_code = f"""
import akshare as ak, json, os
for k in ['HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY','all_proxy']:
    os.environ.pop(k, None)
try:
    df = ak.stock_zh_a_hist_tx(symbol="{api_code}", start_date="{start}", end_date="{end}")
    if df is not None and not df.empty:
        rows = []
        for _, r in df.iterrows():
            rows.append({{
                'date': str(r['date']),
                'open': float(r['open']),
                'high': float(r['high']),
                'low': float(r['low']),
                'close': float(r['close']),
                'volume': float(r.get('volume', 0)),
                'amount': float(r.get('amount', 0)),
            }})
        print(json.dumps(rows, ensure_ascii=False, default=str))
    else:
        print('[]')
except Exception as e:
    print('[]')
"""
    try:
        out = _run_python_with_retry(py_code)
        data = json.loads(out)
        return data if data else None
    except Exception:
        return None

def fetch_kline_from_baostock(code: str, days=120) -> Optional[List[Dict]]:
    """BaoStock 补充历史数据 (免费，无需API key)"""
    api_code = f"sh.{code}" if code.startswith('6') else f"sz.{code}"
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days + 30)).strftime("%Y-%m-%d")

    code_str = f"""
import baostock as bs
import json
lg = bs.login()
if lg.error_code != '0':
    print('ERROR:' + lg.error_msg)
else:
    rs = bs.query_history_k_data_plus(
        "{api_code}",
        "date,open,high,low,close,volume,amount",
        start_date="{start_date}", end_date="{end_date}",
        frequency="d", adjustflag="2")
    rows = []
    while rs.next():
        row = rs.get_row_data()
        rows.append({{'date': row[0], 'open': float(row[1]), 'high': float(row[2]),
                      'low': float(row[3]), 'close': float(row[4]),
                      'volume': float(row[5]), 'amount': float(row[6])}})
    bs.logout()
    print(json.dumps(rows, ensure_ascii=False))
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", code_str],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0 or result.stdout.startswith("ERROR"):
            return None
        data = json.loads(result.stdout.strip())
        return data if data else None
    except Exception:
        return None

def fetch_kline(code: str, days=120, force_refresh=False, period="day") -> List[Dict]:
    """统一K线获取入口，含缓存：腾讯 → AkShare腾讯版 → 新浪 → BaoStock"""
    if not force_refresh and period == "day":
        cached = get_kline_cache(code)
        if cached and len(cached) >= days * 0.8:
            return cached

    data = fetch_kline_from_tencent(code, days, period)
    source = "tencent"

    if not data:
        data = fetch_kline_from_akshare_tx(code, days)
        source = "akshare_tx"

    if not data:
        data = fetch_kline_from_akshare_tx(code, days)
        source = "akshare_tx"

    if not data:
        data = fetch_kline_from_sina(code, days)
        source = "sina"

    if not data:
        data = fetch_kline_from_baostock(code, days)
        source = "baostock"

    if data and period == "day":
        save_kline_cache(code, data, source)

    return data or []


# ─── 实时行情 ───

def fetch_realtime(codes: List[str]) -> Dict:
    """腾讯API批量实时行情（主力，跨越TUN封锁）"""
    prefix_map = {}
    api_codes = []
    for c in codes:
        prefix = "sh" if c.startswith('6') else "sz"
        api = f"{prefix}{c}"
        api_codes.append(api)
        prefix_map[api] = c

    url = f"http://qt.gtimg.cn/q={','.join(api_codes)}"
    response = request_bytes(
        url,
        source="tencent",
        timeout=10,
        max_attempts=2,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    raw = response.data
    trust = transport_contract(url)

    try:
        text = raw.decode('gbk')
    except UnicodeDecodeError:
        text = raw.decode('utf-8', errors='replace')

    results = {}
    for line in text.strip().split('\n'):
        if '=' not in line:
            continue
        val = line.split('=', 1)[1].strip().strip('"').strip("'")
        parts = val.split('~')
        if len(parts) < 40:
            continue

        api_code = line.split('=')[0].strip('_').strip()
        code = prefix_map.get(api_code, api_code[-6:] if len(api_code) >= 6 else api_code)

        try:
            results[code] = {
                'name': parts[1],
                'code': parts[2] if len(parts) > 2 else code,
                'price': float(parts[3]) if parts[3] else 0,
                'pct_change': float(parts[32]) if parts[32] else 0,
                'change': float(parts[31]) if parts[31] else 0,
                'open': float(parts[5]) if parts[5] else 0,
                'high': float(parts[33]) if parts[33] else 0,
                'low': float(parts[34]) if parts[34] else 0,
                'pre_close': float(parts[4]) if parts[4] else 0,
                'volume': float(parts[6]) if parts[6] else 0,
                'amount': float(parts[37]) * 10000 if parts[37] else 0,
                'turnover_rate': float(parts[38]) if parts[38] else 0,
                'pe': float(parts[39]) if parts[39] else 0,
                'total_mv': float(parts[45]) if len(parts) > 45 and parts[45] else 0,
                'provider': 'tencent',
                'fetched_at': response.fetched_at,
                'transport_trust': trust['trust'],
                'directional_eligible': trust['directional_eligible'],
            }
        except (ValueError, IndexError):
            continue

    return results


# ─── 涨停板数据 ───

def fetch_zt_pool(date: str = None) -> List[Dict]:
    """涨停板池（AkShare stock_zt_pool_em，走 push2ex，通）"""
    if date is None:
        date = datetime.now().strftime("%Y%m%d")

    code = f"""
import akshare as ak, os, json, pandas as pd
for k in ['HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY','all_proxy']:
    os.environ.pop(k, None)
df = ak.stock_zt_pool_em(date='{date}')
if df.empty:
    print('[]')
else:
    cols = ['代码','名称','涨跌幅','连板数','封板资金','所属行业','涨停统计']
    available = [c for c in cols if c in df.columns]
    rows = []
    for _, r in df.iterrows():
        row = {{}}
        for c in available:
            v = r[c]
            if c == '封板资金' and pd.notna(v):
                v = float(v) / 1e8
            row[c] = str(v) if pd.notna(v) else ''
        rows.append(row)
    print(json.dumps(rows, ensure_ascii=False, default=str))
"""
    try:
        out = _run_python_with_retry(code)
        return json.loads(out) if out else []
    except Exception:
        return []


# ─── 板块列表（AkShare 同花顺版） ───

def fetch_sectors_ths() -> Dict[str, List[Dict]]:
    """获取同花顺行业板块和概念板块列表（走 AkShare stock_board_industry_name_ths，不走 push2）"""
    py_code = """
import akshare as ak, json, os
for k in ['HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY','all_proxy']:
    os.environ.pop(k, None)
try:
    ind = ak.stock_board_industry_name_ths()
    ind_list = [{"name": r['name'], "code": str(r['code'])} for _, r in ind.iterrows()]

    con = ak.stock_board_concept_name_ths()
    con_list = [{"name": r['name'], "code": str(r['code'])} for _, r in con.iterrows()]

    print(json.dumps({"industry": ind_list, "concept": con_list}, ensure_ascii=False))
except Exception as e:
    print(json.dumps({"error": str(e)}))
"""
    try:
        out = _run_python_with_retry(py_code)
        return json.loads(out)
    except Exception:
        return {}

def save_sectors_to_cache(sectors: Dict):
    """缓存板块列表到 SQLite"""
    conn = get_db()
    conn.execute("DELETE FROM sector_list")
    for cat, items in sectors.items():
        for item in items:
            conn.execute(
                "INSERT OR REPLACE INTO sector_list (code, name, source) VALUES (?,?,?)",
                (item['code'], item['name'], cat)
            )
    conn.commit()
    conn.close()

def get_sectors_from_cache() -> List[Dict]:
    """从缓存读取板块列表"""
    conn = get_db()
    rows = conn.execute("SELECT * FROM sector_list ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── 个股信息（AkShare 雪球版） ───

def fetch_stock_info_xq(code: str) -> Optional[Dict]:
    """雪球个股基本信息（stock_individual_spot_xq）"""
    prefix = "SH" if code.startswith('6') else "SZ"
    api_code = f"{prefix}{code}"

    py_code = f"""
import akshare as ak, json, os
for k in ['HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY','all_proxy']:
    os.environ.pop(k, None)
try:
    df = ak.stock_individual_spot_xq(symbol="{api_code}")
    if df is not None and not df.empty:
        print(df.to_json(orient='records', force_ascii=False))
    else:
        print('[]')
except Exception as e:
    print('[]')
"""
    try:
        out = _run_python_with_retry(py_code)
        data = json.loads(out)
        return data[0] if data else None
    except Exception:
        return None


# ─── 大盘指数 ───

def fetch_index(code="sh000001") -> Dict:
    """腾讯指数行情"""
    url = f"http://qt.gtimg.cn/q={code}"
    raw = request_text(
        url,
        source="tencent",
        timeout=10,
        max_attempts=2,
        encoding="gbk",
        headers={"User-Agent": "Mozilla/5.0"},
    ).data

    val = raw.split('=')[1].strip().strip('"')
    parts = val.split('~')

    return {
        'name': parts[1],
        'price': float(parts[3]) if parts[3] else 0,
        'pct_change': float(parts[32]) if parts[32] else 0,
        'change': float(parts[31]) if parts[31] else 0,
        'high': float(parts[33]) if parts[33] else 0,
        'low': float(parts[34]) if parts[34] else 0,
        'volume': float(parts[36].split('/')[1]) if len(parts) > 36 and '/' in parts[36] else 0,
        'amount': float(parts[37]) * 10000 if parts[37] else 0,
    }


# ─── 全部A股列表（AkShare Sina版） ───

def fetch_stock_list() -> List[Dict]:
    """获取全部A股列表（走新浪 stock_zh_a_spot，不是 EM push2）"""
    py_code = """
import akshare as ak, json, os
for k in ['HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY','all_proxy']:
    os.environ.pop(k, None)
try:
    df = ak.stock_zh_a_spot()
    rows = []
    for _, r in df.iterrows():
        rows.append({'code': r['代码'], 'name': r['名称']})
    print(json.dumps(rows, ensure_ascii=False))
except Exception as e:
    print(json.dumps({"error": str(e)}))
"""
    try:
        out = _run_python_with_retry(py_code)
        return json.loads(out) if out.startswith('[') else []
    except Exception:
        return []


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "realtime"

    if cmd == "realtime":
        codes = sys.argv[2].split(",") if len(sys.argv) > 2 else ["600519"]
        data = fetch_realtime(codes)
        for code, info in data.items():
            arrow = "🟢" if info['pct_change'] >= 0 else "🔴"
            print(f"{arrow} {info['name']}({code}): {info['price']:.2f} | {info['pct_change']:+.2f}% | 成交{info['amount']/1e8:.2f}亿 | 换手{info['turnover_rate']}%")

    elif cmd == "kline":
        code = sys.argv[2] if len(sys.argv) > 2 else "600519"
        days = int(sys.argv[3]) if len(sys.argv) > 3 else 60
        data = fetch_kline(code, days)
        print(f"{code} 近{days}日K线: {len(data)}条")
        for k in data[-10:]:
            print(f"  {k['date']} | O:{k['open']:.2f} H:{k['high']:.2f} L:{k['low']:.2f} C:{k['close']:.2f} V:{k['volume']:.0f}")

    elif cmd == "zt":
        date = sys.argv[2] if len(sys.argv) > 2 else datetime.now().strftime("%Y%m%d")
        data = fetch_zt_pool(date)
        print(f"{date} 涨停板共{len(data)}家")
        for d in data[:20]:
            print(f"  {d.get('名称','?')}({d.get('代码','?')}) | {d.get('涨跌幅','')}% | {d.get('连板数','')}板 | 行业:{d.get('所属行业','')}")

    elif cmd == "sectors":
        sec = fetch_sectors_ths()
        ind = sec.get("industry", [])
        con = sec.get("concept", [])
        print(f"同花顺行业板块: {len(ind)}个")
        print(f"同花顺概念板块: {len(con)}个")
        print(f"前10行业: {', '.join(s['name'] for s in ind[:10])}")

    elif cmd == "stocklist":
        stocks = fetch_stock_list()
        print(f"全部A股: {len(stocks)}只")
        print(f"前10: {', '.join(s['name'] for s in stocks[:10])}")

    elif cmd == "clear":
        code = sys.argv[2] if len(sys.argv) > 2 else None
        days = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        clear_cache(code, days)
        print("Cache cleared")

    elif cmd == "index":
        idx = fetch_index()
        arrow = "🟢" if idx['pct_change'] >= 0 else "🔴"
        print(f"{arrow} {idx['name']}: {idx['price']:.2f} | {idx['pct_change']:+.2f}% | 成交{idx['amount']/1e8:.2f}亿")
