import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import pandas_ta as ta
from datetime import datetime

# --- CONFIGURATION ---
SYMBOL = "XAUUSD_"  # Matches your log symbol
TIMEFRAME = mt5.TIMEFRAME_M1
CANDLES_TO_TEST = 10000  # Approx 7 trading days
REGIME_WINDOW = 15
INIT_BALANCE = 10000
SPREAD_COST = 0.30  # Estimated Gold spread in USD points

def get_data():
    if not mt5.initialize():
        print("MT5 Init Failed")
        return None
    rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME, 0, CANDLES_TO_TEST)
    mt5.shutdown()
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    return df

def apply_strategy_math(df):
    # Standard Indicators
    df['ema9'] = ta.ema(df['close'], length=9)
    df['ema200'] = ta.ema(df['close'], length=200)
    df['rsi'] = ta.rsi(df['close'], length=14)
    df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    
    # Regime Math (Slope & R2)
    slopes, r_squares = [0.0]*len(df), [0.0]*len(df)
    for i in range(REGIME_WINDOW, len(df)):
        y = df['close'].iloc[i-REGIME_WINDOW:i].values
        x = np.arange(REGIME_WINDOW)
        slope, intercept = np.polyfit(x, y, 1)
        y_pred = slope * x + intercept
        r_sq = 1 - (np.sum((y - y_pred)**2) / np.sum((y - np.mean(y))**2))
        slopes[i], r_squares[i] = slope, r_sq
        
    df['slope'], df['r_sq'] = slopes, r_squares
    return df.dropna()

def run_backtest(df):
    balance = INIT_BALANCE
    trades = []
    active_trade = None

    for i in range(len(df)):
        row = df.iloc[i]
        
        # 1. Exit Check (If in a trade)
        if active_trade:
            if active_trade['type'] == 'BUY':
                if row['low'] <= active_trade['sl']:
                    balance -= (active_trade['entry'] - active_trade['sl'] + SPREAD_COST)
                    trades.append('LOSS')
                    active_trade = None
                elif row['high'] >= active_trade['tp']:
                    balance += (active_trade['tp'] - active_trade['entry'] - SPREAD_COST)
                    trades.append('WIN')
                    active_trade = None
            elif active_trade['type'] == 'SELL':
                if row['high'] >= active_trade['sl']:
                    balance -= (active_trade['sl'] - active_trade['entry'] + SPREAD_COST)
                    trades.append('LOSS')
                    active_trade = None
                elif row['low'] <= active_trade['tp']:
                    balance += (active_trade['entry'] - active_trade['tp'] - SPREAD_COST)
                    trades.append('WIN')
                    active_trade = None

        # 2. Entry Logic (If no active trade)
        else:
            # REGIME: TRENDING (Pullback)
            if row['r_sq'] > 0.65:
                if row['slope'] > 0 and row['close'] <= row['ema9']: # Buy Dip
                    active_trade = {'type': 'BUY', 'entry': row['close'], 
                                    'sl': row['close'] - (2.0 * row['atr']), 
                                    'tp': row['close'] + (1.5 * row['atr'])}
            # REGIME: RANGE (Mean Reversion)
            elif row['r_sq'] < 0.35:
                if row['rsi'] > 70: # Sell Spike
                    active_trade = {'type': 'SELL', 'entry': row['close'], 
                                    'sl': row['close'] + (1.2 * row['atr']), 
                                    'tp': row['close'] - (1.0 * row['atr'])}

    return trades, balance

# EXECUTE
raw_data = get_data()
processed_data = apply_strategy_math(raw_data)
results, final_bal = run_backtest(processed_data)

# OUTPUT
win_rate = (results.count('WIN') / len(results) * 100) if results else 0
print(f"--- BACKTEST RESULTS ---")
print(f"Total Trades: {len(results)}")
print(f"Win Rate: {win_rate:.2f}%")
print(f"Final Balance: ${final_bal:.2f}")