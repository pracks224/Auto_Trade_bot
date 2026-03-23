# main.py

from datetime import datetime
import MetaTrader5 as mt5
import matplotlib.pyplot as plt
import numpy as np

from data.data_loader import MT5DataLoader
from strategy.smart_martingale_bot import SmartMartingaleBacktest
from utils.logger import get_logger

logger = get_logger()

def run_backtest():
    logger.info("===== MT5 BACKTEST STARTED =====")

    SYMBOL = "XAUUSD_"
    TIMEFRAME = mt5.TIMEFRAME_M1
    BARS = 10000

    # Connect MT5 and fetch data
    loader = MT5DataLoader(SYMBOL, TIMEFRAME)
    loader.connect()
    df = loader.fetch_data(bars=BARS)
    logger.info(f"Fetched {len(df)} bars")

    # Run backtest
    bot = SmartMartingaleBacktest(df)
    trades, equity_curve, max_dd = bot.run()

    # Performance summary
    wins = [t.profit for t in trades if t.profit > 0]
    losses = [t.profit for t in trades if t.profit <= 0]
    total_trades = len(trades)
    net_profit = sum([t.profit for t in trades])
    win_rate = len(wins)/total_trades*100 if total_trades else 0
    profit_factor = sum(wins)/abs(sum(losses)) if losses else float('inf')

    logger.info("===== PERFORMANCE REPORT =====")
    logger.info(f"Total Trades: {total_trades}")
    logger.info(f"Win Rate: {win_rate:.2f}%")
    logger.info(f"Net Profit: {net_profit:.2f}")
    logger.info(f"Profit Factor: {profit_factor:.2f}")
    logger.info(f"Max Drawdown: {max_dd:.2f}")
    logger.info(f"Final Balance: {bot.balance:.2f}")

    # Plot equity
    plt.figure()
    plt.plot(equity_curve)
    plt.title("Equity Curve")
    plt.xlabel("Bars")
    plt.ylabel("Equity")
    plt.grid()
    plt.show()

if __name__ == "__main__":
    run_backtest()