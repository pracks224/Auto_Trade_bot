# strategy/ema_strategy.py
from strategy.base_strategy import BaseStrategy
from utils.indicators import ema
from utils.logger import get_logger

logger = get_logger()

class EMAStrategy(BaseStrategy):

    def generate_signals(self):
        logger.info("Generating signals using EMA strategy")

        df = self.data.copy()

        df['ema_fast'] = ema(df['close'], 9)
        df['ema_slow'] = ema(df['close'], 200)

        df['signal'] = 0
        df.loc[df['ema_fast'] > df['ema_slow'], 'signal'] = 1
        df.loc[df['ema_fast'] < df['ema_slow'], 'signal'] = -1

        logger.info("Signals generated successfully")
        return df