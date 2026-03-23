# strategy/ema_rsi_atr_strategy.py
from utils.indicators import ema, rsi
import pandas as pd

class EMARSIATRStrategy:

    def __init__(self, data):
        self.data = data

    def prepare(self):
        df = self.data.copy()

        df['ema9'] = ema(df['close'], 9)
        df['ema200'] = ema(df['close'], 200)
        df['rsi'] = rsi(df['close'], 14)
        df['atr'] = (df['high'] - df['low']).rolling(14).mean()

        return df