# config.py

SYMBOL = "XAUUSD_"
TIMEFRAME = 1  # 1-minute MT5 timeframe
LOT_BASE = 0.01
RISK_PERCENT = 1  # % of account per trade
ATR_MULT_SL = 2
ATR_MULT_TP = 4
TRAILING_ATR = 1.5  # ATR multiplier for trailing stop

TELEGRAM_BOT_TOKEN = "####"
TELEGRAM_CHAT_ID = "#####"

LOG_FILE = "gold_bot.log"

RISK_PERCENT = 1.0       # risk 1% per trade
LOT_BASE = 1             # minimum lot
LOT_MAX = 15             # maximum lot
ATR_MULT_SL = 1.5        # SL multiplier
ATR_MULT_TP = 3.0        # TP multiplier
TRAILING_ATR = 1.0       # trailing stop moves 1 ATR behind