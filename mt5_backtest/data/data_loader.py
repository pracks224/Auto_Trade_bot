# data/data_loader.py
import MetaTrader5 as mt5
import pandas as pd
from utils.logger import get_logger

logger = get_logger()

class MT5DataLoader:

    def __init__(self, symbol, timeframe):
        self.symbol = symbol
        self.timeframe = timeframe

    def connect(self):
        logger.info("Connecting to MT5...")
        if not mt5.initialize():
            logger.error("MT5 Initialization Failed")
            raise Exception("MT5 Initialization Failed")
        if not mt5.symbol_select(self.symbol, True):
            logger.error(f"Failed to select symbol {self.symbol}")
            raise Exception("Symbol not available")

        logger.info(f"MT5 Connected & Symbol {self.symbol} selected")
        logger.info("MT5 Connected Successfully")

    def fetch_data(self, bars=5000):
        logger.info(f"Fetching last {bars} bars for {self.symbol}")

        rates = mt5.copy_rates_from_pos(
            self.symbol,
            self.timeframe,
            0,
            bars
        )

        if rates is None or len(rates) == 0:
            logger.error("No data received from MT5")
            raise Exception("Empty data")

        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')

        logger.info(f"Fetched {len(df)} rows")
        return df.set_index('time')
