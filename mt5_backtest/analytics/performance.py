# analytics/performance.py
import numpy as np

class PerformanceAnalyzer:

    def __init__(self, portfolio):
        self.trades = portfolio.trades
        self.balance = portfolio.balance

    def analyze(self):
        if not self.trades:
            print("No trades executed")
            return

        profits = np.array([t.profit for t in self.trades])

        wins = profits[profits > 0]
        losses = profits[profits <= 0]

        total_trades = len(profits)
        win_rate = len(wins) / total_trades * 100

        total_profit = profits.sum()

        gross_profit = wins.sum() if len(wins) > 0 else 0
        gross_loss = abs(losses.sum()) if len(losses) > 0 else 1

        profit_factor = gross_profit / gross_loss

        avg_win = wins.mean() if len(wins) > 0 else 0
        avg_loss = losses.mean() if len(losses) > 0 else 0

        expectancy = (win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss)

        sharpe = self.calculate_sharpe(profits)

        max_dd = self.calculate_drawdown(profits)

        print("\n===== PERFORMANCE REPORT =====")
        print(f"Total Trades: {total_trades}")
        print(f"Win Rate: {win_rate:.2f}%")
        print(f"Net Profit: {total_profit:.2f}")
        print(f"Profit Factor: {profit_factor:.2f}")
        print(f"Avg Win: {avg_win:.2f}")
        print(f"Avg Loss: {avg_loss:.2f}")
        print(f"Expectancy: {expectancy:.2f}")
        print(f"Sharpe Ratio: {sharpe:.2f}")
        print(f"Max Drawdown: {max_dd:.2f}")
        print(f"Final Balance: {self.balance:.2f}")

    def calculate_sharpe(self, profits):
        if len(profits) < 2:
            return 0

        returns = profits
        return np.mean(returns) / (np.std(returns) + 1e-9)

    def calculate_drawdown(self, profits):
        equity = np.cumsum(profits)
        peak = np.maximum.accumulate(equity)
        drawdown = peak - equity
        return np.max(drawdown)