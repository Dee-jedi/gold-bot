"""
Downloads free historical 1-minute BID candles from Dukascopy and builds
UTC-timestamped OHLC files (M1, M5, M15, H1) that backtest.py can read.

Usage:
  python download_dukascopy.py XAUUSD 2018-01-01 2026-11-05
  python download_dukascopy.py EURUSD 2018-01-01 2026-11-05

Output: data/<SYMBOL>_<TF>.pkl   (raw daily files cached in data/raw/ so re-runs resume)

Notes:
  * Times are UTC. Columns: time, open, high, low, close, tick_volume, spread.
    Dukascopy candles have no spread, so `spread` is 0 and the backtest's
    BT_MIN_SPREAD_POINTS floor is what gets charged.
  * Bars with zero volume (market closed) are dropped.
"""
import lzma
import os
import struct
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

URL = "https://datafeed.dukascopy.com/datafeed/{sym}/{y:04d}/{m:02d}/{d:02d}/BID_candles_min_1.bi5"
DIVISOR = {"XAUUSD": 1000, "XAGUSD": 1000, "USDJPY": 1000, "GBPJPY": 1000, "EURJPY": 1000}  # default 100000
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")


WORKERS = int(os.environ.get("DUKA_WORKERS", "1"))    # Dukascopy throttles hard (HTTP 503); keep it low
PAUSE = float(os.environ.get("DUKA_PAUSE", "1.5"))     # seconds between requests per worker


def fetch_day(sym, day, retries=12):
    """Returns raw .bi5 bytes for one UTC day (b'' if the day has no data)."""
    path = os.path.join(DATA, "raw", sym, f"{day:%Y}", f"{day:%m-%d}.bi5")
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    url = URL.format(sym=sym, y=day.year, m=day.month - 1, d=day.day)   # month is 0-indexed
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (historical-data research)"})
    for attempt in range(retries):
        time.sleep(PAUSE)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raw = b""
                break
            time.sleep(min(60, 3 * 2 ** attempt))   # 503 = throttled: back off
        except Exception:
            time.sleep(min(60, 3 * 2 ** attempt))
    else:
        raise RuntimeError(f"failed to download {url}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(raw)
    return raw


def decode(raw, day, divisor):
    if not raw:
        return None
    data = lzma.decompress(raw)
    n = len(data) // 24
    arr = np.frombuffer(data[:n * 24], dtype=np.dtype([("t", ">i4"), ("o", ">i4"), ("c", ">i4"),
                                                       ("l", ">i4"), ("h", ">i4"), ("v", ">f4")]))
    base = np.datetime64(datetime(day.year, day.month, day.day), "s")
    return pd.DataFrame({
        "time": base + arr["t"].astype("timedelta64[s]"),
        "open": arr["o"] / divisor, "high": arr["h"] / divisor,
        "low": arr["l"] / divisor, "close": arr["c"] / divisor,
        "tick_volume": arr["v"].astype(float),
    })


def resample(m1, rule):
    g = m1.set_index("time").resample(rule, label="left", closed="left")
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "tick_volume": g["tick_volume"].sum()}).dropna()
    out["spread"] = 0
    return out.reset_index()


def main(sym, start, end):
    sym = sym.upper()
    divisor = DIVISOR.get(sym, 100000)
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    days = [d0 + timedelta(i) for i in range((d1 - d0).days + 1)]
    days = [d for d in days if d.weekday() != 5]          # no trading on Saturdays
    print(f"{sym}: downloading {len(days)} days ...")

    frames, done = [], 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for day, raw in zip(days, ex.map(lambda d: fetch_day(sym, d), days)):
            df = decode(raw, day, divisor)
            if df is not None:
                frames.append(df)
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(days)}  ({day})", flush=True)

    m1 = pd.concat(frames, ignore_index=True)
    m1 = m1[m1["tick_volume"] > 0].sort_values("time").drop_duplicates("time").reset_index(drop=True)
    m1["spread"] = 0
    os.makedirs(DATA, exist_ok=True)
    m1.to_pickle(os.path.join(DATA, f"{sym}_1M.pkl"))
    print(f"  1M: {len(m1):,} bars  {m1['time'].iloc[0]} .. {m1['time'].iloc[-1]}")
    for tf, rule in (("5M", "5min"), ("15M", "15min"), ("1H", "1h")):
        df = resample(m1, rule)
        df.to_pickle(os.path.join(DATA, f"{sym}_{tf}.pkl"))
        print(f"  {tf}: {len(df):,} bars")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    main(*sys.argv[1:])
