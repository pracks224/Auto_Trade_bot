# models/trade.py
class Trade:
    def __init__(self, entry_price, lot, direction, sl, tp, time):
        self.entry_price = entry_price
        self.lot = lot
        self.direction = direction  # BUY=1, SELL=-1
        self.sl = sl
        self.tp = tp
        self.open_time = time

        self.close_price = None
        self.close_time = None
        self.profit = 0
        self.status = "OPEN"

    def close(self, price, time):
        self.close_price = price
        self.close_time = time

        if self.direction == 1:
            self.profit = (price - self.entry_price) * self.lot
        else:
            self.profit = (self.entry_price - price) * self.lot

        self.status = "CLOSED"