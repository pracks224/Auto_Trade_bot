import MetaTrader5 as mt5
from utils.logger import get_logger
from utils.telegram_alerts import send_telegram

logger = get_logger()

class ExecutionEngine:

    def place_order(self, symbol, lot, order_type, price, sl, tp):
        """
        order_type: 1=BUY, -1=SELL
        FOK execution
        """
        deviation = 20
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot,
            "type": mt5.ORDER_TYPE_BUY if order_type == 1 else mt5.ORDER_TYPE_SELL,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": deviation,
            "magic": 123456,
            "comment": "EMA200 Pullback Bot",
            "type_filling": mt5.ORDER_FILLING_FOK
        }

        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.warning(f"Order failed: {result}")
        else:
            logger.info(f"Order executed: {request}")
            send_telegram(f"Order executed: {request}")
        return result