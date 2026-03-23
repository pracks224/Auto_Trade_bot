import logging
from config import LOG_FILE

def get_logger():
    logger = logging.getLogger("GoldBot")
    logger.setLevel(logging.INFO)

    fh = logging.FileHandler(LOG_FILE)
    fh.setLevel(logging.INFO)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s"
    )
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)

    logger.addHandler(fh)
    logger.addHandler(ch)

    return logger