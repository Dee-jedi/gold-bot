"""
SMC strategy: HTF market structure + order-block POIs, LTF sweep + CHoCH entry.

Everything here is CAUSAL: a value at bar t only uses bars <= t, and a swing
point is only "known" once `swing_length` bars have closed after it. The live
bot and the backtest call exactly the same functions, so what you backtest is
what trades.

HTF (default 15M)
  - Trend is tracked with BOS / CHoCH confirmed by a candle BODY CLOSE beyond
    the last confirmed swing high / low (wicks don't count).
  - Every bullish break creates a demand POI (the last bearish candle at the
    origin of the leg that broke structure); every bearish break a supply POI.
  - A POI stays alive until an HTF candle closes through it, it gets too old,
    or the trend flips against it.

LTF (default 1M)
  - Price trades into an aligned POI and sweeps a prior LTF swing low/high,
    then a candle closes beyond the last LTF swing high/low (CHoCH).
  - SL goes beyond the sweep extreme (or the POI) plus a buffer.
"""
import numpy as np
import pandas as pd
import config

try:
    from logger import logger
except Exception:  # logger is optional for research scripts
    import logging
    logger = logging.getLogger(__name__)


def _cfg(name, default):
    return getattr(config, name, default)


class SMCStrategy:
    def __init__(self, swing_length=None):
        self.swing_length = swing_length or _cfg('SWING_LOOKBACK', 5)
        self.filter_stats = {k: 0 for k in (
            'no_bias', 'no_poi', 'not_in_poi', 'no_sweep', 'no_choch',
            'no_displacement', 'sl_too_small', 'entries')}

    # ------------------------------------------------------------------ #
    #  Indicators
    # ------------------------------------------------------------------ #
    @staticmethod
    def calculate_atr(df, period=None):
        """Average True Range (simple moving average of true range)."""
        period = period or _cfg('ATR_PERIOD', 14)
        prev_close = df['close'].shift(1)
        tr = pd.concat([df['high'] - df['low'],
                        (df['high'] - prev_close).abs(),
                        (df['low'] - prev_close).abs()], axis=1).max(axis=1)
        return tr.rolling(window=period).mean()

    @staticmethod
    def swing_flags(high, low, length):
        """Fractal swing highs/lows (`length` bars each side).
        Flag at j is only usable from bar j + length onwards."""
        n = len(high)
        sh = np.zeros(n, dtype=bool)
        sl = np.zeros(n, dtype=bool)
        if n < 2 * length + 1:
            return sh, sl
        w = 2 * length + 1
        hw = np.lib.stride_tricks.sliding_window_view(high, w)
        lw = np.lib.stride_tricks.sliding_window_view(low, w)
        sh[length:n - length] = high[length:n - length] >= hw.max(axis=1)
        sl[length:n - length] = low[length:n - length] <= lw.min(axis=1)
        return sh, sl

    @staticmethod
    def trend_direction(df, ema_period=None, slope_bars=None):
        """Big-picture trend filter (from the H4 rules): +1 if close > EMA and the
        EMA is rising over `slope_bars`, -1 for the mirror case, 0 otherwise.
        Value at bar i only uses bars <= i."""
        ema_period = ema_period or _cfg('TREND_EMA', 200)
        slope_bars = slope_bars or _cfg('TREND_SLOPE_BARS', 10)
        ema = df['close'].ewm(span=ema_period, adjust=False).mean()
        slope = ema - ema.shift(slope_bars)
        up = (df['close'] > ema) & (slope > 0)
        dn = (df['close'] < ema) & (slope < 0)
        out = np.where(up, 1, np.where(dn, -1, 0))
        out[:ema_period] = 0   # EMA not settled yet
        return out

    # ------------------------------------------------------------------ #
    #  HTF: structure pass
    # ------------------------------------------------------------------ #
    def htf_structure(self, htf_df):
        """
        Single causal pass over closed HTF bars.

        Returns
          bias : np.array[int] per bar (+1 bullish, -1 bearish, 0 none), known at bar close
          pois : list of dicts {id, dir, top, bottom, created, dead, protected}
                 alive on bar i if created <= i < dead
        """
        o = htf_df['open'].to_numpy(float)
        h = htf_df['high'].to_numpy(float)
        l = htf_df['low'].to_numpy(float)
        c = htf_df['close'].to_numpy(float)
        times = htf_df['time'].to_numpy()
        n = len(c)
        L = self.swing_length
        is_sh, is_sl = self.swing_flags(h, l, L)

        max_age = _cfg('POI_MAX_AGE_BARS', 192)
        ob_search = _cfg('OB_SEARCH_BARS', 5)
        min_bos = _cfg('MIN_BOS_FOR_BIAS', 1)
        use_fvg_poi = _cfg('USE_FVG_POI', False)

        bias = np.zeros(n, dtype=int)
        pois, active = [], []
        trend, bos_count = 0, 0
        last_sh = last_sl = None          # (price, idx)
        sh_broken = sl_broken = True

        def add_poi(direction, top, bottom, t, protected, kind):
            if top <= bottom:
                return
            p = {'id': f"{kind}{direction:+d}@{pd.Timestamp(times[t])}", 'dir': direction,
                 'kind': kind, 'top': float(top), 'bottom': float(bottom),
                 'created': t, 'dead': n, 'protected': float(protected)}
            pois.append(p)
            active.append(p)

        for t in range(n):
            # 1. newly confirmed swings (bar j = t-L now has L bars on its right)
            j = t - L
            if j >= L:
                if is_sh[j]:
                    last_sh, sh_broken = (h[j], j), False
                if is_sl[j]:
                    last_sl, sl_broken = (l[j], j), False

            # 2. kill POIs that got closed through / are too old
            for p in active[:]:
                broken = c[t] < p['bottom'] if p['dir'] > 0 else c[t] > p['top']
                if broken or t - p['created'] > max_age:
                    p['dead'] = t
                    active.remove(p)

            # 3. BOS / CHoCH by body close
            if last_sh is not None and not sh_broken and c[t] > last_sh[0]:
                sh_broken = True
                bos_count = bos_count + 1 if trend == 1 else 1
                if trend != 1:  # CHoCH: drop opposite POIs
                    for p in active[:]:
                        if p['dir'] < 0:
                            p['dead'] = t
                            active.remove(p)
                trend = 1
                k = last_sh[1] + int(np.argmin(l[last_sh[1]:t + 1]))   # leg origin (lowest low)
                ob = k
                for m in range(k, max(k - ob_search, -1), -1):
                    if c[m] < o[m]:
                        ob = m
                        break
                if not (c[ob + 1:t + 1] < l[ob]).any():
                    add_poi(+1, h[ob], l[ob], t, l[k], 'OB')
                if use_fvg_poi:
                    for m in range(k + 2, t + 1):
                        if l[m] > h[m - 2] and not (c[m + 1:t + 1] < h[m - 2]).any():
                            add_poi(+1, l[m], h[m - 2], t, l[k], 'FVG')

            elif last_sl is not None and not sl_broken and c[t] < last_sl[0]:
                sl_broken = True
                bos_count = bos_count + 1 if trend == -1 else 1
                if trend != -1:
                    for p in active[:]:
                        if p['dir'] > 0:
                            p['dead'] = t
                            active.remove(p)
                trend = -1
                k = last_sl[1] + int(np.argmax(h[last_sl[1]:t + 1]))
                ob = k
                for m in range(k, max(k - ob_search, -1), -1):
                    if c[m] > o[m]:
                        ob = m
                        break
                if not (c[ob + 1:t + 1] > h[ob]).any():
                    add_poi(-1, h[ob], l[ob], t, h[k], 'OB')
                if use_fvg_poi:
                    for m in range(k + 2, t + 1):
                        if h[m] < l[m - 2] and not (c[m + 1:t + 1] > l[m - 2]).any():
                            add_poi(-1, l[m - 2], h[m], t, h[k], 'FVG')

            bias[t] = trend if bos_count >= min_bos else 0

        return bias, pois

    def get_htf_bias_and_pois(self, htf_df):
        """Live helper: state after the last CLOSED HTF bar."""
        bias, pois = self.htf_structure(htf_df)
        last = len(htf_df) - 1
        alive = [p for p in pois if p['created'] <= last < p['dead']]
        name = {1: 'BULLISH', -1: 'BEARISH', 0: 'NEUTRAL'}[int(bias[last])] if last >= 0 else 'NEUTRAL'
        return name, alive

    # ------------------------------------------------------------------ #
    #  LTF: entry trigger
    # ------------------------------------------------------------------ #
    def ltf_signal(self, o, h, l, c, atr, t, bias, pois, spread_price=0.0, used_pois=()):
        """
        Checks for an entry at the CLOSE of LTF bar t.
        o/h/l/c/atr are numpy arrays (bid prices); bias is +1/-1/0; pois are alive POIs.
        Returns (direction 'BUY'/'SELL', sl_price, poi) or (None, None, None).
        """
        fs = self.filter_stats
        if bias == 0:
            fs['no_bias'] += 1
            return None, None, None
        cands = [p for p in pois if p['dir'] == bias and p['id'] not in used_pois]
        if not cands:
            fs['no_poi'] += 1
            return None, None, None

        Ls = _cfg('LTF_SWING_LENGTH', 2)
        N = _cfg('SWEEP_LOOKBACK', 20)
        ref = _cfg('SWEEP_REF_BARS', 30)
        start = max(0, t - N - ref - Ls * 2)
        if t - start < N + 2 * Ls + 2:
            return None, None, None

        hh, ll, cc, oo = h[start:t + 1], l[start:t + 1], c[start:t + 1], o[start:t + 1]
        T = len(cc) - 1
        is_sh, is_sl = self.swing_flags(hh, ll, Ls)
        is_sh[T - Ls + 1:] = False    # not confirmed yet at bar t
        is_sl[T - Ls + 1:] = False

        if bias > 0:
            m = T - N + 1 + int(np.argmin(ll[T - N + 1:]))       # sweep candle
            extreme = ll[m]
            poi = next((p for p in cands
                        if p['bottom'] - (p['top'] - p['bottom']) <= extreme <= p['top']), None)
        else:
            m = T - N + 1 + int(np.argmax(hh[T - N + 1:]))
            extreme = hh[m]
            poi = next((p for p in cands
                        if p['bottom'] <= extreme <= p['top'] + (p['top'] - p['bottom'])), None)
        if poi is None:
            fs['not_in_poi'] += 1
            return None, None, None

        # Liquidity sweep: the extreme took out an LTF swing formed before it
        if _cfg('REQUIRE_LIQUIDITY_SWEEP', True):
            lo_j = max(0, m - ref)
            if bias > 0:
                prior = ll[lo_j:m][is_sl[lo_j:m]]
                swept = len(prior) > 0 and extreme < prior.max() if _cfg('SWEEP_ANY', True) \
                    else len(prior) > 0 and extreme < prior[-1]
            else:
                prior = hh[lo_j:m][is_sh[lo_j:m]]
                swept = len(prior) > 0 and extreme > prior.min() if _cfg('SWEEP_ANY', True) \
                    else len(prior) > 0 and extreme > prior[-1]
            if not swept:
                fs['no_sweep'] += 1
                return None, None, None

        # CHoCH: THIS bar is the first close beyond the latest LTF swing in the window
        choch_from = max(0, m - _cfg('CHOCH_LOOKBACK', 15))
        if bias > 0:
            idx = np.flatnonzero(is_sh[choch_from:T]) + choch_from
            if len(idx) == 0:
                fs['no_choch'] += 1
                return None, None, None
            k = idx[-1]
            level = hh[k]
            ok = cc[T] > level and (cc[k + 1:T] <= level).all()
        else:
            idx = np.flatnonzero(is_sl[choch_from:T]) + choch_from
            if len(idx) == 0:
                fs['no_choch'] += 1
                return None, None, None
            k = idx[-1]
            level = ll[k]
            ok = cc[T] < level and (cc[k + 1:T] >= level).all()
        if not ok:
            fs['no_choch'] += 1
            return None, None, None

        cur_atr = float(atr[t]) if atr is not None and not np.isnan(atr[t]) else 0.0

        # Displacement: strong, body-dominant breaking candle
        if _cfg('REQUIRE_DISPLACEMENT', False):
            body = abs(cc[T] - oo[T])
            rng = hh[T] - ll[T]
            if rng <= 0 or body < 0.6 * rng or body < _cfg('DISPLACEMENT_ATR', 1.0) * cur_atr:
                fs['no_displacement'] += 1
                return None, None, None

        buffer = max(2 * spread_price, 0.1 * cur_atr)
        if _cfg('SL_MODE', 'sweep') == 'poi':
            sl = min(extreme, poi['bottom']) - buffer if bias > 0 else max(extreme, poi['top']) + buffer
        else:
            sl = extreme - buffer if bias > 0 else extreme + buffer

        dist = abs(cc[T] - sl)
        if dist < _cfg('MIN_SL_ATR', 0.5) * cur_atr or (spread_price > 0 and spread_price > 0.25 * dist):
            fs['sl_too_small'] += 1
            return None, None, None

        fs['entries'] += 1
        return ('BUY' if bias > 0 else 'SELL'), float(sl), poi

    def check_ltf_entry(self, ltf_df, htf_bias, pois, spread_price=0.0, used_pois=()):
        """Live helper: entry check on the last CLOSED LTF bar."""
        b = {'BULLISH': 1, 'BEARISH': -1}.get(htf_bias, 0)
        atr = self.calculate_atr(ltf_df).to_numpy(float)
        sig, sl, poi = self.ltf_signal(
            ltf_df['open'].to_numpy(float), ltf_df['high'].to_numpy(float),
            ltf_df['low'].to_numpy(float), ltf_df['close'].to_numpy(float),
            atr, len(ltf_df) - 1, b, pois, spread_price, used_pois)
        if sig:
            logger.info(f"{sig} signal: POI {poi['kind']} {poi['bottom']:.2f}-{poi['top']:.2f}, "
                        f"sweep + CHoCH confirmed, SL {sl:.2f}")
        return sig, sl, poi

    def print_filter_stats(self):
        print("\n--- Filter Diagnostics ---")
        for k, v in self.filter_stats.items():
            print(f"  {k}: {v}")
