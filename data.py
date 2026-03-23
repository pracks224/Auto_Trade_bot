# data/data_loader.py
import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime

class MT5DataLoader:

    def __init__(self, symbol, timeframe):
        self.symbol = symbol
        self.timeframe = timeframe

    def connect(self):
        if not mt5.initialize():
            raise Exception("MT5 Initialization Failed")

    def fetch_data(self, start, end):
        rates = mt5.copy_rates_range(
            self.symbol,
            self.timeframe,
            start,
            end
        )

        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        return df.set_index('time')