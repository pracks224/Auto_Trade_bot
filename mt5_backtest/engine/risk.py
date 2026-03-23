# engine/risk.py

class RiskManager:

    def __init__(self, balance):
        self.balance = balance

    def calculate_lot(self, risk_distance, risk_per_trade=0.01):
        """
        Lot sizing proportional to account size and risk distance
        """
        return max(0.01, (self.balance * risk_per_trade) / risk_distance)

    def calculate_sl_tp(self, price, atr_value, direction, sl_mult=1.5, tp_mult=3.0):
        """
        Calculate stop loss and take profit using ATR multipliers
        """
        if direction == 1:
            sl = price - atr_value * sl_mult
            tp = price + atr_value * tp_mult
        else:
            sl = price + atr_value * sl_mult
            tp = price - atr_value * tp_mult
        return sl, tp

    def update_trailing_stop(self, trade, current_price, atr_value, trail_mult=1.0):
        """
        Move SL for trailing stop (ATR-based)
        """
        if trade.direction == 1:
            new_sl = max(trade.sl, current_price - atr_value * trail_mult)
        else:
            new_sl = min(trade.sl, current_price + atr_value * trail_mult)
        return new_sl