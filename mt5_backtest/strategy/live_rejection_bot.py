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
last_trade_time = 0
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
    global last_trade_time
    global active_trade_regime
    global buy_zone_armed
    global sell_zone_armed
    # EMAs
    df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema13'] = df['close'].ewm(span=13, adjust=False).mean()
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
    ema13      = df['ema13'].iloc[-1]
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
        pos = open_trend_pos[0]
        logger.info(f"Checking SL for Ticket: {pos.ticket} | Magic: {pos.magic} | Expected Trend Magic: {MAGIC_NUMBER_TRENDING}")
        current_sl = pos.sl
        current_tp = pos.tp
        trail_buffer = curr_atr * 2.00
        if pos.type == mt5.POSITION_TYPE_SELL:
            # New SL is the EMA9 plus our buffer
            suggested_sl = ema30 + trail_buffer   
            logger.info(f"{current_sl} FOR SELL NEW SL | {suggested_sl}") 
            # Only modify if the new SL is LOWER than the current one (Locking profit)
            if suggested_sl < current_sl or current_sl == 0:
                modify_sl(pos.ticket, suggested_sl,current_tp)

        elif pos.type == mt5.POSITION_TYPE_BUY:
            # New SL is the EMA9 minus our buffer
            suggested_sl = ema30 - trail_buffer
            logger.info(f"{current_sl} FOR BUY NEW SL | {suggested_sl}")
            # Only modify if the new SL is HIGHER than the current one
            if suggested_sl > current_sl or current_sl == 0:
                modify_sl(pos.ticket, suggested_sl,current_tp)
        
        # WEAKNESS LOGIC
        # 1. ADX Drop: Trend is turning into a Range
        adx_weak = curr_adx < 22 
        last_close = df['close'].iloc[-1]
        last_ema9 = df['ema9'].iloc[-1]
        # 2. Structural Break: Price crossed the EMA9 "Ceiling/Floor"
        structure_break = False
        if pos.type == mt5.POSITION_TYPE_SELL:
            structure_break = last_close > (last_ema9 + 0.3)
        else:
            structure_break = last_close < (last_ema9 - 0.3)
            
        if active_trade_regime == "TREND" and (adx_weak or structure_break):
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
    is_trending  = curr_adx > 25
    gap_widening = ema_gap > prev_ema_gap
    stretch = abs(curr_price - ema9)
    # Use 3x ATR as the "Extreme" marker for Gold
    is_overstretched = stretch > (curr_atr * 1.2)
    # NEW REVERSAL STRATEGY
    open_price = df['open'].iloc[-1]
    
    # 2. Define the 'Trifecta' variables
    is_turning_up = (curr_price > open_price) and (curr_price > df['close'].iloc[-2])
    is_turning_down = (curr_price < open_price) and (curr_price < df['close'].iloc[-2])
    is_extreme_stretch = stretch > (curr_atr * 2.5) 
    
    candle_body = abs(last['close'] - last['open'])
    logger.info(f"is_expanded {is_expanded} > is_trending {is_trending} is_overstretched {is_overstretched}")
    # --- 3. REASONING & LOGGING ---
    mode = "TREND" if (is_expanded or is_trending) else "RANGE"
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
    # Calculate the 'Stretch' or 'Gap'
    price_ema_gap = abs(curr_price - ema9)
    is_too_tight = price_ema_gap < (curr_atr * 0.8)
    #gap_widening = False
    # --- Inside 4. EXECUTION LOGIC ---
    logger.info(f" GAP WIDENING {gap_widening} trigger_buy {trigger_buy} trigger_sell {trigger_sell} ")
    # --- TREND MODE EXECUTION ---
    if mode == "TREND":
        buy_zone_armed = False
        sell_zone_armed = False
        # CHECK 1: EXTREME OVERSTRETCH (The Peak Reversal)
        # Why first? Because if we are at a blow-off top, we should NEVER buy a breakout.
        if is_extreme_stretch and curr_rsi > 75 and is_turning_down:
            reason = "COUNTER-TREND SELL: Extreme Overstretch"
            logger.info(f"{mode} - {reason}")
            sl = curr_price + (curr_atr * 2.5)
            tp = curr_price - (curr_atr * 3.0) 
            return execute_scalp(symbol, "SELL", 0.07, curr_price, sl, tp, MAGIC_NUMBER_TRENDING)
        
        # Catching the bottom (The one you missed at RSI 8.4!)
        elif is_extreme_stretch and curr_rsi < 25 and is_turning_up:
            reason = "COUNTER-TREND BUY: RSI Oversold Exhaustion"
            logger.info(f"{mode} - {reason}")
            sl = curr_price - (curr_atr * 2.5)
            tp = curr_price + (curr_atr * 3.0)
            return execute_scalp(symbol, "BUY", 0.07, curr_price, sl, tp, MAGIC_NUMBER_TRENDING)
   

        # CHECK 3: 5-MIN BREAKOUT (The Momentum Move)
        # Only if the gap is widening and we aren't overstretched yet.
        if gap_widening:
            if curr_price > trigger_buy and not is_overstretched:
                sl_price = five_min_low - 0.1
                tp = curr_price + 8.0
                return execute_scalp(symbol, "BUY", 0.08, curr_price, sl_price, tp, MAGIC_NUMBER_TRENDING)

            elif curr_price < trigger_sell and not is_overstretched:
                sl_price = five_min_high + 0.1
                tp = curr_price - 8.0
                return execute_scalp(symbol, "SELL", 0.08, curr_price, sl_price, tp, MAGIC_NUMBER_TRENDING)
                
            elif is_overstretched:
                logger.warning(f"BREAKOUT IGNORED: Price too far from EMA9 ({stretch:.2f})")

        # --- END TREND MODE --- 
    elif mode == "RANGE":
        # --- THE DIAGNOSTIC LOGGER ---
        dist_to_low = curr_price - bb_low
        dist_to_up = bb_up - curr_price
        # 1. SETUP PARAMETERS
        range_entry_buffer = 1.75
        # 1. Check if price is within the 'Active Zone'
        is_in_buy_zone = curr_price <= (bb_low + range_entry_buffer)
        is_in_sell_zone = curr_price >= (bb_up - range_entry_buffer)

        # 2. The Confirmation (The Hook)
        hook_buffer = curr_atr * 0.1 # Dynamic buffer
        is_turning_up = curr_price > (prev_price+ hook_buffer)
        is_turning_down = curr_price < (prev_price-hook_buffer)
        confirmed_hook_down = is_turning_down and (curr_price < bb_up)
        confirmed_hook_up = is_turning_up and (curr_price > bb_low)
        
        
        logger.info(f"--- [RANGE CHECK] Price: {curr_price:.2f} | RSI: {curr_rsi:.1f} | "
                    f"Gap_Low: {dist_to_low:.2f} (Target: <{range_entry_buffer}) | "
                    f"Hook: {'UP' if is_turning_up else 'DOWN' if is_turning_down else 'FLAT'} ---")
       
        if dist_to_low < 1.0 and not is_too_tight: # Tight touch to the floor
            buy_zone_armed = True
            logger.info("BUY ZONE ARMED: Price hit floor. Waiting for break...")

        if dist_to_up < 1.0 and not is_too_tight: # Tight touch to the ceiling
            sell_zone_armed = True
            logger.info("SELL ZONE ARMED: Price hit ceiling. Waiting for break...")
        if dist_to_low > 5.0: buy_zone_armed = False
        if dist_to_up > 5.0: sell_zone_armed = False
        # BUY LOGIC
        if buy_zone_armed and confirmed_hook_up and (28 < curr_rsi < 45):
            reason = "RANGE BUY: Hook confirmed in Zone"
            last_trade_time = time.time()
            buy_zone_armed = False
            logger.info(f"RANGE BUY REASON {reason}")
            return execute_scalp(symbol, "BUY", 0.09, curr_price, bb_low - (curr_atr*1.2), bb_mid, MAGIC_NUMBER)

        # SELL LOGIC
        elif sell_zone_armed and confirmed_hook_down and curr_rsi > 65:
            reason = "RANGE SELL: Hook confirmed in Zone"
            last_trade_time = time.time()
            sell_zone_armed = False
            logger.info(f"RANGE SELL: REASON {reason}")
            return execute_scalp(symbol, "SELL", 0.09, curr_price, bb_up + (curr_atr*1.2), bb_mid, MAGIC_NUMBER)     
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

def modify_sl(ticket, sl, tp=0.0):
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "sl": round(sl, 2),
        "tp": round(tp, 2),
    }
    return mt5.order_send(request)

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
        "magic": magic,
        "comment": "BigCandle_Scalp",
        "type_filling": mt5.ORDER_FILLING_FOK,
        "deviation": 3
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
            # Calculate time since last activity
            time_since_last_trade = time.time() - last_trade_time

            # 1. Cooldown Gatekeeper
            if time_since_last_trade < COOLDOWN_PERIOD:
                remaining = int(COOLDOWN_PERIOD - time_since_last_trade)
                # Log every 60 seconds so the console isn't spammed
                if remaining % 60 == 0:
                    logger.info(f"COOLDOWN ACTIVE: {remaining}s remaining before next scan.")
                continue # Skip the rest of the loop and start over
            hybrid_adx_bollinger(df,sym)
    except Exception as e:
        logger.error(f"Loop Error: {e}")
    
    time.sleep(CHECK_INTERVAL)