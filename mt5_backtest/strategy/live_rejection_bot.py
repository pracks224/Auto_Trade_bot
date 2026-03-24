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
last_max_loss_time = 0
COOLDOWN_PERIOD = 900

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
    """Calculates lot size based on ATR but never exceeds 1.00 lot."""
    base_lot = 0.5
    # A risk divider of 2.0 or 3.0 will make the bot more conservative
    risk_divider = 2.5 
    
    # Calculate the raw lot based on volatility
    raw_lot = (base_lot * 20 / max(atr_value, 1.0)) / risk_divider
    
    # 1. Round to 2 decimals for MT5
    lot = round(raw_lot, 2)
    
    # 2. APPLY THE 1.0 LOT CEILING (The most important part)
    if lot > 1.0:
        lot = 1.0
        
    # 3. Ensure it's at least the broker minimum (0.01)
    return max(0.01, lot)

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

    symbol_info = mt5.symbol_info(symbol)
    digits = symbol_info.digits
    # Gold (XAUUSD) often needs a wider trail than 1.5 ATR due to volatility
    trail_dist = atr_value * trail_multiplier

    for pos in positions:
        tick = mt5.symbol_info_tick(symbol)
        if not tick: continue
        
        new_sl = 0.0
        current_sl = pos.sl

        # BUY POSITION LOGIC
        if pos.type == mt5.POSITION_TYPE_BUY:
            target_sl = tick.bid - trail_dist
            # Move UP if target is higher than current SL AND current price is moving away
            if target_sl > current_sl + 0.05: # Minimal 5-cent move to avoid spam
                new_sl = target_sl
                
        # SELL POSITION LOGIC
        elif pos.type == mt5.POSITION_TYPE_SELL:
            target_sl = tick.ask + trail_dist
            # If current_sl is 0 (no SL) or the new target is LOWER than current SL
            if current_sl == 0 or target_sl < current_sl - 0.05:
                new_sl = target_sl

        # Only send request if we have a valid update
        if new_sl > 0:
            # Final Safety: Ensure we don't move SL into a worse position than current price
            # (Wait for price to move at least 1 ATR before trailing begins)
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": symbol,
                "position": pos.ticket,
                "sl": float(round(new_sl, digits)),
                "tp": float(pos.tp),
                "magic": MAGIC_NUMBER
            }
            
            result = mt5.order_send(request)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(f" TRAIL MOVED: {symbol} {pos.ticket} | New SL: {new_sl:.2f}")
            else:
                # This will tell you if you are too close to the price (Code 10016)
                logger.warning(f" TRAIL REJECTED: {result.comment} (Code: {result.retcode})")

def place_trade(symbol, side, lot, price, atr_value, tp_multiplier=2.5):
    """
    Executes a trade with a dynamic SL and a customizable TP multiplier.
    """
    # Standard 1.5x ATR Stop Loss
    m15_high, m15_low = get_m15_structure("XAUUSD_", lookback=4)
    #sl_dist = m15_low-2.0
    tp_dist = atr_value * tp_multiplier
    
    if side == "BUY":
        sl = m15_low - 2.0
        tp = price + tp_dist
        type_mt5 = mt5.ORDER_TYPE_BUY
    else:
        sl = m15_low + 2.0
        tp = price - tp_dist
        type_mt5 = mt5.ORDER_TYPE_SELL

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lot),
        "type": type_mt5,
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
    logger.info(f"{symbol} {side} {lot} lot executed at {price}")
    send_telegram(f"{side} {lot} {symbol} at {price}")
    return True
def get_m15_structure(symbol, lookback=5):
    # Fetch last 5 candles from M15
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, lookback)
    if rates is None:
        return None, None
    
    # Get highest high and lowest low of the M15 range
    highs = [x['high'] for x in rates]
    lows = [x['low'] for x in rates]
    
    return max(highs), min(lows)

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
# --- GLOBAL THRESHOLDS ---
TREND_GAP_MIN = 15.0       # $15 difference between EMA9 and EMA200
REDUCED_LOT_FACTOR = 0.5   # Risk 50% less on breakouts
QUICK_TP_MULT = 1          # Exit faster on breakouts
# --- MAIN LOOP ---
while True:
    try:
        for sym in SYMBOLS:
            df = fetch_data(sym)
            if df.empty: continue
            
            df = calculate_indicators(df)
            last = df.iloc[-1]   # Current Candle
            prev = df.iloc[-2]   # Previous Candle
            atr_v = last['atr']
            ema_gap = abs(last['ema200'] - last['ema9'])

            # Check if we are already in a trade
            current_pos = mt5.positions_get(symbol=sym, magic=MAGIC_NUMBER)

            # 1. PnL Monitor
            pnl = get_total_floating_pnl(sym)
            logger.info(
                f"[{sym}] Price: {last['close']:.2f} | "
                f"RSI: {last['rsi']:.1f} | "
                f"ATR: {atr_v:.2f} | "
                f"EMA9/200: {last['ema9']:.1f}/{last['ema200']:.1f} | "
                f"PnL: ${pnl:.2f}"
            )

            if pnl <= -MAX_LOSS:
                logger.error(f"!!! MAX LOSS HIT (${pnl:.2f}) !!! Closing all.")
                close_all_positions(sym)
                last_max_loss_time = time.time()
                continue
            # At the start of your Entry Logic:
            if time.time() - last_max_loss_time < COOLDOWN_PERIOD:
                # Optional: logger.info("In Max Loss Cooldown... waiting.")
                continue

            # 2. Trade Management
            set_break_even(sym, atr_v)
            //update_trailing_stop(sym, atr_v)

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
                # --- SCENARIO B: INTENSE TREND BREAKOUT (THE NEW ADDITION) ---
                # Only triggers if Scenario A hasn't happened yet
        
                # BEARISH BREAKOUT: EMA Gap is huge + we broke the previous Low
                elif last['ema9'] < last['ema200'] and ema_gap > TREND_GAP_MIN:
                    if last['close'] < prev['low']:
                        small_lot = 0.1
                        logger.info(f"INTENSE BEARISH: Gap {ema_gap:.2f} | Breaking Low {prev['low']}")
                        place_trade(sym, "SELL", small_lot, last['close'], atr_v, tp_multiplier=QUICK_TP_MULT)
                # BULLISH BREAKOUT: EMA Gap is huge + we broke the previous High
                elif last['ema9'] > last['ema200'] and ema_gap > TREND_GAP_MIN:
                    if last['close'] > prev['high']:
                        small_lot = 0.1
                        logger.info(f" INTENSE BULLISH: Gap {ema_gap:.2f} | Breaking High {prev['high']}")
                        place_trade(sym, "BUY", small_lot, last['close'], atr_v, tp_multiplier=QUICK_TP_MULT)

    except Exception as e:
        logger.error(f"Loop Error: {e}")
    
    time.sleep(CHECK_INTERVAL)