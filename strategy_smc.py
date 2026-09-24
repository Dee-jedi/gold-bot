import pandas as pd
import numpy as np
from logger import logger
import config

class SMCStrategy:
    def __init__(self, swing_length=5):
        self.swing_length = swing_length

    # ------------------------------------------------------------------ #
    #  Core Helpers
    # ------------------------------------------------------------------ #

    def find_swings(self, df):
        """Finds swing highs and swing lows using vectorized operations for speed."""
        window = self.swing_length * 2 + 1

        rolling_max = df['high'].rolling(window=window, center=True).max()
        rolling_min = df['low'].rolling(window=window, center=True).min()

        df['is_swing_high'] = (df['high'] == rolling_max) & df['high'].notna()
        df['is_swing_low'] = (df['low'] == rolling_min) & df['low'].notna()

        return df

    def _find_micro_swings(self, df, lookback=2):
        """Finds micro swing highs/lows using a smaller lookback for CHoCH detection."""
        window = lookback * 2 + 1

        rolling_max = df['high'].rolling(window=window, center=True).max()
        rolling_min = df['low'].rolling(window=window, center=True).min()

        df['is_swing_high'] = (df['high'] == rolling_max) & df['high'].notna()
        df['is_swing_low'] = (df['low'] == rolling_min) & df['low'].notna()

        return df

    def calculate_atr(self, df, period=None):
        """Calculates Average True Range for volatility-based stops."""
        if period is None:
            period = config.ATR_PERIOD

        high = df['high']
        low = df['low']
        close = df['close'].shift(1)

        tr1 = high - low
        tr2 = (high - close).abs()
        tr3 = (low - close).abs()

        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = true_range.rolling(window=period).mean()
        return atr

    # ------------------------------------------------------------------ #
    #  New Filters
    # ------------------------------------------------------------------ #

    def check_premium_discount(self, current_price, recent_swing_high, recent_swing_low, direction):
        """
        Checks if price is in the correct zone for the trade direction.
        BUY only in Discount (below 50%), SELL only in Premium (above 50%).
        Returns True if the zone is correct for the direction.
        """
        if not config.USE_PREMIUM_DISCOUNT:
            return True

        dealing_range = recent_swing_high - recent_swing_low
        if dealing_range <= 0:
            return True  # Can't calculate, allow the trade

        equilibrium = recent_swing_low + (dealing_range * 0.50)

        if direction == 'BUY' and current_price < equilibrium:
            return True  # Price is in Discount — good for buys
        elif direction == 'SELL' and current_price > equilibrium:
            return True  # Price is in Premium — good for sells

        return False

    def detect_liquidity_sweep(self, ltf_df, direction, lookback=20):
        """
        Detects if a recent liquidity sweep occurred.
        For BUYS: price wicked below a prior swing low then closed back above it.
        For SELLS: price wicked above a prior swing high then closed back below it.
        """
        if len(ltf_df) < lookback + self.swing_length * 2:
            return False

        # Find swings in the lookback window (use a slightly older portion)
        ref_df = ltf_df.iloc[-(lookback + 20):-lookback].copy()
        ref_df = self.find_swings(ref_df)
        
        # Recent candles to check for the sweep
        recent = ltf_df.iloc[-lookback:]

        if direction == 'BUY':
            ref_lows = ref_df[ref_df['is_swing_low']]
            if len(ref_lows) == 0:
                return False
            key_low = ref_lows['low'].min()
            # Check if any recent candle wicked below and closed above
            swept = ((recent['low'] < key_low) & (recent['close'] > key_low)).any()
            return swept

        elif direction == 'SELL':
            ref_highs = ref_df[ref_df['is_swing_high']]
            if len(ref_highs) == 0:
                return False
            key_high = ref_highs['high'].max()
            # Check if any recent candle wicked above and closed below
            swept = ((recent['high'] > key_high) & (recent['close'] < key_high)).any()
            return swept

        return False

    def detect_fvg(self, ltf_df, direction, min_size=None):
        """
        Detects a recent Fair Value Gap (FVG) as displacement confirmation.
        FVG = gap between candle[i-2].high/low and candle[i].low/high (3-candle pattern).
        Returns True if a valid FVG exists in the last few candles.
        """
        if min_size is None:
            min_size = config.MIN_FVG_SIZE

        if len(ltf_df) < 5:
            return False

        # Check last 5 candles for an FVG
        for i in range(-1, -4, -1):
            idx = len(ltf_df) + i
            if idx < 2:
                continue

            if direction == 'BUY':
                # Bullish FVG: candle[idx] low > candle[idx-2] high
                gap = ltf_df['low'].iloc[idx] - ltf_df['high'].iloc[idx - 2]
                if gap > 0:
                    return True

            elif direction == 'SELL':
                # Bearish FVG: candle[idx] high < candle[idx-2] low
                gap = ltf_df['low'].iloc[idx - 2] - ltf_df['high'].iloc[idx]
                if gap > 0:
                    return True

        return False

    def is_ob_unmitigated(self, poi, htf_df):
        """
        Checks if an Order Block has NOT been fully mitigated since its creation.
        An OB is mitigated only if price CLOSED through it entirely:
        - Bullish OB: mitigated if a candle closed below the OB bottom
        - Bearish OB: mitigated if a candle closed above the OB top
        """
        ob_time = poi['time']

        # Get all candles after the OB was formed (exclude the most recent one)
        subsequent = htf_df[htf_df['time'] > ob_time]
        if len(subsequent) <= 1:
            return True  # Not enough data to determine — treat as unmitigated

        # Exclude the current (last) candle since price might be testing it right now
        check_candles = subsequent.iloc[:-1]

        if poi['type'] == 'BULLISH_OB':
            # Only mitigated if price closed below the OB bottom (demand zone broken)
            mitigated = (check_candles['close'] < poi['bottom']).any()
        elif poi['type'] == 'BEARISH_OB':
            # Only mitigated if price closed above the OB top (supply zone broken)
            mitigated = (check_candles['close'] > poi['top']).any()
        else:
            return True

        return not mitigated

    # ------------------------------------------------------------------ #
    #  HTF Analysis (Bias + POI)
    # ------------------------------------------------------------------ #

    def get_htf_bias_and_pois(self, htf_df):
        """
        Analyzes HTF data to find directional bias and Point of Interest (POI).
        Returns bias ('BULLISH', 'BEARISH', 'NEUTRAL') and a list of POIs (Order Blocks).
        Now filters out mitigated OBs.
        """
        htf_df = self.find_swings(htf_df)

        swing_highs = htf_df[htf_df['is_swing_high']]
        swing_lows = htf_df[htf_df['is_swing_low']]

        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return "NEUTRAL", []

        last_high = swing_highs.iloc[-1]
        prev_high = swing_highs.iloc[-2]
        last_low = swing_lows.iloc[-1]
        prev_low = swing_lows.iloc[-2]

        bias = "NEUTRAL"
        pois = []

        # Simple structural analysis (HH/HL -> Bullish, LL/LH -> Bearish)
        if last_high['high'] > prev_high['high'] and last_low['low'] > prev_low['low']:
            bias = "BULLISH"
            # Find Bullish Order Block (last down candle before the up move that created HH)
            ob_start_idx = htf_df.index.get_loc(last_low.name)
            for i in range(ob_start_idx, max(0, ob_start_idx - 10), -1):
                if htf_df['close'].iloc[i] < htf_df['open'].iloc[i]:  # Down candle
                    candidate_poi = {
                        'type': 'BULLISH_OB',
                        'top': htf_df['high'].iloc[i],
                        'bottom': htf_df['low'].iloc[i],
                        'time': htf_df['time'].iloc[i]
                    }
                    # Only add if unmitigated
                    if self.is_ob_unmitigated(candidate_poi, htf_df):
                        pois.append(candidate_poi)
                    break

        elif last_high['high'] < prev_high['high'] and last_low['low'] < prev_low['low']:
            bias = "BEARISH"
            # Find Bearish Order Block (last up candle before the down move that created LL)
            ob_start_idx = htf_df.index.get_loc(last_high.name)
            for i in range(ob_start_idx, max(0, ob_start_idx - 10), -1):
                if htf_df['close'].iloc[i] > htf_df['open'].iloc[i]:  # Up candle
                    candidate_poi = {
                        'type': 'BEARISH_OB',
                        'top': htf_df['high'].iloc[i],
                        'bottom': htf_df['low'].iloc[i],
                        'time': htf_df['time'].iloc[i]
                    }
                    # Only add if unmitigated
                    if self.is_ob_unmitigated(candidate_poi, htf_df):
                        pois.append(candidate_poi)
                    break

        return bias, pois

    # Diagnostic counters (class-level)
    filter_stats = {
        'no_pois': 0,
        'not_in_poi': 0,
        'wrong_bias_poi': 0,
        'no_swings': 0,
        'premium_discount_reject': 0,
        'liquidity_sweep_reject': 0,
        'choch_reject': 0,
        'fvg_reject': 0,
        'entries': 0,
    }

    def check_ltf_entry(self, ltf_df, htf_bias, pois):
        """
        Analyzes LTF data for entry signals with full confirmation:
        1. Price is inside an unmitigated OB (POI)
        2. Premium/Discount zone filter
        3. Liquidity sweep detected
        4. CHoCH confirmed
        5. FVG displacement confirmed
        Returns (signal, sl_price) or (None, None).
        """
        if not pois:
            self.filter_stats['no_pois'] += 1
            return None, None

        current_price = ltf_df['close'].iloc[-1]
        in_poi = False
        active_poi = None

        # 1. Are we in a POI?
        for poi in pois:
            if poi['bottom'] <= current_price <= poi['top']:
                in_poi = True
                active_poi = poi
                break

        if not in_poi:
            self.filter_stats['not_in_poi'] += 1
            return None, None

        # Determine direction
        if htf_bias == "BULLISH" and active_poi['type'] == 'BULLISH_OB':
            direction = 'BUY'
        elif htf_bias == "BEARISH" and active_poi['type'] == 'BEARISH_OB':
            direction = 'SELL'
        else:
            self.filter_stats['wrong_bias_poi'] += 1
            return None, None

        # 2. Premium/Discount filter
        ltf_df_swings = self.find_swings(ltf_df)
        ltf_highs = ltf_df_swings[ltf_df_swings['is_swing_high']]
        ltf_lows = ltf_df_swings[ltf_df_swings['is_swing_low']]

        if len(ltf_highs) < 1 or len(ltf_lows) < 1:
            self.filter_stats['no_swings'] += 1
            return None, None

        recent_swing_high = ltf_highs['high'].iloc[-1]
        recent_swing_low = ltf_lows['low'].iloc[-1]

        if not self.check_premium_discount(current_price, recent_swing_high, recent_swing_low, direction):
            self.filter_stats['premium_discount_reject'] += 1
            return None, None

        # 3. Liquidity sweep check (configurable)
        if config.REQUIRE_LIQUIDITY_SWEEP:
            if not self.detect_liquidity_sweep(ltf_df, direction):
                self.filter_stats['liquidity_sweep_reject'] += 1
                return None, None

        # 4. CHoCH confirmation (micro-structure: last 15 candles, swing_length=2)
        #    We look for a break of a very recent, local swing — not the full-window swing.
        micro_window = ltf_df.iloc[-15:].copy()
        micro_window_swings = self._find_micro_swings(micro_window, lookback=2)
        micro_highs = micro_window_swings[micro_window_swings['is_swing_high']]
        micro_lows = micro_window_swings[micro_window_swings['is_swing_low']]

        if direction == 'BUY':
            if len(micro_highs) == 0 or current_price <= micro_highs['high'].iloc[-1]:
                self.filter_stats['choch_reject'] += 1
                return None, None
        elif direction == 'SELL':
            if len(micro_lows) == 0 or current_price >= micro_lows['low'].iloc[-1]:
                self.filter_stats['choch_reject'] += 1
                return None, None

        # 5. FVG displacement confirmation (configurable)
        if config.REQUIRE_FVG:
            if not self.detect_fvg(ltf_df, direction):
                self.filter_stats['fvg_reject'] += 1
                return None, None

        # --- All filters passed! Calculate ATR-buffered stop loss ---
        self.filter_stats['entries'] += 1
        atr_series = self.calculate_atr(ltf_df)
        current_atr = atr_series.iloc[-1] if not atr_series.empty and not pd.isna(atr_series.iloc[-1]) else 0

        if direction == 'BUY':
            sl_price = active_poi['bottom'] - (current_atr * config.ATR_SL_MULTIPLIER)
            logger.info(f"Bullish entry confirmed! POI + Discount + Sweep + CHoCH + FVG. ATR-buffered SL: {sl_price:.2f}")
            return 'BUY', sl_price

        elif direction == 'SELL':
            sl_price = active_poi['top'] + (current_atr * config.ATR_SL_MULTIPLIER)
            logger.info(f"Bearish entry confirmed! POI + Premium + Sweep + CHoCH + FVG. ATR-buffered SL: {sl_price:.2f}")
            return 'SELL', sl_price

        return None, None

    def print_filter_stats(self):
        """Prints diagnostic info about which filters rejected the most signals."""
        print("\n--- Filter Diagnostics ---")
        for k, v in self.filter_stats.items():
            print(f"  {k}: {v}")

