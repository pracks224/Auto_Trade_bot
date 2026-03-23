# strategy/trade.py

class Trade:
    def __init__(self, entry, exit=None, direction=None, sl=None, tp=None, lot=None, profit=None, entry_index=None):
        self.entry = entry
        self.exit = exit
        self.direction = direction
        self.sl = sl
        self.tp = tp
        self.lot = lot
        self.profit = profit
        self.entry_index = entry_index