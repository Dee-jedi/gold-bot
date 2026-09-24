import pytz
from datetime import time

# --- Account & Risk Settings ---
SYMBOL = "XAUUSD"
RISK_PERCENT = 2.0  # 2% of account per trade
MAX_SPREAD_POINTS = 50  # Avoid trading if spread > 50 points (5 pips)
MAGIC_NUMBER = 7770001  # Unique identifier for the bot's trades
MAX_SLIPPAGE = 10  # Maximum allowed slippage

# --- Timeframes ---
HTF = "15M"  # High Timeframe for Structure & POI
LTF = "1M"   # Low Timeframe for Entry

# --- Trading Hours (Kill Zones in UTC) ---
# London Open Kill Zone: sweeps Asian session highs/lows
LONDON_OPEN_START = time(6, 0)   # 06:00 UTC (Early London)
LONDON_OPEN_END = time(12, 0)    # 12:00 UTC (London Close/NY Open)

# New York Open Kill Zone: sweeps London session highs/lows
NY_OPEN_START = time(12, 0)      # 12:00 UTC (NY Open)
NY_OPEN_END = time(20, 0)        # 20:00 UTC (NY Close)

TIMEZONE = pytz.UTC

# --- Strategy Settings ---
# How many bars back to look for structural highs/lows
SWING_LOOKBACK = 5
# Minimum size of FVG to be considered valid (in points)
MIN_FVG_SIZE = 10
# Wait for LTF CHoCH confirmation after tapping HTF POI?
REQUIRE_LTF_CHOCH = True

# --- ATR Settings ---
ATR_PERIOD = 14          # Period for ATR calculation
ATR_SL_MULTIPLIER = 0.5  # Buffer added to SL beyond OB edge
ATR_TRAIL_MULTIPLIER = 1.5  # Trailing stop distance from extreme

# --- Entry Filters (toggle individually to find best combo) ---
USE_PREMIUM_DISCOUNT = True      # Only buy in discount, sell in premium
REQUIRE_LIQUIDITY_SWEEP = True   # Require a liquidity sweep before entry
REQUIRE_FVG = False              # Require FVG displacement confirmation

# --- Risk Controls ---
MAX_DAILY_DRAWDOWN_PERCENT = 6.0  # Stop trading if daily loss exceeds this
