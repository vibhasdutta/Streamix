import logging
import os
from logging.handlers import RotatingFileHandler

def setup_logger(name, filename, level=logging.INFO):
    """Function to setup as many loggers as you want"""
    
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "logs")
    os.makedirs(log_dir, exist_ok=True)
    
    log_path = os.path.join(log_dir, filename)
    
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # Use RotatingFileHandler to manage log size (10MB per file, 5 backups)
    handler = RotatingFileHandler(log_path, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
    handler.setFormatter(formatter)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # Avoid duplicate handlers if setup_logger is called multiple times on the same logger name
    if not logger.handlers:
        logger.addHandler(handler)
        
    return logger
