import math
import MetaTrader5 as mt5
import config
from logger import logger

class ExecutionEngine:
    def __init__(self):
        pass
        
    def calculate_lot_size(self, stop_loss_price, order_type):
        """
        Lot size so that hitting the SL loses at most RISK_PERCENT of equity.
        Returns 0 (= skip the trade) if anything is missing or even the
        minimum lot would risk more than allowed.

        NOTE: we ask MT5 for the real loss per lot (order_calc_profit) instead of
        using trade_tick_value: on MetaQuotes-Demo XAUUSD reports tick_value 0.1
        while the true value is $1 per 0.01 per lot, which made lots 10x too big.
        """
        account_info = mt5.account_info()
        symbol_info = mt5.symbol_info(config.SYMBOL)
        tick = mt5.symbol_info_tick(config.SYMBOL)
        if account_info is None or symbol_info is None or tick is None:
            logger.error("Missing account/symbol/tick info - skipping trade.")
            return 0.0

        entry = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
        loss_per_lot = mt5.order_calc_profit(order_type, config.SYMBOL, 1.0, entry, stop_loss_price)
        if loss_per_lot is None or loss_per_lot >= 0:
            logger.error(f"Invalid SL {stop_loss_price} for entry {entry} - skipping trade.")
            return 0.0
        loss_per_lot = abs(loss_per_lot)

        risk_amount = account_info.equity * (config.RISK_PERCENT / 100)
        step = symbol_info.volume_step
        lot_size = math.floor(risk_amount / loss_per_lot / step + 1e-9) * step  # round DOWN
        lot_size = round(min(lot_size, symbol_info.volume_max), 8)

        if lot_size < symbol_info.volume_min:
            logger.warning(f"Min lot {symbol_info.volume_min} would risk "
                           f"${symbol_info.volume_min * loss_per_lot:.2f} > budget ${risk_amount:.2f} - skipping trade.")
            return 0.0

        logger.info(f"Lot {lot_size}: risk ${lot_size * loss_per_lot:.2f} "
                    f"({lot_size * loss_per_lot / account_info.equity * 100:.2f}% of equity)")
        return lot_size

    @staticmethod
    def _filling_mode():
        """Pick a filling mode the broker actually allows for this symbol."""
        info = mt5.symbol_info(config.SYMBOL)
        flags = info.filling_mode if info else 0
        if flags & 2:   # SYMBOL_FILLING_IOC
            return mt5.ORDER_FILLING_IOC
        if flags & 1:   # SYMBOL_FILLING_FOK
            return mt5.ORDER_FILLING_FOK
        return mt5.ORDER_FILLING_RETURN

    def open_position(self, signal, sl_price, tp_r=None):
        """
        Opens a market order. signal is 'BUY' or 'SELL'.
        tp_r: if set, TP is placed at tp_r x the actual risk distance from the fill price.
        """
        order_type = mt5.ORDER_TYPE_BUY if signal == 'BUY' else mt5.ORDER_TYPE_SELL
        lot = self.calculate_lot_size(sl_price, order_type)
        if lot <= 0:
            return False

        tick = mt5.symbol_info_tick(config.SYMBOL)
        if tick is None:
            logger.error("No tick data - order not sent.")
            return False
        price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid

        tp_price = 0.0
        if tp_r:
            direction = 1 if order_type == mt5.ORDER_TYPE_BUY else -1
            tp_price = round(price + direction * tp_r * abs(price - sl_price), 2)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": config.SYMBOL,
            "volume": lot,
            "type": order_type,
            "price": price,
            "sl": round(sl_price, 2),
            "tp": tp_price,
            "deviation": config.MAX_SLIPPAGE,
            "magic": config.MAGIC_NUMBER,
            "comment": "SMC Gold Bot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(),
        }
        
        result = mt5.order_send(request)
        if result is None:
            logger.error(f"Order failed: order_send returned None ({mt5.last_error()})")
            return False
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"Order failed: {result.comment} (code: {result.retcode})")
            return False
            
        logger.info(f"Opened {signal} position! Ticket: {result.order}, Lot: {lot}, "
                    f"SL: {sl_price:.2f}, TP: {tp_price or 'none'}")
        return True

    def manage_breakeven(self, be_at_r, tp_r):
        """Moves SL to entry once price has moved be_at_r x risk in our favour.
        Risk is recovered from the TP (placed at tp_r x risk), so no state is needed."""
        if not be_at_r or not tp_r:
            return
        tick = mt5.symbol_info_tick(config.SYMBOL)
        if tick is None:
            return
        for pos in self.get_open_positions():
            if not pos.tp:
                continue
            risk = abs(pos.tp - pos.price_open) / tp_r
            if pos.type == mt5.ORDER_TYPE_BUY:
                if tick.bid - pos.price_open >= be_at_r * risk and pos.sl < pos.price_open:
                    self._modify_sl(pos, pos.price_open)
            else:
                if pos.price_open - tick.ask >= be_at_r * risk and (pos.sl == 0 or pos.sl > pos.price_open):
                    self._modify_sl(pos, pos.price_open)

    def get_open_positions(self):
        """Returns list of open positions for our bot."""
        positions = mt5.positions_get(symbol=config.SYMBOL)
        if positions is None:
            return []
        # Filter by magic number
        return [p for p in positions if p.magic == config.MAGIC_NUMBER]
        
    def close_position(self, ticket):
        """Closes a specific position."""
        position = mt5.positions_get(ticket=ticket)
        if position is None or len(position) == 0:
            return False
        position = position[0]
            
        tick = mt5.symbol_info_tick(config.SYMBOL)
        order_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": config.SYMBOL,
            "volume": position.volume,
            "type": order_type,
            "position": position.ticket,
            "price": price,
            "deviation": config.MAX_SLIPPAGE,
            "magic": config.MAGIC_NUMBER,
            "comment": "SMC Close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(),
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"Failed to close position {ticket}: "
                         f"{result.comment if result else mt5.last_error()}")
            return False
            
        logger.info(f"Successfully closed position {ticket}.")
        return True

    def manage_positions(self, htf_bias):
        """
        Dynamic exit logic based on structure.
        If HTF bias shifts against our position, we close it early.
        """
        positions = self.get_open_positions()
        for pos in positions:
            # If we are long and HTF shifts to BEARISH
            if pos.type == mt5.ORDER_TYPE_BUY and htf_bias == "BEARISH":
                logger.info(f"Dynamic Exit Triggered: HTF Structure shifted to BEARISH. Closing Long.")
                self.close_position(pos.ticket)
            # If we are short and HTF shifts to BULLISH
            elif pos.type == mt5.ORDER_TYPE_SELL and htf_bias == "BULLISH":
                logger.info(f"Dynamic Exit Triggered: HTF Structure shifted to BULLISH. Closing Short.")
                self.close_position(pos.ticket)

    def manage_trailing_stops(self, htf_atr_val):
        """
        Updates the Stop Loss on the MT5 server dynamically based on ATR trailing logic.
        """
        positions = self.get_open_positions()
        for pos in positions:
            tick = mt5.symbol_info_tick(config.SYMBOL)
            if tick is None:
                return

            if pos.type == mt5.ORDER_TYPE_BUY:
                current_high = tick.bid # Or highest recent price
                # If we are in profit
                if current_high > pos.price_open:
                    new_sl = current_high - (htf_atr_val * config.ATR_TRAIL_MULTIPLIER)
                    if new_sl > pos.sl:
                        self._modify_sl(pos, new_sl)
                        
            elif pos.type == mt5.ORDER_TYPE_SELL:
                current_low = tick.ask
                # If we are in profit
                if current_low < pos.price_open:
                    new_sl = current_low + (htf_atr_val * config.ATR_TRAIL_MULTIPLIER)
                    # For SELL, sl is above price. Only move it down if new_sl is lower than current SL (or if SL wasn't set)
                    if pos.sl == 0.0 or new_sl < pos.sl:
                        self._modify_sl(pos, new_sl)
                        
    def _modify_sl(self, position, new_sl):
        """Helper to modify SL of an existing position."""
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": config.SYMBOL,
            "position": position.ticket,
            "sl": round(new_sl, 2),
            "tp": position.tp, # Keep existing TP
            "magic": config.MAGIC_NUMBER
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"Failed to move SL for {position.ticket}: "
                         f"{result.comment if result else mt5.last_error()}")
        else:
            logger.info(f"Moved SL for {position.ticket} to {new_sl:.2f}")
