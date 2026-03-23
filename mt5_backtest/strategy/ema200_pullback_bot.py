# strategy/ema200_pullback_bot.py

import pandas as pd
import numpy as np
from utils.indicators import ema, rsi, atr, bollinger_bands
from engine.risk import RiskManager
from strategy.trade import Trade
from utils.logger import get_logger

logger = get_logger()

class EMA200PullbackBacktest:

    def __init__(self, df, balance=10000):
        self.df = df.copy()
        self.balance = balance
        self.trades = []
        self.risk = RiskManager(balance)
        self.equity_curve = []

    def run(self):
        df = self.df

        # ===== CALCULATE INDICATORS =====
        df['ema9'] = ema(df['close'], 9)
        df['ema200'] = ema(df['close'], 200)
        df['rsi'] = rsi(df['close'], 14)
        df['atr'] = atr(df, 14)
        df['bb_upper'], df['bb_lower'] = bollinger_bands(df['close'], 20, 2)

        open_trade = None

        # ===== MAIN BACKTEST LOOP =====
        for i in range(201, len(df)):
            row = df.iloc[i]
            prev = df.iloc[i-1]
            prev2 = df.iloc[i-2]

            price = row['close']
            direction = 0

            # ===== TRADE SIGNALS =====
            # BUY condition
            if (price > row['ema200'] and price <= row['ema9'] and
                ((row['close'] > prev['high']) or (row['high'] > prev['high'] and prev['high'] > prev2['high'])) and
                (row['rsi'] < 30 or row['close'] <= row['bb_lower'])):
                direction = 1

            # SELL condition
            elif (price < row['ema200'] and price >= row['ema9'] and
                  ((row['close'] < prev['low']) or (row['low'] < prev['low'] and prev['low'] < prev2['low'])) and
                  (row['rsi'] > 70 or row['close'] >= row['bb_upper'])):
                direction = -1

            # ===== OPEN NEW TRADE =====
            if open_trade is None and direction != 0:
                sl, tp = self.risk.calculate_sl_tp(price, row['atr'], direction)
                lot = self.risk.calculate_lot(abs(sl - price))
                open_trade = Trade(
                    entry=price,
                    direction=direction,
                    sl=sl,
                    tp=tp,
                    lot=lot,
                    entry_index=i
                )
                logger.info(f"Trade opened: {direction} at {price}, SL: {sl}, TP: {tp}, lot: {lot}")

            # ===== UPDATE TRAILING STOP =====
            if open_trade is not None:
                open_trade.sl = self.risk.update_trailing_stop(open_trade, price, row['atr'])

            # ===== CHECK EXIT =====
            if open_trade is not None:
                if open_trade.direction == 1:  # BUY
                    if row['low'] <= open_trade.sl:
                        profit = -abs(open_trade.sl - open_trade.entry) * open_trade.lot
                        self._close_trade(open_trade, profit, i)
                        open_trade = None
                    elif row['high'] >= open_trade.tp:
                        profit = abs(open_trade.tp - open_trade.entry) * open_trade.lot
                        self._close_trade(open_trade, profit, i)
                        open_trade = None
                elif open_trade.direction == -1:  # SELL
                    if row['high'] >= open_trade.sl:
                        profit = -abs(open_trade.sl - open_trade.entry) * open_trade.lot
                        self._close_trade(open_trade, profit, i)
                        open_trade = None
                    elif row['low'] <= open_trade.tp:
                        profit = abs(open_trade.tp - open_trade.entry) * open_trade.lot
                        self._close_trade(open_trade, profit, i)
                        open_trade = None

            # ===== UPDATE EQUITY CURVE =====
            self.equity_curve.append(self.balance)

        logger.info("Backtest run complete")
        return self.trades

    # ===== CLOSE TRADE METHOD =====
    def _close_trade(self, trade, profit, exit_index):
        trade.exit = exit_index
        trade.profit = profit
        self.balance += profit
        self.trades.append(trade)
        logger.info(f"Trade closed at index {exit_index}, profit: {profit}, balance: {self.balance}")