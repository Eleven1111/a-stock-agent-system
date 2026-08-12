#!/usr/bin/env python3
"""
Fast tail anomaly scanner - optimized version for cron jobs.
Fetches minute data only for top-N most liquid stocks.
"""
import sys, time, json
sys.path.insert(0, '.')
sys.path.insert(0, 'skills/common')
sys.path.insert(0, 'skills/stock-triage/scripts')

from market_adapters import fetch_a_share_spot, fetch_tencent_minute, fetch_tencent_kline
from datetime import date

TAIL_START = "1430"
TAIL_END = "1500"
BUCKETS = 7
VOLUME_RATIO_MIN = 2.5
PRICE_CHANGE_MIN = 1.5
POSITION_MAX = 70.0
TOP_N = 200  # Only scan top 200 most liquid stocks

def is_excluded(name):
    u = (name or "").upper()
    return "ST" in u or "退" in (name or "")

def market_of(code):
    return "sh" if str(code).startswith(("6","9")) else "sz"

def fetch_spot():
    print("Fetching spot data...", file=sys.stderr)
    t0 = time.time()
    df = fetch_a_share_spot()
    rows = df.to_dict("records")
    print(f"  {len(rows)} stocks in {time.time()-t0:.1f}s", file=sys.stderr)
    return rows

def screen(rows):
    screened = []
    for r in rows:
        name = str(r.get("名称") or "")
        code = str(r.get("代码") or "").strip()
        # Strip prefix
        for p in ("sh","sz","bj"):
            if code.startswith(p):
                code = code[2:]
                break
        if code.startswith(("920","8","4")) or code.startswith("bj"):
            continue
        code = code.zfill(6)
        if not code or is_excluded(name):
            continue
        try:
            price = float(r.get("最新价") or 0)
            amount = float(r.get("成交额") or 0)
            mc_raw = r.get("总市值")
            mc = float(mc_raw) if mc_raw not in (None,"","-") else None
            pe_raw = r.get("市盈率-动态")
            pe = float(pe_raw) if pe_raw not in (None,"","-") else None
        except (TypeError, ValueError):
            continue
        if price <= 0 or amount < 1e8:
            continue
        if mc is not None and mc < 50e8:
            continue
        if pe is not None and (pe < 0 or pe > 100):
            continue
        screened.append({
            "code": code, "name": name, "price": price,
            "amount": amount, "market_cap": mc, "pe": pe,
        })
    # Sort by amount desc, take top N
    screened.sort(key=lambda x: -x["amount"])
    return screened[:TOP_N]

def tail_anomaly(rows):
    if not rows:
        return None
    rows = sorted([r for r in rows if r.get("time")], key=lambda r: r["time"])
    before = [r for r in rows if r["time"] < TAIL_START]
    tail = [r for r in rows if r["time"] >= TAIL_START]
    if not before or not tail:
        return None
    # Check coverage - need data up to 1500
    if max(r["time"] for r in rows) < TAIL_END:
        return None
    baseline_vol = before[-1]["cum_volume"]
    tail_vol = tail[-1]["cum_volume"] - baseline_vol
    avg_vol = baseline_vol / BUCKETS
    if avg_vol <= 0:
        return None
    tail_price_chg = (tail[-1]["price"] - before[-1]["price"]) / before[-1]["price"] * 100
    return {
        "tail_volume_ratio": round(tail_vol / avg_vol, 2),
        "tail_price_change_pct": round(tail_price_chg, 2),
    }

def position_60d(code, market):
    try:
        bars = fetch_tencent_kline(code, market=market, days=60)
        if len(bars) < 2:
            return None
        high = max(b["high"] for b in bars)
        low = min(b["low"] for b in bars)
        if high <= low:
            return None
        return round((bars[-1]["close"] - low) / (high - low) * 100, 1)
    except Exception:
        return None

def main():
    asof = date.today().isoformat()
    
    # Step 1: Fetch spot
    rows = fetch_spot()
    universe_count = len(rows)
    
    # Step 2: Screen
    screened = screen(rows)
    screened_count = len(screened)
    print(f"Screened: {screened_count} stocks", file=sys.stderr)
    
    # Step 3: Fetch minute data for screened stocks
    print(f"Fetching minute data for {screened_count} stocks...", file=sys.stderr)
    t0 = time.time()
    signals = {}
    for i, s in enumerate(screened):
        code = s["code"]
        market = market_of(code)
        try:
            minute_rows = fetch_tencent_minute(code, market=market)
            sig = tail_anomaly(minute_rows)
            if sig:
                signals[code] = sig
        except Exception:
            pass
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{screened_count} done ({time.time()-t0:.0f}s elapsed)", file=sys.stderr)
    print(f"  Minute data done in {time.time()-t0:.1f}s, {len(signals)} signals", file=sys.stderr)
    
    # Step 4: Filter by thresholds
    anomaly_codes = [c for c, s in signals.items() 
                     if s["tail_volume_ratio"] >= VOLUME_RATIO_MIN 
                     and s["tail_price_change_pct"] >= PRICE_CHANGE_MIN]
    print(f"  Anomaly candidates: {len(anomaly_codes)}", file=sys.stderr)
    
    # Step 5: Fetch 60d positions for anomaly codes
    positions = {}
    for code in anomaly_codes:
        market = market_of(code)
        positions[code] = position_60d(code, market)
    
    # Step 6: Build candidates
    screened_by_code = {s["code"]: s for s in screened}
    candidates = []
    for code in anomaly_codes:
        pos = positions.get(code)
        if pos is not None and pos >= POSITION_MAX:
            continue
        base = screened_by_code.get(code, {})
        sig = signals[code]
        candidates.append({
            "code": code,
            "name": base.get("name"),
            "price": base.get("price"),
            "change_pct": base.get("change_pct", 0),
            "volume_ratio": sig["tail_volume_ratio"],
            "tail_price_change_pct": sig["tail_price_change_pct"],
            "position_60d_pct": pos,
            "pe": base.get("pe"),
            "market_cap": base.get("market_cap"),
            "anomaly_strength": round(sig["tail_volume_ratio"] * sig["tail_price_change_pct"], 2),
        })
    
    # Sort by anomaly strength
    candidates.sort(key=lambda x: -x["anomaly_strength"])
    
    # Build result
    result = {
        "schema": "eod_anomaly_scan_v1",
        "asof": asof,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "ok",
        "universe_count": universe_count,
        "screened_count": screened_count,
        "tail_signal_count": len(candidates),
        "candidates": candidates,
        "data_source": "tencent_minute"
    }
    
    print(json.dumps(result, ensure_ascii=False))
    
    # Also write to archive
    import os
    data_dir = "skills/stock-triage/data"
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "eod_anomaly_latest.json"), "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
