import time
from datetime import datetime

import MetaTrader5 as mt5
import pytz

import config
from logger import logger
from data_feed import DataFeed
from strategy_smc import SMCStrategy
from execution import ExecutionEngine

HTF_BARS = 2000   # long history so the structure pass matches the backtest closely
LTF_BARS = 200
TREND_BARS = 600


class GoldBot:
    def __init__(self):
        logger.info("Initializing Gold Bot...")
        if config.EXIT_MODE == 'partial':
            raise ValueError("EXIT_MODE='partial' is backtest-only for now; use 'rr' or 'trail'.")
        self.data_feed = DataFeed()
        self.strategy = SMCStrategy(swing_length=config.SWING_LOOKBACK)
        self.executor = ExecutionEngine()
        self.current_trading_day = None
        self.starting_balance = None
        self.used_pois = set()         # each POI is traded once (MAX_TRADES_PER_POI)
        self.last_ltf_bar = None       # only evaluate entries once per closed LTF bar

    # ------------------------------------------------------------------ #
    def is_kill_zone(self):
        """Checks if current UTC time is within any kill zone."""
        now_utc = datetime.now(pytz.UTC).time()
        in_london = config.LONDON_OPEN_START <= now_utc <= config.LONDON_OPEN_END
        in_ny = config.NY_OPEN_START <= now_utc <= config.NY_OPEN_END
        return in_london or in_ny

    def check_daily_drawdown(self):
        """True while today's loss is inside MAX_DAILY_DRAWDOWN_PERCENT."""
        today = datetime.now(pytz.UTC).date()
        account = mt5.account_info()
        if account is None:
            return False
        if self.current_trading_day != today:
            self.current_trading_day = today
            self.starting_balance = account.balance
            logger.info(f"New trading day: {today}. Starting balance: {self.starting_balance}")
        if self.starting_balance:
            pnl_pct = (account.equity - self.starting_balance) / self.starting_balance * 100
            if pnl_pct <= -config.MAX_DAILY_DRAWDOWN_PERCENT:
                logger.warning(f"Daily drawdown limit hit ({pnl_pct:.2f}%). No new trades today.")
                return False
        return True

    def ensure_connection(self):
        """Re-initialise MT5 if the terminal connection dropped."""
        if mt5.terminal_info() is not None and mt5.account_info() is not None:
            return True
        logger.warning("MT5 connection lost - reconnecting...")
        mt5.shutdown()
        if mt5.initialize() and mt5.symbol_select(config.SYMBOL, True):
            logger.info("Reconnected to MT5.")
            return True
        logger.error(f"Reconnect failed: {mt5.last_error()}")
        return False

    # ------------------------------------------------------------------ #
    def tick(self):
        """One loop iteration. Uses CLOSED bars only (the last bar from MT5 is still forming)."""
        htf_df = self.data_feed.get_data(config.HTF, num_bars=HTF_BARS)
        ltf_df = self.data_feed.get_data(config.LTF, num_bars=LTF_BARS)
        if htf_df.empty or ltf_df.empty:
            logger.warning("Failed to fetch data.")
            self.ensure_connection()
            return
        htf_df, ltf_df = htf_df.iloc[:-1], ltf_df.iloc[:-1]

        htf_bias, pois = self.strategy.get_htf_bias_and_pois(htf_df)

        # --- manage open positions (every loop) ---
        if config.EXIT_ON_BIAS_FLIP:
            self.executor.manage_positions(htf_bias)
        if config.EXIT_MODE == 'rr':
            self.executor.manage_breakeven(config.BE_AT_R, config.TP_R)
        elif config.EXIT_MODE == 'trail':
            htf_atr = self.strategy.calculate_atr(htf_df)
            if not htf_atr.empty:
                self.executor.manage_trailing_stops(htf_atr.iloc[-1])

        # --- new entries: once per closed LTF bar, in kill zones, flat, within daily limit ---
        bar_time = ltf_df['time'].iloc[-1]
        if bar_time == self.last_ltf_bar:
            return
        self.last_ltf_bar = bar_time

        if not self.is_kill_zone() or self.executor.get_open_positions() or not self.check_daily_drawdown():
            return

        if config.TREND_FILTER:
            trend_df = self.data_feed.get_data(config.TREND_TF, num_bars=TREND_BARS)
            if trend_df.empty:
                return
            trend = int(self.strategy.trend_direction(trend_df.iloc[:-1])[-1])
            if {'BULLISH': 1, 'BEARISH': -1}.get(htf_bias, 0) != trend:
                return

        tick = self.data_feed.get_tick()
        info = self.data_feed.get_symbol_info()
        if tick is None or info is None:
            return
        spread_pts = (tick.ask - tick.bid) / info.point
        if spread_pts > config.MAX_SPREAD_POINTS:
            logger.warning(f"Spread too high ({spread_pts:.0f} points). Skipping entry check.")
            return

        signal, sl_price, poi = self.strategy.check_ltf_entry(
            ltf_df, htf_bias, pois, spread_price=tick.ask - tick.bid, used_pois=self.used_pois)
        if signal:
            tp_r = config.TP_R if config.EXIT_MODE == 'rr' else None
            if self.executor.open_position(signal, sl_price, tp_r=tp_r):
                if config.MAX_TRADES_PER_POI <= 1:
                    self.used_pois.add(poi['id'])

    def run(self):
        logger.info("Bot started. Entering main loop.")
        logger.info(f"{config.SYMBOL} | HTF {config.HTF} -> LTF {config.LTF} | trend filter "
                    f"{'ON (' + config.TREND_TF + ')' if config.TREND_FILTER else 'OFF'} | exit {config.EXIT_MODE}")
        logger.info(f"Kill Zones (UTC): London {config.LONDON_OPEN_START}-{config.LONDON_OPEN_END} | "
                    f"NY {config.NY_OPEN_START}-{config.NY_OPEN_END}")
        logger.info(f"Risk: {config.RISK_PERCENT}% | Daily DD Limit: {config.MAX_DAILY_DRAWDOWN_PERCENT}%")
        try:
            while True:
                try:
                    self.tick()
                except Exception as e:  # never let one bad iteration kill the bot
                    logger.error(f"Error in main loop: {e}", exc_info=True)
                    self.ensure_connection()
                time.sleep(20)
        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
        finally:
            self.data_feed.shutdown()


if __name__ == "__main__":
    GoldBot().run()
