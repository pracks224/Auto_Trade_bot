import sys
import os
import time
import pytz # You may need to: pip install pytz
from datetime import datetime
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
MAX_LOSS = 500
CHECK_INTERVAL = 30  
MAGIC_NUMBER = 123456
MAGIC_NUMBER_TRENDING = 666549
last_max_loss_time = 0
COOLDOWN_PERIOD = 180
last_trade_candle_time = None
active_trade_regime = None
buy_zone_armed = None
sell_zone_armed = None

# Connect MT5
if not mt5.initialize():
    logger.error("MT5 initialization failed")
    exit()
else:
    logger.info("PRO Hybrid Bot connected to MT5 successfully")

def fetch_data(symbol, bars=500):
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

def calculate_regime(df, window=15):
    # Get the last 'n' close prices
    y = df['close'].tail(window).values
    x = np.arange(window)
    
    # Perform Linear Regression
    slope, intercept = np.polyfit(x, y, 1)
    
    # Calculate R-squared (How well the line fits the data)
    y_pred = slope * x + intercept
    residuals = y - y_pred
    ss_res = np.sum(residuals**2)
    ss_tot = np.sum((y - np.mean(y))**2)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot != 0 else 0
    # Identify Regime for the logger
    regime = "TRENDING" if r_squared > 0.65 else "RANGE" if r_squared < 0.35 else "TRANSITION"
    
    # The Log Line
    logger.info(f"[REGIME] R2: {r_squared:.3f} | Slope: {slope:.4f} | Mode: {regime}")
    return slope, r_squared
def calculate_adx_robust(df, window=14):
    # 1. True Range
    df['h-l'] = df['high'] - df['low']
    df['h-pc'] = abs(df['high'] - df['close'].shift(1))
    df['l-pc'] = abs(df['low'] - df['close'].shift(1))
    df['tr'] = df[['h-l', 'h-pc', 'l-pc']].max(axis=1)

    # 2. Directional Movement
    df['up'] = df['high'].diff()
    df['down'] = df['low'].shift(1) - df['low']
    
    df['+dm'] = np.where((df['up'] > df['down']) & (df['up'] > 0), df['up'], 0.0)
    df['-dm'] = np.where((df['down'] > df['up']) & (df['down'] > 0), df['down'], 0.0)

    # 3. Use EWM (Wilder's Smoothing) - This is the "Magic" that prevents 0.0
    alpha = 1/window
    tr_smooth = df['tr'].ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100 * (df['+dm'].ewm(alpha=alpha, adjust=False).mean() / tr_smooth)
    minus_di = 100 * (df['-dm'].ewm(alpha=alpha, adjust=False).mean() / tr_smooth)

    # 4. DX calculation with a tiny epsilon to avoid divide-by-zero
    # This ensures that if the market is flat, ADX is 0, but it can WAKE UP later
    dx = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10))
    
    # 5. Final ADX
    df['adx'] = dx.ewm(alpha=alpha, adjust=False).mean()
    
    return df['adx'].iloc[-1]
def hybrid_adx_bollinger(df, symbol):
    global last_trade_candle_time
    global last_max_loss_time
    global active_trade_regime
    global buy_zone_armed
    global sell_zone_armed
    # EMAs
    df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema30'] = df['close'].ewm(span=30, adjust=False).mean()
    df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()

    # Bollinger Bands
    df['bb_mid'] = df['close'].rolling(window=20).mean()
    bb_std = df['close'].rolling(window=20).std()
    df['bb_upper'] = df['bb_mid'] + (bb_std * 2)
    df['bb_lower'] = df['bb_mid'] - (bb_std * 2)
    df['bb_width'] = df['bb_upper'] - df['bb_lower']
    df['bb_width_avg'] = df['bb_width'].rolling(window=20).mean()

    # RSI
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))

    # ADX & ATR
    # --- 1. ROBUST ADX CALCULATION ---
    # Calculate True Range
    df['tr'] = np.maximum(df['high'] - df['low'], 
                np.maximum(abs(df['high'] - df['close'].shift(1)), 
                abs(df['low'] - df['close'].shift(1))))

    # Use a shorter window for the DI to get it to "wake up" faster
    window = 14
    df['atr_smooth'] = df['tr'].rolling(window=window).mean() 

    # --- 2. THE LOGGING FIX ---
    # Instead of just taking iloc[-1], let's ensure we have a fallback
    curr_adx = calculate_adx_robust(df,window)

    # If it's still NaN, the math hasn't reached the end of the 500 rows yet
    if np.isnan(curr_adx):
        curr_adx = 0.0

    # --- 2. EXTRACT LATEST VALUES (SETUP DATA) ---
    curr_price = df['close'].iloc[-1]
    prev_price = df['close'].iloc[-2] # Added for "Hook" check
   # curr_adx   = df['adx'].iloc[-1]
    curr_rsi   = df['rsi'].iloc[-1]
    curr_atr   = df['atr_smooth'].iloc[-1]  # This is a float now
    ema9       = df['ema9'].iloc[-1]
    ema30      = df['ema30'].iloc[-1]
    ema200     = df['ema200'].iloc[-1]
    bb_up      = df['bb_upper'].iloc[-1]
    bb_low     = df['bb_lower'].iloc[-1]
    bb_mid     = df['bb_mid'].iloc[-1]
   
    current_candle_time = df.index[-1]
    # --- NEW SECTION: TREND WEAKNESS & MANAGEMENT ---
    # We check if a Trend Position is already open before looking for new ones

    open_trend_pos = mt5.positions_get(symbol=symbol, magic=MAGIC_NUMBER_TRENDING)
    #logger.info(f"Filtered Trend Count (Magic {MAGIC_NUMBER_TRENDING}): {len(open_trend_pos)}")
    if open_trend_pos:
        pos = open_trend_pos[0] # You mentioned you only have one trend position
        current_sl = pos.sl
        trail_buffer = 0.3 # Small buffer to avoid 'noise'
        if pos.type == mt5.POSITION_TYPE_SELL:
            # New SL is the EMA9 plus our buffer
            suggested_sl = ema9 + trail_buffer    
            # Only modify if the new SL is LOWER than the current one (Locking profit)
            if suggested_sl < current_sl or current_sl == 0:
                modify_sl(pos.ticket, suggested_sl)

        elif pos.type == mt5.POSITION_TYPE_BUY:
            # New SL is the EMA9 minus our buffer
            suggested_sl = ema9 - trail_buffer
            
            # Only modify if the new SL is HIGHER than the current one
            if suggested_sl > current_sl or current_sl == 0:
                modify_sl(pos.ticket, suggested_sl)
        
        # WEAKNESS LOGIC
        # 1. ADX Drop: Trend is turning into a Range
        adx_weak = curr_adx < 22 
        # 2. Structural Break: Price crossed the EMA9 "Ceiling/Floor"
        structure_break = False
        if pos.type == mt5.POSITION_TYPE_SELL:
            structure_break = curr_price > (ema9 + 0.3)
        else:
            structure_break = curr_price < (ema9 - 0.3)
            
        if active_trade_regime == "TREND" and adx_weak or structure_break:
            reason = "WEAKNESS: ADX Low" if adx_weak else "WEAKNESS: EMA9 Break"
            logger.info(f"[EXIT] Closing Trend Position | {reason} | ADX: {curr_adx:.1f}")
            mt5.Close(symbol, ticket=pos.ticket)
            last_trade_candle_time = current_candle_time
        logger.info(f"Position active. Skipping entry logic for this tick.")
        return None # Exit function, we are done for this tick

    # --- 2. ENTRY LOGIC GATE ---

    if current_candle_time == last_trade_candle_time:
        return None
    
    ema_gap      = abs(ema9 - ema200)
    prev_ema_gap = abs(df['ema9'].iloc[-2] - df['ema200'].iloc[-2])

    # Booleans for logic
    is_expanded  = df['bb_width'].iloc[-1] > (df['bb_width_avg'].iloc[-1] * 1.2)
    is_trending  = curr_adx > 24
    gap_widening = ema_gap > prev_ema_gap
    stretch = abs(curr_price - ema9)
    # Use 3x ATR as the "Extreme" marker for Gold
    is_overstretched = stretch > (curr_atr * 2.0)
    candle_body = abs(last['close'] - last['open'])

    # --- 3. REASONING & LOGGING ---
    mode = "TREND" if (is_expanded and is_trending) else "RANGE"
    reason = "No setup"
    active_trade_regime = mode
    if mode == "TREND":
        if not gap_widening:
            reason = f"Gap not widening ({ema_gap:.2f} <= {prev_ema_gap:.2f})"
        elif ema9 > ema200:
            reason = f"Uptrend: No pullback Yet {ema9:.2f}"
            if curr_price <= ema9: reason = "BUY SIGNAL (Trend Pullback)"
        else:
            reason = f"Downtrend: Wait for pullback to {ema9:.2f}"
            if curr_price >= ema9: reason = "SELL SIGNAL (Trend Pullback)"
    else:
        if curr_rsi >= 40 and curr_rsi <= 60:
            reason = f"RSI Neutral ({curr_rsi:.1f})"
        elif curr_price > bb_low and curr_price < bb_up:
            reason = "Price inside BB bands"
        elif curr_price <= bb_low and curr_rsi < 35:
            reason = "BUY SIGNAL (Range Bottom)"
        elif curr_price >= bb_up and curr_rsi > 65:
            reason = "SELL SIGNAL (Range Top)"

    logger.info(f"[{mode}] ADX: {curr_adx:.1f} | Price: {curr_price:.2f} | BB_UP: {bb_up:.2f} | BB_LOW: {bb_low:.2f} | {reason}")

    # --- Outside the loop or in a persistent state object ---
    last_5_candles = df.iloc[-6:-1]
    
    # Calculate the High and Low of that 5-minute window
    five_min_high = last_5_candles['high'].max()
    five_min_low = last_5_candles['low'].min()
    
    # 2. Setup the Trigger with a Buffer
    trigger_buy = five_min_high + 0.10
    trigger_sell = five_min_low - 0.10

    # --- Inside 4. EXECUTION LOGIC ---
    isAnyCoolDown = time.time() - last_max_loss_time > COOLDOWN_PERIOD

    if isAnyCoolDown and mode == "TREND" and gap_widening:
        # 2. TRIGGER PHASE (Wait for price action to confirm)
        if curr_price > trigger_buy:
            sl_price = five_min_low - 0.05
            tp = curr_price+2
            last_max_loss_time = time.time() # Start 5-min cooldown
            logger.info(f"5-MIN BREAKOUT BUY: Price {curr_price} > High {five_min_high}")
            return execute_scalp(symbol, "BUY", 0.57, curr_price, sl, tp, MAGIC_NUMBER_TRENDING)

        elif curr_price < trigger_sell:
            sl_price = five_min_high + 0.05
            tp=curr_price-2
            last_max_loss_time = time.time()
            logger.info(f"5-MIN BREAKOUT SELL: Price {curr_price} < Low {five_min_low}")
            return execute_scalp(symbol, "SELL", 0.57, curr_price, sl, tp, MAGIC_NUMBER_TRENDING)

        # 3. INVALIDATION (Optional)
        # If the price moves too far away from the EMA9, cancel the pending signal
        if pending_signal and abs(curr_price - ema9) > (curr_atr * 5):
            logger.info("Signal invalidated: Price moved too far from EMA9")
            pending_signal = None

    elif mode == "RANGE":
        # --- THE DIAGNOSTIC LOGGER ---
        dist_to_low = curr_price - bb_low
        dist_to_up = bb_up - curr_price
        # 1. SETUP PARAMETERS
        range_entry_buffer = 1.5
        # 1. Check if price is within the 'Active Zone'
        is_in_buy_zone = curr_price <= (bb_low + range_entry_buffer)
        is_in_sell_zone = curr_price >= (bb_up - range_entry_buffer)

        # 2. The Confirmation (The Hook)
        is_turning_up = curr_price > prev_price
        is_turning_down = curr_price < prev_price

        logger.info(f"--- [RANGE CHECK] Price: {curr_price:.2f} | RSI: {curr_rsi:.1f} | "
                    f"Gap_Low: {dist_to_low:.2f} (Target: <{range_entry_buffer}) | "
                    f"Hook: {'UP' if is_turning_up else 'DOWN' if is_turning_down else 'FLAT'} ---")
        if dist_to_low < 1.0: # Tight touch to the floor
            buy_zone_armed = True
            logger.info("BUY ZONE ARMED: Price hit floor. Waiting for break...")

        if dist_to_up < 1.0: # Tight touch to the ceiling
            sell_zone_armed = True
            logger.info("SELL ZONE ARMED: Price hit ceiling. Waiting for break...")
        # BUY LOGIC
        if buy_zone_armed and is_turning_up and curr_rsi < 45:
            reason = "RANGE BUY: Hook confirmed in Zone"
            last_max_loss_time = time.time()
            return execute_scalp(symbol, "BUY", 0.35, curr_price, bb_low - (curr_atr), bb_mid, MAGIC_NUMBER)

        # SELL LOGIC
        elif sell_zone_armed and is_turning_down and curr_rsi > 65:
            reason = "RANGE SELL: Hook confirmed in Zone"
            last_max_loss_time = time.time()
            return execute_scalp(symbol, "SELL", 0.35, curr_price, bb_up + (curr_atr), bb_mid, MAGIC_NUMBER)     
    return None



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
    risk_divider = 3 
    
    # Calculate the raw lot based on volatility
    raw_lot = (base_lot * 20 / max(atr_value, 1.0)) / risk_divider
    
    # 1. Round to 2 decimals for MT5
    lot = round(raw_lot, 2)
    
    # 2. APPLY THE 1.0 LOT CEILING (The most important part)
    if lot > 1.0:
        lot = 1.0
        
    # 3. Ensure it's at least the broker minimum (0.01)
    return max(0.01, lot)

def modify_sl(ticket, sl, tp=0.0):
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "sl": round(sl, 2),
        "tp": round(tp, 2),
    }
    return mt5.order_send(request)

def set_break_even(symbol, atr_value, multiplier=1.2,magic_num=MAGIC_NUMBER):
    positions = mt5.positions_get(symbol=symbol, magic=magic_num)
    if not positions: return
    
    for pos in positions:
        tick = mt5.symbol_info_tick(symbol)
        if pos.type == mt5.POSITION_TYPE_BUY:
            if (tick.bid - pos.price_open) >= (atr_value * multiplier) and pos.sl < pos.price_open:
                modify_sl(pos.ticket, pos.price_open + 0.10, pos.tp)
        elif pos.type == mt5.POSITION_TYPE_SELL:
            if (pos.price_open - tick.ask) >= (atr_value * multiplier) and (pos.sl > pos.price_open or pos.sl == 0):
                modify_sl(pos.ticket, pos.price_open - 0.10, pos.tp)

def update_trailing_stop(symbol, atr_value, trail_multiplier=1.5,magic_num=MAGIC_NUMBER):
    positions = mt5.positions_get(symbol=symbol, magic=magic_num)
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


def get_m15_structure(symbol, lookback=5):
    # Fetch last 5 candles from M15
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, lookback)
    if rates is None:
        return None, None
    
    # Get highest high and lowest low of the M15 range
    highs = [x['high'] for x in rates]
    lows = [x['low'] for x in rates]
    
    return max(highs), min(lows)

def place_trade(symbol, side, lot, price, atr_value, tp_multiplier=2.5):
    """
    Executes a trade using M15 structural levels for SL and ATR for TP.
    """
    # 1. Fetch the actual M15 High and Low coordinates
    m15_high, m15_low = get_m15_structure(symbol, lookback=4)
    
    # Fallback safety: if M15 data fails, use a wide ATR stop
    if m15_high is None or m15_low is None:
        logger.warning("M15 structure not found. Falling back to ATR stops.")
        m15_high = price + (atr_value * 3)
        m15_low = price - (atr_value * 3)

    # 2. Define SL and TP based on Direction
    if side == "BUY":
        # SL goes BELOW the structural low
        sl = m15_low - 1.5 
        # Safety: SL must be below entry price
        if sl >= price:
            sl = price - (atr_value * 2)
            
        tp = price + (atr_value * tp_multiplier)
        type_mt5 = mt5.ORDER_TYPE_BUY
    else:
        # SELL: SL goes ABOVE the structural high (This fixes the Invalid Stop error)
        sl = m15_high + 1.5 
        # Safety: SL must be above entry price
        if sl <= price:
            sl = price + (atr_value * 2)
            
        tp = price - (atr_value * tp_multiplier)
        type_mt5 = mt5.ORDER_TYPE_SELL

    # 3. Build the Request
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lot),
        "type": type_mt5,
        "price": float(price),
        "sl": float(round(sl, 2)), # Rounds to 2 decimals for Gold
        "tp": float(round(tp, 2)),
        "deviation": 10,
        "magic": MAGIC_NUMBER,
        "comment": "PRO Hybrid Bot",
        "type_filling": mt5.ORDER_FILLING_FOK
    }

    # 4. Send Order
    result = mt5.order_send(request)
    
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        # Detailed error logging helps debug "Invalid Stops"
        logger.error(f" Trade failed: {result.comment} (SL: {sl}, TP: {tp}, Price: {price})")
        return False
        
    logger.info(f" {symbol} {side} {lot} executed at {price} | SL: {sl} | TP: {tp}")
    send_telegram(f" {side} {lot} {symbol} at {price}\nSL: {sl}\nTP: {tp}")
    return True

def close_all_positions(symbol,magic_num=MAGIC_NUMBER):
    positions = mt5.positions_get(symbol=symbol, magic=magic_num)
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

def rubber_band_strategy(df, symbol):
    last = df.iloc[-1]
    atr_v = last['atr']
    
    # 1. Trend Direction
    is_uptrend = last['close'] > last['ema200']
    is_downtrend = last['close'] < last['ema200']

    # 2. Buy Trigger: Uptrend + Pullback + RSI Oversold
    if is_uptrend and last['rsi'] < 32 and last['close'] < last['ema9']:
        # Ensure we have a "Lower Wick" (Hammer-ish)
        candle_bottom_wick = min(last['open'], last['close']) - last['low']
        if candle_bottom_wick > (atr_v * 0.2):
            tp = last['close'] + 1.2 # Tight $1.20 profit
            sl = last['close'] - (atr_v * 1.5) # ATR based safety
            return execute_scalp(symbol, "BUY", 0.5, last['close'], sl, tp)

    # 3. Sell Trigger: Downtrend + Pop + RSI Overbought
    elif is_downtrend and last['rsi'] > 68 and last['close'] > last['ema9']:
        # Ensure we have an "Upper Wick" (Shooting Star-ish)
        candle_top_wick = last['high'] - max(last['open'], last['close'])
        if candle_top_wick > (atr_v * 0.2):
            tp = last['close'] - 1.2
            sl = last['close'] + (atr_v * 1.5)
            return execute_scalp(symbol, "SELL", 0.5, last['close'], sl, tp)
            
    return False
def check_big_candle_momentum(df, symbol, lot_size=1.0, tp_pips=10):
    """
    Refined Momentum: Only enters 'Big Candles' if RSI is NOT exhausted
    and price is aligned with the EMA 200 trend.
    """
    last = df.iloc[-1]
    
    # 1. Core Data Points
    candle_body = abs(last['close'] - last['open'])
    atr_value = last['atr']
    rsi_value = last['rsi']
    ema_200 = last['ema200']
    current_price = last['close']
    
    # 2. Basic Volatility Filter (Keep your existing ATR range)
    if not (2.50 < atr_value < 4.50):
        return False

    # 3. Direction & Trend Logic
    is_bullish = last['close'] > last['open']
    
    # NEW: Trend Alignment (Only Buy above EMA200, Sell below)
    trend_ok = (is_bullish and current_price > ema_200) or (not is_bullish and current_price < ema_200)
    
    # NEW: Exhaustion Filter (The RSI "Cool-Off" Rule)
    # Don't BUY if RSI > 70 (Overbought), Don't SELL if RSI < 30 (Oversold)
    rsi_ok = (is_bullish and rsi_value < 70) or (not is_bullish and rsi_value > 30)

    # 4. Final Execution Logic
    if candle_body > atr_value and trend_ok and rsi_ok:
        side = "BUY" if is_bullish else "SELL"
        
        # Calculate SL (Using your buffer logic)
        sl = (last['low'] - 0.10) if is_bullish else (last['high'] + 0.10)
        
        # Calculate TP (I increased tp_pips slightly to 10 pips / 1.0 point for Gold)
        tp_dist = tp_pips * 0.10 
        tp = (current_price + tp_dist) if is_bullish else (current_price - tp_dist)
        
        logger.info(f" MOMENTUM EXECUTED: {side} @ {current_price} | RSI: {rsi_value:.1f} | Body: {candle_body:.2f}")
        return execute_scalp(symbol, side, lot_size, current_price, sl, tp)
    
    # Log why we skipped (for debugging)
    if candle_body > atr_value:
        logger.warning(f" MOMENTUM SKIPPED: RSI ({rsi_value:.1f}) or Trend Alignment issues.")
        
    return False

def execute_scalp(symbol, side, lot, price, sl, tp,magic=MAGIC_NUMBER):
    """Internal execution for the scalper logic"""
    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lot),
        "type": order_type,
        "price": float(price),
        "sl": float(round(sl, 2)),
        "tp": float(round(tp, 2)),
        "magic": magic, # Unique ID for scalp trades
        "comment": "BigCandle_Scalp",
        "type_filling": mt5.ORDER_FILLING_FOK,
        "deviation": 3 # Tight deviation for fast moves
    }
    
    result = mt5.order_send(request)
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        logger.info(f" SCALP SUCCESS: {side} {lot} @ {price} | TP: {tp}")
        send_telegram(f" SCALP {side} {lot} {symbol} at {price}\nSL: {sl}\nTP: {tp}")
        return True
    else:
        logger.error(f" SCALP FAILED: {result.comment}")
        return False

def is_trading_allowed():
    kwt_tz = pytz.timezone('Asia/Kuwait')
    now_kwt = datetime.now(kwt_tz)
    current_hour_min = now_kwt.hour * 100 + now_kwt.minute # e.g., 4:13 becomes 413
   # logger.info(f"DEBUG: Current Kuwait Time is {now_kwt}")
   # logger.info(f"DEBUG: Current  Hour Min Kuwait Time is {current_hour_min}")

    # 2330 (11:30 PM) to 0300 (3:00 AM)
    if current_hour_min >= 2330 or current_hour_min <= 300:
        return False # Sleep
    
    return True # Trade

# --- GLOBAL THRESHOLDS ---
TREND_GAP_MIN = 15.0       # $15 difference between EMA9 and EMA200
REDUCED_LOT_FACTOR = 0.5   # Risk 50% less on breakouts
QUICK_TP_MULT = 1          # Exit faster on breakouts
# --- MAIN LOOP ---
while True:
    try:
        for sym in SYMBOLS:
            # 1. CHECK TIME FIRST
            if not is_trading_allowed():
                # Optional: Close open positions if you don't want to hold overnight
                # close_all_positions(symbol)           
                logger.info("Blackout Zone (11:30 PM - 3:00 AM). Bot is sleeping...")
                time.sleep(1800) # Check again in 1 minute
                continue
            # 1. Request 500 candles (Solves the Warm-up/NaN issue)
            rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M1, 0, 500)
            if rates is None or len(rates) == 0:
                logger.error(f"MT5 ERROR: Could not fetch rates for {sym}. Check Symbol name!")
                continue
            df = fetch_data(sym)
            if df is None or df.empty:
                logger.error(f"FETCH ERROR: Dataframe for {sym} is empty.")
                continue
            if df.empty: continue
            
            df = calculate_indicators(df)
            last = df.iloc[-1]   # Current Candle
            prev = df.iloc[-2]   # Previous Candle
            atr_v = last['atr']
            ema_gap = abs(last['ema200'] - last['ema9'])

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
                logger.info("Cooldown After WIN or LOSS... waiting.")
                continue
            hybrid_adx_bollinger(df,sym)
    except Exception as e:
        logger.error(f"Loop Error: {e}")
    
    time.sleep(CHECK_INTERVAL)