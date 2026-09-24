import MetaTrader5 as mt5
import config
from logger import logger

class ExecutionEngine:
    def __init__(self):
        pass
        
    def calculate_lot_size(self, stop_loss_price, order_type):
        """
        Calculates lot size based on 4% risk and distance to stop loss.
        """
        account_info = mt5.account_info()
        if account_info is None:
            logger.error("Failed to retrieve account info")
            return 0.01 # Fallback to min lot

        balance = account_info.balance
        risk_amount = balance * (config.RISK_PERCENT / 100)
        
        tick = mt5.symbol_info_tick(config.SYMBOL)
        current_price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
        
        # Calculate sl distance in points
        sl_distance_points = abs(current_price - stop_loss_price) / mt5.symbol_info(config.SYMBOL).point
        
        if sl_distance_points <= 0:
            logger.error("Invalid SL distance.")
            return 0.01

        # Lot size = Risk Amount / (SL points * tick value)
        # For XAUUSD, tick value is typically 1 per standard lot (100 oz) depending on broker, let's use MT5 built in functions.
        # However, to keep it simple and robust, many MT5 wrappers use a standard formula.
        # Let's use mt5.order_calc_margin or a simplified estimation:
        tick_value = mt5.symbol_info(config.SYMBOL).trade_tick_value
        if tick_value == 0: tick_value = 1
        
        lot_size = risk_amount / (sl_distance_points * tick_value)
        
        # Clamp to broker limits
        symbol_info = mt5.symbol_info(config.SYMBOL)
        min_lot = symbol_info.volume_min
        max_lot = symbol_info.volume_max
        step = symbol_info.volume_step
        
        # Round to step
        lot_size = round(lot_size / step) * step
        lot_size = max(min_lot, min(lot_size, max_lot))
        
        return lot_size

    def open_position(self, signal, sl_price):
        """
        Opens a market order. signal is 'BUY' or 'SELL'.
        """
        order_type = mt5.ORDER_TYPE_BUY if signal == 'BUY' else mt5.ORDER_TYPE_SELL
        lot = self.calculate_lot_size(sl_price, order_type)
        
        tick = mt5.symbol_info_tick(config.SYMBOL)
        price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
        
        # We don't set TP here because the user wants it to be managed dynamically by structure.
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": config.SYMBOL,
            "volume": lot,
            "type": order_type,
            "price": price,
            "sl": sl_price,
            "deviation": config.MAX_SLIPPAGE,
            "magic": config.MAGIC_NUMBER,
            "comment": "SMC Gold Bot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"Order failed: {result.comment} (code: {result.retcode})")
            return False
            
        logger.info(f"Opened {signal} position! Ticket: {result.order}, Lot: {lot}, SL: {sl_price}")
        return True

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
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"Failed to close position {ticket}: {result.comment}")
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
            "sl": new_sl,
            "tp": position.tp, # Keep existing TP
            "magic": config.MAGIC_NUMBER
        }
        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"Failed to trail SL for {position.ticket}: {result.comment}")
        else:
            logger.info(f"Trailed SL for {position.ticket} to {new_sl:.2f}")
