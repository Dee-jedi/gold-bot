"""
Realistic bar-by-bar backtest for the SMC gold bot.

How it mimics live trading:
  * Signals are evaluated at the CLOSE of each LTF bar, using only HTF bars that
    had already closed by then (no look-ahead).
  * Orders fill at the OPEN of the next bar: BUY at ask (bid + spread + slippage),
    SELL at bid (- slippage). Spread = that bar's MT5 spread, never below
    the symbol's min_spread in config.SYMBOL_SPECS.
  * Stops: BUY stop triggers on bid low, SELL stop on ask high. If price gaps
    through the stop, the fill is the (worse) open price, plus slippage.
  * If SL and TP are both touched inside one bar we assume the SL hit first.
  * Lot size = equity x RISK_PERCENT / loss-per-lot at the SL, rounded DOWN to
    0.01; trade skipped if below the minimum lot. Commission and swap applied.
  * Trailing / breakeven updates use closed-bar data and apply from the next bar.

Usage:
  python backtest.py                         # config timeframes, fresh MT5 data
  python backtest.py --ltf 5M --cache DIR    # use cached pickles XAUUSD_<TF>.pkl
  python backtest.py --set USE_FVG_POI=True --set EXIT_MODE=rr
"""
import argparse
import math
import os
from contextlib import contextmanager

import numpy as np
import pandas as pd

import config
from strategy_smc import SMCStrategy

TF_MIN = {"1M": 1, "3M": 3, "5M": 5, "15M": 15, "1H": 60, "1D": 1440}
MIN_LOT, LOT_STEP = 0.01, 0.01


@contextmanager
def overrides(**kw):
    """Temporarily override config values."""
    old = {k: getattr(config, k) for k in kw if hasattr(config, k)}
    for k, v in kw.items():
        setattr(config, k, v)
    try:
        yield
    finally:
        for k in kw:
            if k in old:
                setattr(config, k, old[k])
            else:
                delattr(config, k)


def load_data(tf, cache_dir=None):
    if cache_dir:
        return pd.read_pickle(os.path.join(cache_dir, f"{config.SYMBOL}_{tf}.pkl"))
    from data_feed import DataFeed
    feed = DataFeed()
    try:
        return feed.get_data(tf, num_bars=99999)
    finally:
        feed.shutdown()


def in_kill_zone(ts):
    t = ts.time()
    return (config.LONDON_OPEN_START <= t <= config.LONDON_OPEN_END or
            config.NY_OPEN_START <= t <= config.NY_OPEN_END)


def run_backtest(htf_df, ltf_df, htf_tf, ltf_tf, start=None, end=None,
                 starting_equity=10_000.0, trend_df=None, **kw):
    """Runs one backtest. Extra keyword args temporarily override config values.
    trend_df: bars of config.TREND_TF, required when TREND_FILTER is on."""
    with overrides(**kw):
        return _run(htf_df, ltf_df, htf_tf, ltf_tf, start, end, starting_equity, trend_df)


def _run(htf_df, ltf_df, htf_tf, ltf_tf, start, end, starting_equity, trend_df=None):
    strat = SMCStrategy()
    spec = config.SYMBOL_SPECS[config.SYMBOL]
    POINT = spec['point']
    comm = spec['commission']
    htf_df = htf_df.reset_index(drop=True)
    ltf_df = ltf_df.reset_index(drop=True)

    # ---- optional big-picture trend filter, indexed by bar close time ----
    if config.TREND_FILTER:
        if trend_df is None:
            raise ValueError("TREND_FILTER is on: pass trend_df (bars of config.TREND_TF)")
        trend_df = trend_df.reset_index(drop=True)
        trend_dir = strat.trend_direction(trend_df)
        trend_close_t = (trend_df['time'] + pd.Timedelta(minutes=TF_MIN[config.TREND_TF])).to_numpy()

    # ---- HTF: one causal pass, state indexed by HTF bar close time ----
    bias, pois = strat.htf_structure(htf_df)
    pois.sort(key=lambda p: p['created'])
    htf_atr = strat.calculate_atr(htf_df).to_numpy(float)
    htf_close_t = (htf_df['time'] + pd.Timedelta(minutes=TF_MIN[htf_tf])).to_numpy()

    # ---- LTF arrays ----
    t_open = ltf_df['time']
    o, h, l, c = (ltf_df[k].to_numpy(float) for k in ('open', 'high', 'low', 'close'))
    spread = np.maximum(ltf_df['spread'].to_numpy(float), spec['min_spread']) * POINT
    if spec.get('spread_pct'):   # e.g. crypto: spread quoted as % of price
        spread = np.maximum(spread, spec['spread_pct'] * ltf_df['close'].to_numpy(float))
    atr = strat.calculate_atr(ltf_df).to_numpy(float)
    ltf_len = pd.Timedelta(minutes=TF_MIN[ltf_tf])
    decision_t = (t_open + ltf_len)
    decision_np = decision_t.to_numpy()
    server_day = (t_open.dt.tz_localize('UTC').dt.tz_convert(config.SERVER_TIMEZONE)
                  .dt.tz_localize(None).dt.normalize())
    server_day_np = server_day.to_numpy()
    utc_day_np = t_open.dt.normalize().to_numpy()
    slip_arr = np.full(len(ltf_df), spec['slippage'] * POINT)
    if spec.get('slippage_pct'):
        slip_arr = np.maximum(slip_arr, spec['slippage_pct'] * ltf_df['close'].to_numpy(float))
    max_spread = spec.get('max_spread_points', config.MAX_SPREAD_POINTS)
    max_spread = np.inf if max_spread is None else max_spread * POINT
    cs = spec['contract']

    i0 = 60
    if start is not None:
        i0 = max(i0, int(np.searchsorted(t_open.to_numpy(), np.datetime64(pd.Timestamp(start)))))
    i1 = len(ltf_df)
    if end is not None:
        i1 = int(np.searchsorted(t_open.to_numpy(), np.datetime64(pd.Timestamp(end))))

    equity = starting_equity
    trades, curve = [], []
    pos = pending = None
    exit_next_open = False
    used = set()
    active, poi_ptr, hp = [], 0, -1
    day, day_start_eq = None, equity

    def close_pos(t, price, reason, frac=1.0):
        """Close `frac` of the original lots at `price` (already incl. spread/slip)."""
        nonlocal pos, equity
        lots = round(pos['lots'] * frac, 2) if frac < 1 else pos['remaining']
        lots = min(lots, pos['remaining'])
        pnl = (price - pos['entry']) * pos['dir'] * cs * lots
        pnl -= comm * lots
        equity += pnl
        pos['realized'] += pnl
        pos['remaining'] = round(pos['remaining'] - lots, 2)
        if pos['remaining'] <= 1e-9:
            total = pos['realized'] + pos['swap']
            trades.append({
                'dir': 'BUY' if pos['dir'] > 0 else 'SELL', 'entry_time': pos['time'],
                'exit_time': t_open.iloc[t], 'entry': pos['entry'], 'exit': price,
                'sl0': pos['sl0'], 'lots': pos['lots'], 'pnl': total,
                'R': total / pos['risk_usd'], 'reason': reason, 'poi': pos['poi'],
                'poi_top': pos['poi_top'], 'poi_bottom': pos['poi_bottom'],
                'poi_age_htf': pos['poi_age_htf'], 'equity': equity})
            curve.append((t_open.iloc[t], equity))
            pos = None

    for t in range(i0, i1):
        sp = spread[t]
        slip = slip_arr[t]

        # ---------------- 1. fills at this bar's OPEN ----------------
        if pos is not None and exit_next_open:
            close_pos(t, o[t] - slip if pos['dir'] > 0 else o[t] + sp + slip, 'bias_flip')
            exit_next_open = False

        if pending is not None:
            d, sl, poi = pending
            pending = None
            entry = o[t] + sp + slip if d > 0 else o[t] - slip
            loss_per_lot = (entry - sl) * d * cs + comm
            if loss_per_lot > 0:
                lots = math.floor(equity * config.RISK_PERCENT / 100 / loss_per_lot / LOT_STEP + 1e-9) * LOT_STEP
                lots = round(lots, 2)
                if lots >= MIN_LOT:
                    r = abs(entry - sl)
                    tp = None
                    if config.EXIT_MODE in ('rr', 'partial'):
                        tp = entry + d * config.TP_R * r
                    pos = {'dir': d, 'entry': entry, 'sl': sl, 'sl0': sl, 'tp': tp, 'lots': lots,
                           'remaining': lots, 'time': t_open.iloc[t], 'extreme': entry,
                           'risk_usd': lots * loss_per_lot, 'realized': 0.0, 'swap': 0.0,
                           'be_done': False, 'partial_done': False, 'poi': poi['kind'],
                           'poi_top': poi['top'], 'poi_bottom': poi['bottom'],
                           'poi_age_htf': hp - poi['created']}

        # ---------------- 2. intrabar SL / TP ----------------
        if pos is not None:
            d = pos['dir']
            if d > 0:
                if l[t] <= pos['sl']:
                    close_pos(t, min(pos['sl'], o[t]) - slip, 'sl')
                elif pos['tp'] is not None and h[t] >= pos['tp']:
                    if config.EXIT_MODE == 'partial' and not pos['partial_done']:
                        close_pos(t, pos['tp'], 'tp1', frac=0.5)
                        if pos is not None:
                            pos['partial_done'], pos['tp'] = True, None
                            pos['sl'] = max(pos['sl'], pos['entry'])
                    else:
                        close_pos(t, pos['tp'], 'tp')
            else:
                if h[t] + sp >= pos['sl']:
                    close_pos(t, max(pos['sl'], o[t] + sp) + slip, 'sl')
                elif pos['tp'] is not None and l[t] + sp <= pos['tp']:
                    if config.EXIT_MODE == 'partial' and not pos['partial_done']:
                        close_pos(t, pos['tp'], 'tp1', frac=0.5)
                        if pos is not None:
                            pos['partial_done'], pos['tp'] = True, None
                            pos['sl'] = min(pos['sl'], pos['entry'])
                    else:
                        close_pos(t, pos['tp'], 'tp')

        # ---------------- 3. at bar CLOSE ----------------
        # advance HTF state to the last bar closed by now
        moved = False
        while hp + 1 < len(htf_close_t) and htf_close_t[hp + 1] <= decision_np[t]:
            hp += 1
            moved = True
        if moved:
            while poi_ptr < len(pois) and pois[poi_ptr]['created'] <= hp:
                active.append(pois[poi_ptr])
                poi_ptr += 1
            active = [p for p in active if p['dead'] > hp]
        b = int(bias[hp]) if hp >= 0 else 0

        if pos is not None:
            d = pos['dir']
            # swap at broker rollover (server midnight), x3 on Wednesday night
            if t + 1 < len(server_day_np) and server_day_np[t + 1] != server_day_np[t]:
                if spec.get('swap_daily'):   # crypto: charged every night, no triple Wednesday
                    nights = 1
                else:
                    nights = 3 if pd.Timestamp(server_day_np[t]).weekday() == 2 else 1
                sw = (spec['swap_long'] if d > 0 else spec['swap_short']) * pos['remaining'] * nights
                if spec.get('swap_pct_long') is not None:   # financing as % of position value
                    pct = spec['swap_pct_long'] if d > 0 else spec['swap_pct_short']
                    sw += pct * c[t] * cs * pos['remaining'] * nights
                pos['swap'] += sw
                equity += sw
            r = abs(pos['entry'] - pos['sl0'])
            pos['extreme'] = max(pos['extreme'], h[t]) if d > 0 else min(pos['extreme'], l[t] + sp)
            fav = (pos['extreme'] - pos['entry']) * d
            if config.BE_AT_R > 0 and not pos['be_done'] and fav >= config.BE_AT_R * r:
                pos['sl'] = max(pos['sl'], pos['entry']) if d > 0 else min(pos['sl'], pos['entry'])
                pos['be_done'] = True
            trailing = config.EXIT_MODE == 'trail' or (config.EXIT_MODE == 'partial' and pos['partial_done'])
            if trailing and fav > 0 and hp >= 0 and not np.isnan(htf_atr[hp]):
                dist = config.ATR_TRAIL_MULTIPLIER * htf_atr[hp]
                if d > 0:
                    pos['sl'] = max(pos['sl'], pos['extreme'] - dist)
                else:
                    pos['sl'] = min(pos['sl'], pos['extreme'] + dist)
            if config.EXIT_ON_BIAS_FLIP and b == -d:
                exit_next_open = True

        # daily loss limit (UTC day)
        if day != utc_day_np[t]:
            day, day_start_eq = utc_day_np[t], equity
        daily_ok = (equity - day_start_eq) / day_start_eq * 100 > -config.MAX_DAILY_DRAWDOWN_PERCENT

        if pos is None and pending is None and t + 1 < i1 and daily_ok \
                and spread[t] <= max_spread and in_kill_zone(decision_t.iloc[t]):
            b_entry = b
            if config.TREND_FILTER:
                ti = np.searchsorted(trend_close_t, decision_np[t], side='right') - 1
                if ti < 0 or trend_dir[ti] != b:
                    b_entry = 0
            sig, sl, poi = strat.ltf_signal(o, h, l, c, atr, t, b_entry, active, sp, used)
            if sig:
                pending = (1 if sig == 'BUY' else -1, sl, poi)
                if config.MAX_TRADES_PER_POI <= 1:
                    used.add(poi['id'])

    if pos is not None:
        close_pos(i1 - 1, c[i1 - 1] if pos['dir'] > 0 else c[i1 - 1] + spread[i1 - 1], 'end')

    return pd.DataFrame(trades), starting_equity, strat.filter_stats


def stats(trades, starting_equity):
    if trades is None or len(trades) == 0:
        return {'trades': 0}
    eq = np.concatenate([[starting_equity], trades['equity'].to_numpy()])
    peak = np.maximum.accumulate(eq)
    dd = ((peak - eq) / peak).max() * 100
    wins = trades[trades['pnl'] > 0]
    losses = trades[trades['pnl'] <= 0]
    months = max((trades['exit_time'].iloc[-1] - trades['entry_time'].iloc[0]).days / 30.4, 1)
    streak = cur = 0
    for p in trades['pnl']:
        cur = cur + 1 if p <= 0 else 0
        streak = max(streak, cur)
    return {
        'trades': len(trades),
        'per_month': round(len(trades) / months, 1),
        'win_rate': round(len(wins) / len(trades) * 100, 1),
        'avg_R': round(trades['R'].mean(), 2),
        'profit_factor': round(wins['pnl'].sum() / -losses['pnl'].sum(), 2) if losses['pnl'].sum() < 0 else float('inf'),
        'return_pct': round((eq[-1] / starting_equity - 1) * 100, 1),
        'max_dd_pct': round(dd, 1),
        'max_losing_streak': streak,
    }


def report(trades, starting_equity, filter_stats, title):
    print("=" * 60)
    print(title)
    print("=" * 60)
    s = stats(trades, starting_equity)
    for k, v in s.items():
        print(f"  {k:18s} {v}")
    if len(trades):
        print("\n  Exit reasons:", trades['reason'].value_counts().to_dict())
        by_m = trades.set_index('exit_time')['pnl'].resample('ME').agg(['count', 'sum'])
        print("\n  Monthly P/L ($):")
        for idx, row in by_m.iterrows():
            print(f"    {idx:%Y-%m}  trades {int(row['count']):3d}  P/L {row['sum']:+10.2f}")
    print("\n  Filter diagnostics:", filter_stats)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument('--symbol', default=config.SYMBOL)
    ap.add_argument('--htf', default=config.HTF)
    ap.add_argument('--ltf', default=config.LTF)
    ap.add_argument('--cache', default=None, help='folder with cached XAUUSD_<TF>.pkl files')
    ap.add_argument('--start', default=None)
    ap.add_argument('--end', default=None)
    ap.add_argument('--equity', type=float, default=10_000.0)
    ap.add_argument('--set', action='append', default=[], help='KEY=VALUE config override')
    ap.add_argument('--trades', action='store_true', help='print every trade')
    a = ap.parse_args()

    ov = {}
    for kv in a.set:
        k, v = kv.split('=', 1)
        try:
            v = eval(v, {}, {})
        except Exception:
            pass
        ov[k] = v

    config.SYMBOL = a.symbol.upper()
    htf_df = load_data(a.htf, a.cache)
    ltf_df = load_data(a.ltf, a.cache)
    trend_df = load_data(ov.get('TREND_TF', config.TREND_TF), a.cache) \
        if ov.get('TREND_FILTER', config.TREND_FILTER) else None
    tr, eq0, fs = run_backtest(htf_df, ltf_df, a.htf, a.ltf, a.start, a.end, a.equity,
                               trend_df=trend_df, **ov)
    report(tr, eq0, fs, f"SMC backtest {config.SYMBOL} {a.htf} -> {a.ltf} | risk {config.RISK_PERCENT}% | "
                        f"{ltf_df['time'].iloc[0]:%Y-%m-%d} .. {ltf_df['time'].iloc[-1]:%Y-%m-%d} {ov or ''}")
    if a.trades and len(tr):
        pd.set_option('display.width', 200)
        print(tr[['dir', 'entry_time', 'exit_time', 'entry', 'exit', 'lots', 'pnl', 'R', 'reason', 'poi']].to_string())
