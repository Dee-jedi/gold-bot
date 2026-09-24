import pytz
from datetime import datetime, timedelta
import pandas as pd
import time
from data_feed import DataFeed
from strategy_smc import SMCStrategy
import config

print("Starting Backtest Engine...")

# Overrides for Backtesting
config.HTF = "5M"
config.LTF = "1M"
SPREAD = 20  # points (2 pips for volatile pairs like GBPJPY)
COMMISSION = 0  # simple

data_feed = DataFeed()
strategy = SMCStrategy(swing_length=config.SWING_LOOKBACK)

# 1. Fetch Data
print("Fetching historical data (up to 4 months/120k bars)...")
# 98,000 bars of 1M data
# 19,600 bars of 5M data matches the same period
htf_df = data_feed.get_data(config.HTF, num_bars=19600)
ltf_df = data_feed.get_data(config.LTF, num_bars=98000)

data_feed.shutdown()

if htf_df.empty or ltf_df.empty:
    print("Failed to fetch data. Max bars may be limited by broker.")
    exit()

date_from = ltf_df['time'].iloc[0]
date_to = ltf_df['time'].iloc[-1]



print(f"Loaded {len(htf_df)} HTF bars and {len(ltf_df)} LTF bars.")

# Pre-calculate HTF ATR for trailing stops (much wider than 1M ATR)
htf_atr = strategy.calculate_atr(htf_df, period=config.ATR_PERIOD)
htf_df['atr'] = htf_atr

# Map HTF ATR values to LTF timestamps
htf_atr_times = htf_df[['time', 'atr']].dropna().copy()

def get_htf_atr(current_time):
    """Get the most recent HTF ATR value for a given LTF timestamp."""
    valid = htf_atr_times[htf_atr_times['time'] <= current_time]
    if valid.empty:
        return 0
    return valid['atr'].iloc[-1]

# 2. Pre-calculate HTF States to optimize simulation speed
print("Pre-calculating HTF states (this may take a few seconds)...")
htf_states = {}  # key: time, value: (bias, pois)

for i in range(config.SWING_LOOKBACK * 2, len(htf_df)):
    sub_htf = htf_df.iloc[:i+1].copy()
    bias, pois = strategy.get_htf_bias_and_pois(sub_htf)
    htf_states[htf_df['time'].iloc[i]] = (bias, pois)

# Helper to get the most recent HTF state
htf_times = pd.Series(list(htf_states.keys()))

def get_htf_state(current_time):
    valid_times = htf_times[htf_times <= current_time]
    if valid_times.empty:
        return "NEUTRAL", []
    latest_time = valid_times.iloc[-1]
    return htf_states[latest_time]

def is_kill_zone(t):
    """Check if time falls within any kill zone (London Open or NY Open)."""
    t = t.time() if hasattr(t, 'time') else t
    in_london = config.LONDON_OPEN_START <= t <= config.LONDON_OPEN_END
    in_ny = config.NY_OPEN_START <= t <= config.NY_OPEN_END
    return in_london or in_ny

# 3. Simulate Execution
print("Running simulation loop...")
trades = []
open_trade = None
daily_pnl = 0.0
current_trading_day = None

for ltf_idx in range(50, len(ltf_df)):
    current_time = ltf_df['time'].iloc[ltf_idx]
    current_price = ltf_df['close'].iloc[ltf_idx]

    # --- Daily Drawdown Circuit Breaker ---
    trade_day = current_time.date()
    if current_trading_day != trade_day:
        current_trading_day = trade_day
        daily_pnl = 0.0  # Reset daily P&L

    if daily_pnl <= -config.MAX_DAILY_DRAWDOWN_PERCENT:
        # Daily loss limit hit — don't open new trades, but still manage open ones
        pass
    
    # Get the HTF ATR for trailing stop (much wider than 1M ATR)
    htf_atr_val = get_htf_atr(current_time)

    htf_bias, pois = get_htf_state(current_time)

    # Manage Open Trade
    if open_trade is not None:
        # --- ATR Trailing Stop (using HTF ATR, only after break-even) ---
        if htf_atr_val > 0:
            if open_trade['type'] == 'BUY':
                # Track highest price since entry
                if current_price > open_trade.get('highest_since_entry', open_trade['entry_price']):
                    open_trade['highest_since_entry'] = current_price
                # Only start trailing after trade is at least break-even
                if open_trade['highest_since_entry'] > open_trade['entry_price']:
                    trail_stop = open_trade['highest_since_entry'] - (htf_atr_val * config.ATR_TRAIL_MULTIPLIER)
                    # Only move SL up, never down
                    if trail_stop > open_trade['sl']:
                        open_trade['sl'] = trail_stop

            elif open_trade['type'] == 'SELL':
                # Track lowest price since entry
                if current_price < open_trade.get('lowest_since_entry', open_trade['entry_price']):
                    open_trade['lowest_since_entry'] = current_price
                # Only start trailing after trade is at least break-even
                if open_trade['lowest_since_entry'] < open_trade['entry_price']:
                    trail_stop = open_trade['lowest_since_entry'] + (htf_atr_val * config.ATR_TRAIL_MULTIPLIER)
                    # Only move SL down, never up
                    if trail_stop < open_trade['sl']:
                        open_trade['sl'] = trail_stop

        # Check Stop Loss
        if open_trade['type'] == 'BUY' and ltf_df['low'].iloc[ltf_idx] <= open_trade['sl']:
            open_trade['exit_price'] = open_trade['sl']
            open_trade['exit_time'] = current_time
            entry_dist = open_trade['entry_price'] - open_trade['initial_sl']
            if entry_dist > 0:
                actual_dist = open_trade['exit_price'] - open_trade['entry_price']
                total_cost = (SPREAD + config.MAX_SLIPPAGE) * 0.01
                open_trade['pnl'] = ((actual_dist - total_cost) / entry_dist) * config.RISK_PERCENT
            else:
                open_trade['pnl'] = -config.RISK_PERCENT
            daily_pnl += open_trade['pnl']
            trades.append(open_trade)
            open_trade = None
            continue

        elif open_trade['type'] == 'SELL' and ltf_df['high'].iloc[ltf_idx] >= open_trade['sl']:
            open_trade['exit_price'] = open_trade['sl']
            open_trade['exit_time'] = current_time
            entry_dist = open_trade['initial_sl'] - open_trade['entry_price']
            if entry_dist > 0:
                actual_dist = open_trade['entry_price'] - open_trade['exit_price']
                total_cost = (SPREAD + config.MAX_SLIPPAGE) * 0.01
                open_trade['pnl'] = ((actual_dist - total_cost) / entry_dist) * config.RISK_PERCENT
            else:
                open_trade['pnl'] = -config.RISK_PERCENT
            daily_pnl += open_trade['pnl']
            trades.append(open_trade)
            open_trade = None
            continue

        # Check Dynamic Exit (HTF bias shift)
        if open_trade['type'] == 'BUY' and htf_bias == 'BEARISH':
            open_trade['exit_price'] = current_price
            open_trade['exit_time'] = current_time
            entry_dist = open_trade['entry_price'] - open_trade['initial_sl']
            actual_dist = current_price - open_trade['entry_price']
            total_cost = (SPREAD + config.MAX_SLIPPAGE) * 0.01
            open_trade['pnl'] = ((actual_dist - total_cost) / entry_dist) * config.RISK_PERCENT if entry_dist > 0 else 0
            daily_pnl += open_trade['pnl']
            trades.append(open_trade)
            open_trade = None
            continue

        elif open_trade['type'] == 'SELL' and htf_bias == 'BULLISH':
            open_trade['exit_price'] = current_price
            open_trade['exit_time'] = current_time
            entry_dist = open_trade['initial_sl'] - open_trade['entry_price']
            actual_dist = open_trade['entry_price'] - current_price
            total_cost = (SPREAD + config.MAX_SLIPPAGE) * 0.01
            open_trade['pnl'] = ((actual_dist - total_cost) / entry_dist) * config.RISK_PERCENT if entry_dist > 0 else 0
            daily_pnl += open_trade['pnl']
            trades.append(open_trade)
            open_trade = None
            continue

    # Look for Entries (Only during kill zones AND if daily drawdown limit not hit)
    if (open_trade is None
        and is_kill_zone(current_time)
        and daily_pnl > -config.MAX_DAILY_DRAWDOWN_PERCENT):

        sub_ltf = ltf_df.iloc[ltf_idx-50 : ltf_idx+1].copy()
        signal, sl_price = strategy.check_ltf_entry(sub_ltf, htf_bias, pois)

        if signal:
            open_trade = {
                'type': signal,
                'entry_time': current_time,
                'entry_price': current_price,
                'sl': sl_price,
                'initial_sl': sl_price,  # Keep original SL for R-multiple calc
                'highest_since_entry': current_price,
                'lowest_since_entry': current_price,
            }

# 4. Generate Report
print("\n" + "=" * 50)
print("         BACKTEST REPORT (Enhanced SMC)")
print("=" * 50)
print(f"Period: {date_from.date()} to {date_to.date()}")
print(f"Strategy: {config.HTF} POI -> {config.LTF} CHoCH + Filters")
print(f"Risk per trade: {config.RISK_PERCENT}%")
print(f"ATR SL Buffer: {config.ATR_SL_MULTIPLIER}x | Trail: {config.ATR_TRAIL_MULTIPLIER}x")
print(f"Kill Zones: London {config.LONDON_OPEN_START}-{config.LONDON_OPEN_END} | NY {config.NY_OPEN_START}-{config.NY_OPEN_END}")
print(f"Premium/Discount Filter: {'ON' if config.USE_PREMIUM_DISCOUNT else 'OFF'}")
print(f"Liquidity Sweep: {'ON' if config.REQUIRE_LIQUIDITY_SWEEP else 'OFF'} | FVG Confirm: {'ON' if config.REQUIRE_FVG else 'OFF'}")
print(f"Daily Drawdown Limit: {config.MAX_DAILY_DRAWDOWN_PERCENT}%")
print("-" * 50)
print(f"Total Trades: {len(trades)}")

if len(trades) > 0:
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]

    win_rate = len(wins) / len(trades) * 100
    total_return = sum(t['pnl'] for t in trades)
    avg_win = sum(t['pnl'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['pnl'] for t in losses) / len(losses) if losses else 0
    max_win = max(t['pnl'] for t in trades)
    max_loss = min(t['pnl'] for t in trades)

    # Balance Simulator ($25k starting)
    starting_balance = 25000.00
    current_balance = starting_balance
    for t in trades:
        dollar_pnl = current_balance * (t['pnl'] / 100)
        t['dollar_pnl'] = dollar_pnl
        current_balance += dollar_pnl
    
    total_dollar_profit = current_balance - starting_balance

    # Calculate max drawdown
    cumulative = 0
    peak = 0
    max_dd = 0
    for t in trades:
        cumulative += t['pnl']
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    print(f"Wins: {len(wins)} | Losses: {len(losses)}")
    print(f"Win Rate: {win_rate:.1f}%")
    print(f"Total Return: {total_return:+.2f}%")
    print(f"Avg Win: {avg_win:+.2f}% | Avg Loss: {avg_loss:+.2f}%")
    print(f"Best Trade: {max_win:+.2f}% | Worst Trade: {max_loss:+.2f}%")
    print(f"Max Drawdown: {max_dd:.2f}%")

    if avg_loss != 0:
        profit_factor = abs(sum(t['pnl'] for t in wins) / sum(t['pnl'] for t in losses)) if losses else float('inf')
        print(f"Profit Factor: {profit_factor:.2f}")

    print("-" * 50)
    print(f"Starting Balance: ${starting_balance:,.2f}")
    print(f"Ending Balance:   ${current_balance:,.2f}")
    print(f"Total Profit:     ${total_dollar_profit:+,.2f} (Compounded)")

    # Print individual trades
    print("\n--- Trade Log ---")
    for i, t in enumerate(trades, 1):
        result = "WIN " if t['pnl'] > 0 else "LOSS"
        print(f"  #{i:02d} [{result}] {t['type']:4s} | "
              f"Entry: {t['entry_time']} @ {t['entry_price']:.2f} | "
              f"Exit: {t['exit_time']} @ {t['exit_price']:.2f} | "
              f"PnL: {t['pnl']:+.2f}% (${t['dollar_pnl']:+,.2f})")
else:
    print("No trades taken.")

strategy.print_filter_stats()
print("=" * 50)
