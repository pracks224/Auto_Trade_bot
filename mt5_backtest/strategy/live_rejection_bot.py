import sys
import os
import time
import pandas as pd
import numpy as np
import MetaTrader5 as mt5

# Add project root for utils imports
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.append(project_root)

# Ensure these utils exist in your folder structure
try:
    from utils.indicators import atr, rsi, bollinger_bands
    from utils.logger import get_logger
    from utils.telegram_alerts import send_telegram
except ImportError:
    # Fallback for demonstration if utils are missing
    print("Warning: Utils not found. Ensure utils folder is present.")

logger = get_logger()
SYMBOLS = ["XAUUSD_"]  # Ensure this matches your broker's suffix
TIMEFRAME = mt5.TIMEFRAME_M1
MAX_LOSS = 350
CHECK_INTERVAL = 60  
MAGIC_NUMBER = 123456

# Connect MT5
if not mt5.initialize():
    logger.error("MT5 initialization failed")
    exit()
else:
    logger.info("PRO Hybrid Bot connected to MT5 successfully")

def fetch_data(symbol, bars=200):
    rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, bars)
    if rates is None: return pd.DataFrame()
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df.set_index('time', inplace=True)
    return df

def calculate_indicators(df):
    if df.empty: return df
    df['atr'] = atr(df, 14)
    df['rsi'] = rsi(df['close'], 14)
    df['bb_upper'], df['bb_lower'] = bollinger_bands(df['close'], 20, 2)
    df['ema9'] = df['close'].ewm(span=9).mean()
    df['ema200'] = df['close'].ewm(span=200).mean()
    return df

def get_total_floating_pnl(symbol):
    """Reads live PnL directly from MT5 terminal with safety defaults."""
    positions = mt5.positions_get(symbol=symbol, magic=MAGIC_NUMBER)
    if not positions: 
        return 0.0
        
    total_pnl = 0.0
    for pos in positions:
        # Use getattr(obj, 'attr', default) to prevent 'No attribute' errors
        profit = getattr(pos, 'profit', 0.0)
        swap = getattr(pos, 'swap', 0.0)
        comm = getattr(pos, 'commission', 0.0) 
        
        total_pnl += (profit + swap + comm)
        
    return total_pnl

def dynamic_lot(symbol, atr_value):
    base_lot = 0.5
    # Risk adjustment: higher ATR = lower lot size
    lot = round(base_lot * 20 / max(atr_value, 1.0), 2)
    return max(0.01, lot) # Minimum 0.01

def modify_sl(ticket, sl, tp):
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "sl": round(sl, 2),
        "tp": round(tp, 2),
    }
    return mt5.order_send(request)

def set_break_even(symbol, atr_value, multiplier=1.2):
    positions = mt5.positions_get(symbol=symbol, magic=MAGIC_NUMBER)
    if not positions: return
    
    for pos in positions:
        tick = mt5.symbol_info_tick(symbol)
        if pos.type == mt5.POSITION_TYPE_BUY:
            if (tick.bid - pos.price_open) >= (atr_value * multiplier) and pos.sl < pos.price_open:
                modify_sl(pos.ticket, pos.price_open + 0.10, pos.tp)
        elif pos.type == mt5.POSITION_TYPE_SELL:
            if (pos.price_open - tick.ask) >= (atr_value * multiplier) and (pos.sl > pos.price_open or pos.sl == 0):
                modify_sl(pos.ticket, pos.price_open - 0.10, pos.tp)

def update_trailing_stop(symbol, atr_value, trail_multiplier=1.5):
    positions = mt5.positions_get(symbol=symbol, magic=MAGIC_NUMBER)
    if not positions:
        return

    # Get symbol decimal digits (e.g., 2 for Gold)
    symbol_info = mt5.symbol_info(symbol)
    digits = symbol_info.digits
    trail_dist = atr_value * trail_multiplier

    for pos in positions:
        tick = mt5.symbol_info_tick(symbol)
        new_sl = 0.0

        if pos.type == mt5.POSITION_TYPE_BUY:
            target_sl = tick.bid - trail_dist
            # Only move SL UP
            if target_sl > pos.sl + 0.01: # Small buffer to avoid spamming
                new_sl = target_sl
                
        elif pos.type == mt5.POSITION_TYPE_SELL:
            target_sl = tick.ask + trail_dist
            # Only move SL DOWN (or set it if currently 0)
            if target_sl < pos.sl or pos.sl == 0:
                new_sl = target_sl

        if new_sl > 0:
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": symbol,
                "position": pos.ticket,
                "sl": float(round(new_sl, digits)), # Force float and correct decimals
                "tp": float(pos.tp),               # Always include current TP
                "magic": MAGIC_NUMBER
            }
            result = mt5.order_send(request)
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                logger.error(f"Trailing SL failed: {result.comment} (Code: {result.retcode})")

def place_trade(symbol, direction, lot, price, atr_value):
    order_type = mt5.ORDER_TYPE_BUY if direction == 'BUY' else mt5.ORDER_TYPE_SELL
    sl = price - (2 * atr_value) if direction == 'BUY' else price + (2 * atr_value)
    tp = price + (4 * atr_value) if direction == 'BUY' else price - (4 * atr_value)

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lot),
        "type": order_type,
        "price": float(price),
        "sl": float(round(sl, 2)),
        "tp": float(round(tp, 2)),
        "deviation": 10,
        "magic": MAGIC_NUMBER,
        "comment": "PRO Hybrid Bot",
        "type_filling": mt5.ORDER_FILLING_FOK
    }

    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.error(f"Trade failed: {result.comment}")
        return False
    logger.info(f"{symbol} {direction} {lot} lot executed at {price}")
    send_telegram(f"{direction} {lot} {symbol} at {price}")
    return True

def close_all_positions(symbol):
    positions = mt5.positions_get(symbol=symbol, magic=MAGIC_NUMBER)
    if not positions: return
    
    for pos in positions:
        tick = mt5.symbol_info_tick(symbol)
        type_close = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price_close = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": pos.volume,
            "type": type_close,
            "position": pos.ticket,
            "price": price_close,
            "magic": MAGIC_NUMBER,
            "type_filling": mt5.ORDER_FILLING_FOK,
        }
        mt5.order_send(request)

# --- MAIN LOOP ---
while True:
    try:
        for sym in SYMBOLS:
            df = fetch_data(sym)
            if df.empty: continue
            
            df = calculate_indicators(df)
            last = df.iloc[-1]
            atr_v = last['atr']

            # 1. PnL Monitor
            pnl = get_total_floating_pnl(sym)
            logger.info(
                f"[{sym}] Price: {last['close']:.2f} | "
                f"RSI: {last['rsi']:.1f} | "
                f"EMA9/200: {last['ema9']:.1f}/{last['ema200']:.1f} | "
                f"PnL: ${pnl:.2f}"
            )

            if pnl <= -MAX_LOSS:
                logger.error(f"!!! MAX LOSS HIT (${pnl:.2f}) !!! Closing all.")
                close_all_positions(sym)
                time.sleep(300)
                continue

            # 2. Trade Management
            set_break_even(sym, atr_v)
            update_trailing_stop(sym, atr_v)

            # 3. New Entry Logic
            open_pos = mt5.positions_get(symbol=sym, magic=MAGIC_NUMBER)
            if not open_pos:
                lot_size = dynamic_lot(sym, atr_v)
                
                # BUY: Trend up, Oversold, at Support
                if last['ema9'] > last['ema200'] and last['rsi'] <= 35 and last['close'] <= last['bb_lower'] * 1.005:
                    place_trade(sym, "BUY", lot_size, last['close'], atr_v)
                
                # SELL: Trend down, Overbought, at Resistance
                elif last['ema9'] < last['ema200'] and last['rsi'] >= 65 and last['close'] >= last['bb_upper'] * 0.995:
                    place_trade(sym, "SELL", lot_size, last['close'], atr_v)

    except Exception as e:
        logger.error(f"Loop Error: {e}")
    
    time.sleep(CHECK_INTERVAL)