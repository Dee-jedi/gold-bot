import time
from datetime import datetime
import pytz

import config
from logger import logger
from data_feed import DataFeed
from strategy_smc import SMCStrategy
from execution import ExecutionEngine

class GoldBot:
    def __init__(self):
        logger.info("Initializing Gold Bot...")
        self.data_feed = DataFeed()
        self.strategy = SMCStrategy(swing_length=config.SWING_LOOKBACK)
        self.executor = ExecutionEngine()
        self.daily_pnl = 0.0
        self.current_trading_day = None
        self.starting_balance = None

    def is_kill_zone(self):
        """Checks if current UTC time is within any kill zone."""
        now_utc = datetime.now(pytz.UTC).time()
        in_london = config.LONDON_OPEN_START <= now_utc <= config.LONDON_OPEN_END
        in_ny = config.NY_OPEN_START <= now_utc <= config.NY_OPEN_END
        return in_london or in_ny

    def check_daily_drawdown(self):
        """
        Checks if the daily loss limit has been exceeded.
        Returns True if we are still within the limit and can trade.
        """
        import MetaTrader5 as mt5

        today = datetime.now(pytz.UTC).date()
        if self.current_trading_day != today:
            self.current_trading_day = today
            account = mt5.account_info()
            if account:
                self.starting_balance = account.balance
            self.daily_pnl = 0.0
            logger.info(f"New trading day: {today}. Starting balance: {self.starting_balance}")

        if self.starting_balance and self.starting_balance > 0:
            account = mt5.account_info()
            if account:
                current_pnl_pct = ((account.equity - self.starting_balance) / self.starting_balance) * 100
                if current_pnl_pct <= -config.MAX_DAILY_DRAWDOWN_PERCENT:
                    logger.warning(f"Daily drawdown limit hit! Current: {current_pnl_pct:.2f}% "
                                   f"(limit: -{config.MAX_DAILY_DRAWDOWN_PERCENT}%). No new trades.")
                    return False

        return True

    def run(self):
        logger.info("Bot started. Entering main loop.")
        logger.info(f"Kill Zones: London {config.LONDON_OPEN_START}-{config.LONDON_OPEN_END} | "
                     f"NY {config.NY_OPEN_START}-{config.NY_OPEN_END}")
        logger.info(f"Risk: {config.RISK_PERCENT}% | Daily DD Limit: {config.MAX_DAILY_DRAWDOWN_PERCENT}%")

        try:
            while True:
                # 1. Check Kill Zones
                if not self.is_kill_zone():
                    # Still manage open positions outside kill zones
                    self._manage_existing_positions()
                    time.sleep(60)  # Wait a minute before checking again
                    continue

                # 2. Fetch Data
                htf_df = self.data_feed.get_data(config.HTF, num_bars=500)
                ltf_df = self.data_feed.get_data(config.LTF, num_bars=200)

                if htf_df.empty or ltf_df.empty:
                    logger.warning("Failed to fetch data, retrying in 10s...")
                    time.sleep(10)
                    continue

                # 3. Strategy Analysis
                htf_bias, pois = self.strategy.get_htf_bias_and_pois(htf_df)

                # 4. Manage Open Positions
                self.executor.manage_positions(htf_bias)
                
                # Update Trailing Stops
                htf_atr = self.strategy.calculate_atr(htf_df)
                if not htf_atr.empty:
                    htf_atr_val = htf_atr.iloc[-1]
                    self.executor.manage_trailing_stops(htf_atr_val)

                # 5. Check for New Entries (only if no open position AND daily limit not hit)
                open_positions = self.executor.get_open_positions()
                if len(open_positions) == 0 and self.check_daily_drawdown():
                    signal, sl_price = self.strategy.check_ltf_entry(ltf_df, htf_bias, pois)
                    if signal:
                        # Ensure spread is acceptable before executing
                        tick = self.data_feed.get_tick()
                        spread = (tick.ask - tick.bid) / self.data_feed.get_symbol_info().point

                        if spread > config.MAX_SPREAD_POINTS:
                            logger.warning(f"Spread too high ({spread} points). Ignoring entry signal.")
                        else:
                            self.executor.open_position(signal, sl_price)

                # Sleep to prevent spamming the CPU and MT5 terminal
                # Since our lowest timeframe is 1M, sleeping 30 seconds is safe.
                time.sleep(30)

        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
        except Exception as e:
            logger.error(f"Critical error in main loop: {str(e)}", exc_info=True)
        finally:
            self.data_feed.shutdown()

    def _manage_existing_positions(self):
        """Manage open positions even outside kill zones."""
        try:
            htf_df = self.data_feed.get_data(config.HTF, num_bars=500)
            if not htf_df.empty:
                htf_bias, _ = self.strategy.get_htf_bias_and_pois(htf_df)
                self.executor.manage_positions(htf_bias)
        except Exception as e:
            logger.error(f"Error managing positions: {str(e)}")

if __name__ == "__main__":
    bot = GoldBot()
    bot.run()
