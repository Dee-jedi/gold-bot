import logging
import os
from logging.handlers import RotatingFileHandler

def setup_logger(name="GoldBot"):
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    
    if not logger.handlers:
        # Console Handler
        c_handler = logging.StreamHandler()
        c_handler.setLevel(logging.INFO)
        c_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S')
        c_handler.setFormatter(c_format)
        
        # File Handler (Rotating)
        f_handler = RotatingFileHandler(f"{log_dir}/trading_bot.log", maxBytes=5*1024*1024, backupCount=3)
        f_handler.setLevel(logging.DEBUG)
        f_format = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s')
        f_handler.setFormatter(f_format)
        
        logger.addHandler(c_handler)
        logger.addHandler(f_handler)
        
    return logger

logger = setup_logger()
