# strategy/high_win_rate_bot.py

import pandas as pd
import numpy as np
from utils.indicators import ema, rsi, atr, bollinger_bands
from strategy.trade import Trade
from engine.risk import RiskManager
from utils.logger import get_logger

logger = get_logger()

class SmartMartingaleBacktest:

    def __init__(self, df, balance=10000, base_lot=1, small_lot=0.5):
        self.df = df.copy()
        self.balance = balance
        self.base_lot = base_lot
        self.small_lot = small_lot
        self.trades = []
        self.open_trade = None
        self.risk = RiskManager(balance)
        self.equity_curve = []
        self.peak_equity = balance
        self.max_drawdown = 0

    def run(self):
        df = self.df

        # ===== INDICATORS =====
        df['ema50'] = ema(df['close'], 50)
        df['ema200'] = ema(df['close'], 200)
        df['rsi'] = rsi(df['close'], 14)
        df['atr'] = atr(df, 14)
        df['bb_upper'], df['bb_lower'] = bollinger_bands(df['close'], 20, 2)

        for i in range(200, len(df)):
            row = df.iloc[i]
            price = row['close']
            direction = 0

            # ===== TREND + PULLBACK SIGNAL =====
            # BUY in uptrend
            if row['ema50'] > row['ema200'] and row['rsi'] < 40 and price <= row['bb_lower']:
                direction = 1

            # SELL in downtrend
            elif row['ema50'] < row['ema200'] and row['rsi'] > 60 and price >= row['bb_upper']:
                direction = -1

            # ===== DETERMINE LOT BASED ON ATR =====
            if direction != 0:
                lot = self.base_lot if row['atr'] < 15 else self.small_lot
                sl, tp = self.risk.calculate_sl_tp(price, row['atr'], direction, sl_mult=1.5, tp_mult=3.0)

                self.open_trade = Trade(
                    entry=price,
                    direction=direction,
                    sl=sl,
                    tp=tp,
                    lot=lot,
                    entry_index=i
                )
                logger.info(f"Trade opened: {direction} at {price:.2f}, SL: {sl:.2f}, TP: {tp:.2f}, lot: {lot}")

            # ===== UPDATE OPEN TRADE =====
            if self.open_trade is not None:
                self.open_trade.sl = self.risk.update_trailing_stop(self.open_trade, price, row['atr'])

                if self.open_trade.direction == 1:
                    if row['low'] <= self.open_trade.sl:
                        profit = -abs(self.open_trade.sl - self.open_trade.entry) * self.open_trade.lot
                        self._close_trade(profit, i)
                    elif row['high'] >= self.open_trade.tp:
                        profit = abs(self.open_trade.tp - self.open_trade.entry) * self.open_trade.lot
                        self._close_trade(profit, i)

                elif self.open_trade.direction == -1:
                    if row['high'] >= self.open_trade.sl:
                        profit = -abs(self.open_trade.sl - self.open_trade.entry) * self.open_trade.lot
                        self._close_trade(profit, i)
                    elif row['low'] <= self.open_trade.tp:
                        profit = abs(self.open_trade.tp - self.open_trade.entry) * self.open_trade.lot
                        self._close_trade(profit, i)

            # ===== EQUITY CURVE + DRAWDOWN =====
            floating_pnl = self._calculate_floating_pnl(price)
            equity = self.balance + floating_pnl
            self.equity_curve.append(equity)

            if equity > self.peak_equity:
                self.peak_equity = equity
            dd = self.peak_equity - equity
            if dd > self.max_drawdown:
                self.max_drawdown = dd

        logger.info("Backtest run complete")
        return self.trades, self.equity_curve, self.max_drawdown

    # ===== CLOSE TRADE =====
    def _close_trade(self, profit, exit_index):
        self.open_trade.exit = exit_index
        self.open_trade.profit = profit
        self.balance += profit
        self.trades.append(self.open_trade)
        logger.info(f"Trade closed at index {exit_index}, profit: {profit:.2f}, balance: {self.balance:.2f}")
        self.open_trade = None

    # ===== FLOATING PNL =====
    def _calculate_floating_pnl(self, price):
        if self.open_trade is None:
            return 0
        trade = self.open_trade
        if trade.direction == 1:
            return (price - trade.entry) * trade.lot
        else:
            return (trade.entry - price) * trade.lot