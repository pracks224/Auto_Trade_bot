# pro_hybrid_bot.py
import sys
import os
import time
import pandas as pd
import numpy as np
from decimal import Decimal
import MetaTrader5 as mt5

# Add project root for utils imports
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.append(project_root)

from utils.indicators import atr, rsi, bollinger_bands
from utils.logger import get_logger
from utils.telegram_alerts import send_telegram

logger = get_logger()
SYMBOLS = ["XAUUSD_"]
TIMEFRAME = mt5.TIMEFRAME_M1
MAX_LOSS = 350
CHECK_INTERVAL = 60  # seconds

# Connect MT5
if not mt5.initialize():
    logger.error("MT5 initialization failed")
    exit()
else:
    logger.info("PRO Hybrid Bot connected to MT5 successfully")

# Store positions and pending orders
positions = {sym: [] for sym in SYMBOLS}
pending_orders = {sym: [] for sym in SYMBOLS}

def fetch_data(symbol, bars=200):
    rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, bars)
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df.set_index('time', inplace=True)
    return df

def calculate_indicators(df):
    df['atr'] = atr(df, 14)
    df['rsi'] = rsi(df['close'], 14)
    df['bb_upper'], df['bb_lower'] = bollinger_bands(df['close'], 20, 2)
    df['ema9'] = df['close'].ewm(span=9).mean()
    df['ema200'] = df['close'].ewm(span=200).mean()
    return df

def collective_floating_pnl(symbol):
    total = 0
    tick = mt5.symbol_info_tick(symbol)
    for pos in positions[symbol]:
        if pos['type'] == 'BUY':
            total += (tick.bid - pos['price']) * pos['lot'] * 100
        else:
            total += (pos['price'] - tick.ask) * pos['lot'] * 100
    return total

def dynamic_lot(symbol, atr_value):
    base_lot = 0.5
    return round(base_lot * 20 / max(atr_value, 1), 2)  # smaller lot for higher ATR

def place_trade(symbol, direction, lot, price):
    order_type = mt5.ORDER_TYPE_BUY if direction == 'BUY' else mt5.ORDER_TYPE_SELL
    sl = price - 2 * atr_value if direction == 'BUY' else price + 2 * atr_value
    tp = price + 4 * atr_value if direction == 'BUY' else price - 4 * atr_value

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 10,
        "magic": 123456,
        "comment": "PRO Hybrid Bot",
        "type_filling": mt5.ORDER_FILLING_FOK
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.error(f"{symbol} Trade failed: {result}")
        return False

    logger.info(f"{symbol} {direction} {lot} lot executed at {price}")
    send_telegram(f"{symbol} {direction} {lot} lot executed at {price}")
    positions[symbol].append({"type": direction, "lot": lot, "price": price})
    return True

def check_pending(symbol, tick):
    for po in pending_orders[symbol][:]:
        if po['type'] == "BUYSTOP" and tick.ask >= po['price']:
            logger.info(f"{symbol} BUYSTOP triggered at {po['price']}")
            place_trade(symbol, "BUY", po['lot'], po['price'])
            pending_orders[symbol].remove(po)
        elif po['type'] == "SELLSTOP" and tick.bid <= po['price']:
            logger.info(f"{symbol} SELLSTOP triggered at {po['price']}")
            place_trade(symbol, "SELL", po['lot'], po['price'])
            pending_orders[symbol].remove(po)

# Main Loop
while True:
    for sym in SYMBOLS:
        df = fetch_data(sym)
        df = calculate_indicators(df)
        last = df.iloc[-1]
        prev = df.iloc[-2]

        logger.info(f"{sym} | Price:{last['close']:.2f} EMA9:{last['ema9']:.2f} EMA200:{last['ema200']:.2f} ATR:{last['atr']:.2f} RSI:{last['rsi']:.2f}")

        floating_pnl = collective_floating_pnl(sym)
        logger.info(f"{sym} Floating PnL: {floating_pnl:.2f}")

        if floating_pnl <= -MAX_LOSS:
            logger.info(f"{sym} Max loss reached, closing all positions")
            send_telegram(f"{sym} Max loss reached, closing all positions")
            # close logic here...
            positions[sym].clear()
            pending_orders[sym].clear()
            continue

        atr_value = last['atr']
        lot = dynamic_lot(sym, atr_value)

        # Trend-based Rejection Logic
        # SELL: EMA9 < EMA200, RSI > 65, close near BB_upper
        if last['ema9'] < last['ema200'] and last['rsi'] >= 65 and last['close'] >= last['bb_upper']*0.995:
            place_trade(sym, "SELL", lot, last['close'])

        # BUY: EMA9 > EMA200, RSI < 35, close near BB_lower
        elif last['ema9'] > last['ema200'] and last['rsi'] <= 35 and last['close'] <= last['bb_lower']*1.005:
            place_trade(sym, "BUY", lot, last['close'])

        # Check pending orders
        tick = mt5.symbol_info_tick(sym)
        check_pending(sym, tick)

    time.sleep(CHECK_INTERVAL)