# engine/backtester.py

from engine.execution import ExecutionEngine
from engine.risk import RiskManager
from engine.portfolio import Portfolio
from utils.logger import get_logger

logger = get_logger()


class Backtester:

    def __init__(self, data, strategy):
        self.strategy = strategy(data)
        self.data = self.strategy.prepare()

        self.execution = ExecutionEngine()
        self.portfolio = Portfolio()
        self.risk = RiskManager(self.portfolio.balance)

    def run(self):
        logger.info("Starting backtest...")

        df = self.data

        for i in range(200, len(df)):  # warmup period
            row = df.iloc[i]

            price = row['close']

            # ================= ENTRY =================
            if self.portfolio.open_trade is None:

                # BUY condition
                if row['ema9'] > row['ema200'] and row['rsi'] < 30:
                    entry = self.execution.execute_order(price, 1)

                    sl = entry - row['atr'] * 2
                    tp = entry + row['atr'] * 4

                    lot = self.risk.calculate_lot(entry, sl)

                    self.portfolio.open_trade_fn(
                        entry, lot, 1, sl, tp, row.name
                    )

                    logger.info(f"BUY opened at {entry}")

                # SELL condition
                elif row['ema9'] < row['ema200'] and row['rsi'] > 70:
                    entry = self.execution.execute_order(price, -1)

                    sl = entry + row['atr'] * 2
                    tp = entry - row['atr'] * 4

                    lot = self.risk.calculate_lot(entry, sl)

                    self.portfolio.open_trade_fn(
                        entry, lot, -1, sl, tp, row.name
                    )

                    logger.info(f"SELL opened at {entry}")

            # ================= EXIT =================
            self.portfolio.check_close(price, row.name)

            # Optional progress log
            if i % 1000 == 0:
                logger.info(f"Processed {i} candles")

        logger.info("Backtest completed")
        logger.info(f"Final Balance: {self.portfolio.balance}")

        return self.portfolio