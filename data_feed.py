import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime
from logger import logger
import config

class DataFeed:
    def __init__(self):
        # Initialize MT5 connection
        if not mt5.initialize():
            logger.error(f"MT5 initialize() failed, error code = {mt5.last_error()}")
            raise Exception("Failed to initialize MT5")
        logger.info("MT5 initialized successfully.")
        
        # Check if the symbol is available
        symbol_info = mt5.symbol_info(config.SYMBOL)
        if symbol_info is None:
            logger.error(f"Symbol {config.SYMBOL} not found.")
            mt5.shutdown()
            raise Exception(f"Symbol {config.SYMBOL} not found.")
        
        # Select the symbol in Market Watch if not already there
        if not symbol_info.visible:
            logger.info(f"{config.SYMBOL} is not visible, trying to select it")
            if not mt5.symbol_select(config.SYMBOL, True):
                logger.error(f"Failed to select {config.SYMBOL}")
                mt5.shutdown()
                raise Exception(f"Failed to select {config.SYMBOL}")

    def get_data(self, timeframe, num_bars=500):
        """
        Fetches historical data for the given timeframe.
        timeframe: string ('1H', '3M', '15M', '1M')
        num_bars: number of bars to fetch
        """
        mt5_tf = self._map_timeframe(timeframe)
        
        # Fetch rates
        rates = mt5.copy_rates_from_pos(config.SYMBOL, mt5_tf, 0, num_bars)
        if rates is None or len(rates) == 0:
            logger.error(f"Failed to fetch data for {config.SYMBOL} on {timeframe}")
            return pd.DataFrame()
            
        # Convert to pandas DataFrame
        df = pd.DataFrame(rates)
        # Convert time in seconds to datetime format
        df['time'] = pd.to_datetime(df['time'], unit='s')
        
        # Set time as index (optional, but standard for time series)
        # df.set_index('time', inplace=True)
        return df

    def get_data_range(self, timeframe, date_from, date_to):
        """
        Fetches historical data between two dates.
        """
        mt5_tf = self._map_timeframe(timeframe)
        rates = mt5.copy_rates_range(config.SYMBOL, mt5_tf, date_from, date_to)
        
        if rates is None or len(rates) == 0:
            logger.error(f"Failed to fetch data for {config.SYMBOL} from {date_from} to {date_to}")
            return pd.DataFrame()
            
        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        return df

    def get_tick(self):
        """Fetches the latest tick data (Bid/Ask)."""
        tick = mt5.symbol_info_tick(config.SYMBOL)
        if tick is None:
            logger.error(f"Failed to fetch tick for {config.SYMBOL}")
            return None
        return tick
        
    def get_symbol_info(self):
        """Fetches symbol properties (point size, digit count, etc.)."""
        return mt5.symbol_info(config.SYMBOL)

    def shutdown(self):
        mt5.shutdown()
        logger.info("MT5 connection closed.")

    def _map_timeframe(self, tf):
        """Maps our config timeframes to MT5 timeframe constants."""
        if tf == "1H":
            return mt5.TIMEFRAME_H1
        elif tf == "15M":
            return mt5.TIMEFRAME_M15
        elif tf == "5M":
            return mt5.TIMEFRAME_M5
        elif tf == "3M":
            return mt5.TIMEFRAME_M3
        elif tf == "1M":
            return mt5.TIMEFRAME_M1
        elif tf == "1D":
            return mt5.TIMEFRAME_D1
        else:
            logger.warning(f"Unknown timeframe {tf}, defaulting to H1")
            return mt5.TIMEFRAME_H1
