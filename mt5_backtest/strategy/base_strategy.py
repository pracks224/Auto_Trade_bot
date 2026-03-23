# strategy/base_strategy.py
class BaseStrategy:
    def __init__(self, data):
        self.data = data

    def generate_signals(self):
        raise NotImplementedError