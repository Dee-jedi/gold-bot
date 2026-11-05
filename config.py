import pytz
from datetime import time

# --- Account & Risk Settings ---
SYMBOL = "XAUUSD"
RISK_PERCENT = 2.0  # 2% of account per trade
MAX_SPREAD_POINTS = 50  # Avoid trading if spread > 50 points (5 pips)
MAGIC_NUMBER = 7770001  # Unique identifier for the bot's trades
MAX_SLIPPAGE = 10  # Maximum allowed slippage

# --- Timeframes ---
HTF = "1H"   # High Timeframe for Structure & POI (15M/1M had no edge in testing)
LTF = "15M"  # Low Timeframe for Entry

# --- Trading Hours (Kill Zones in UTC) ---
# London Open Kill Zone: sweeps Asian session highs/lows
LONDON_OPEN_START = time(6, 0)   # 06:00 UTC (Early London)
LONDON_OPEN_END = time(12, 0)    # 12:00 UTC (London Close/NY Open)

# New York Open Kill Zone: sweeps London session highs/lows
NY_OPEN_START = time(12, 0)      # 12:00 UTC (NY Open)
NY_OPEN_END = time(20, 0)        # 20:00 UTC (NY Close)

TIMEZONE = pytz.UTC

# MT5 bar times are in the broker's SERVER time, not UTC. data_feed converts them
# to UTC using this zone so kill zones / daily resets line up in live and backtest.
# MetaQuotes-Demo runs on EET with EU daylight saving (UTC+2 winter, UTC+3 summer).
# Check yours: the daily gold break should reopen at 01:00 server time.
SERVER_TIMEZONE = "Europe/Athens"

# --- HTF Structure (see strategy_smc.py) ---
SWING_LOOKBACK = 5          # HTF fractal: swing = extreme of 5 bars each side
MIN_BOS_FOR_BIAS = 1        # breaks in the same direction needed before we trade it
POI_MAX_AGE_BARS = 192      # POI expires after this many HTF bars (192 x 1H = 8 days)
OB_SEARCH_BARS = 5          # how far back from the leg origin to look for the OB candle
USE_FVG_POI = False         # also use FVGs left by the breaking leg as POIs

# --- Big-picture trend filter (only trade with it) ---
TREND_FILTER = True         # require TREND_TF trend to agree with the HTF bias
TREND_TF = "1H"
TREND_EMA = 200             # 200 EMA on 1H ~ 50 EMA on H4
TREND_SLOPE_BARS = 10       # EMA must be rising/falling over this many bars

# --- LTF Entry Trigger ---
LTF_SWING_LENGTH = 2        # LTF fractal size for sweep / CHoCH swings
SWEEP_LOOKBACK = 20         # the sweep must have happened within the last N LTF bars
SWEEP_REF_BARS = 30         # LTF swings older than the sweep (within N bars) that can be swept
SWEEP_ANY = True            # True: sweeping any of those swings counts; False: only the latest
CHOCH_LOOKBACK = 15         # CHoCH level = latest LTF swing from up to N bars before the sweep
REQUIRE_LIQUIDITY_SWEEP = True
REQUIRE_DISPLACEMENT = False  # breaking candle must be body-dominant and >= DISPLACEMENT_ATR x ATR
DISPLACEMENT_ATR = 1.0
SL_MODE = "sweep"           # "sweep": SL beyond sweep wick | "poi": beyond sweep or POI, whichever is further
MIN_SL_ATR = 0.5            # skip if SL is closer than this x LTF ATR (noise stop-outs)
MAX_TRADES_PER_POI = 1      # each POI can be traded once

# --- ATR Settings ---
ATR_PERIOD = 14             # Period for ATR calculation
ATR_TRAIL_MULTIPLIER = 1.5  # Trailing stop distance from extreme (x HTF ATR)

# --- Exits ---
EXIT_MODE = "rr"            # "trail": HTF-ATR trailing stop | "rr": fixed TP | "partial": 50% at TP, rest trails
TP_R = 3.0                  # take profit at this multiple of the risk (rr / partial)
BE_AT_R = 1.0               # move SL to breakeven at this R (0 = off)
EXIT_ON_BIAS_FLIP = True    # close when HTF trend flips against the trade

# --- Risk Controls ---
MAX_DAILY_DRAWDOWN_PERCENT = 6.0  # Stop trading if daily loss exceeds this

# --- Backtest cost model per symbol (realistic fills) ---
# point: price step | contract: units per lot (P/L in USD = price move x contract x lots)
# min_spread: points charged at least (MT5 bars store the bar's MIN spread; Dukascopy has none)
# slippage: extra points on every market / stop fill | swap: $ per lot per night (x3 Wednesday)
# commission: $ round-turn per lot (MetaQuotes-Demo: 0; ECN brokers ~$7 with tighter spreads)
SYMBOL_SPECS = {
    "XAUUSD": dict(point=0.01, contract=100, min_spread=20, slippage=5,
                   swap_long=-12.6, swap_short=-4.6, commission=0.0),
    "EURUSD": dict(point=0.00001, contract=100_000, min_spread=10, slippage=2,   # 1 pip retail spread
                   swap_long=-0.7, swap_short=-1.0, commission=0.0),
    # BTC CFD (typical retail broker; MetaQuotes-Demo has no BTCUSD): 1 lot = 1 BTC,
    # spread 0.04% of price, slippage 0.01%, financing ~ -22%/yr longs, -11%/yr shorts, every night
    "BTCUSD": dict(point=0.01, contract=1, min_spread=0, slippage=0, spread_pct=0.0004,
                   slippage_pct=0.0001, swap_long=0, swap_short=0, swap_pct_long=-0.0006,
                   swap_pct_short=-0.0003, swap_daily=True, max_spread_points=None, commission=0.0),
}
