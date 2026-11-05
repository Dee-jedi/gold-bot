"""
Downloads BTC/USD candles from Bitstamp's public API straight at the timeframes
the backtest needs (default 15M and 1H, ~400 requests for 9 years).

Usage:
  python download_bitstamp.py btcusd BTCUSD 2018-01-01 2026-11-05 [15M,1H,5M]

Output: data/<SYMBOL>_<TF>.pkl  (UTC; spread 0 - the backtest charges config.SYMBOL_SPECS costs)
"""
import json
import os
import sys
import time
import urllib.request
from datetime import timezone

import pandas as pd

API = "https://www.bitstamp.net/api/v2/ohlc/{pair}/?step={step}&limit=1000&start={start}"
STEP = {"1M": 60, "5M": 300, "15M": 900, "1H": 3600}
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def get(url, retries=8):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (historical-data research)"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(min(60, 2 ** attempt))
    raise RuntimeError(f"failed: {url}")


def download(pair, sym, start, end, tf):
    step = STEP[tf]
    t = int(pd.Timestamp(start).tz_localize(timezone.utc).timestamp())
    t_end = int((pd.Timestamp(end) + pd.Timedelta(days=1)).tz_localize(timezone.utc).timestamp())
    rows = []
    while t < t_end:
        data = get(API.format(pair=pair, step=step, start=t))["data"]["ohlc"]
        data = [r for r in data if t <= int(r["timestamp"]) < t_end]
        if not data:
            t += step * 1000
            continue
        rows += data
        t = int(data[-1]["timestamp"]) + step
        if len(rows) % 20000 < 1000:
            print(f"  {tf}: up to {pd.to_datetime(t, unit='s'):%Y-%m-%d}", flush=True)
        time.sleep(0.2)
    df = pd.DataFrame(rows)
    df = pd.DataFrame({
        "time": pd.to_datetime(df["timestamp"].astype(int), unit="s"),
        "open": df["open"].astype(float), "high": df["high"].astype(float),
        "low": df["low"].astype(float), "close": df["close"].astype(float),
        "tick_volume": df["volume"].astype(float)})
    df = df.drop_duplicates("time").sort_values("time").reset_index(drop=True)
    df["spread"] = 0
    os.makedirs(DATA, exist_ok=True)
    df.to_pickle(os.path.join(DATA, f"{sym}_{tf}.pkl"))
    print(f"{sym} {tf}: {len(df):,} bars  {df['time'].iloc[0]} .. {df['time'].iloc[-1]}")


if __name__ == "__main__":
    if len(sys.argv) not in (5, 6):
        print(__doc__)
        sys.exit(1)
    tfs = sys.argv[5].split(",") if len(sys.argv) == 6 else ["15M", "1H"]
    for tf in tfs:
        download(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], tf)
