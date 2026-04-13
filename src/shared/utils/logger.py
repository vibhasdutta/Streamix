import logging
import asyncio
import sys
import threading
import traceback
from logging.handlers import RotatingFileHandler

from core.paths import LOGS_DIR, ensure_data_directories

_exception_hooks_installed = False

def setup_logger(name, filename, level=logging.INFO):
    """Function to setup as many loggers as you want"""

    ensure_data_directories()
    log_path = LOGS_DIR / filename
    
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # Use RotatingFileHandler to manage log size (10MB per file, 5 backups)
    handler = RotatingFileHandler(str(log_path), maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
    handler.setFormatter(formatter)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    
    # Avoid duplicate handlers if setup_logger is called multiple times on the same logger name
    if not logger.handlers:
        logger.addHandler(handler)

    _install_exception_hooks(logger)
        
    return logger


def _install_exception_hooks(logger):
    global _exception_hooks_installed
    if _exception_hooks_installed:
        return

    def _log_uncaught_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.error(
            "Uncaught exception",
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    def _thread_exception_handler(args):
        logger.error(
            "Uncaught thread exception in %s",
            args.thread.name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _log_uncaught_exception
    threading.excepthook = _thread_exception_handler
    _exception_hooks_installed = True

def install_asyncio_exception_handler(loop, logger):
    def _handle_asyncio_exception(loop, context):
        message = context.get("message", "Unhandled asyncio exception")
        exception = context.get("exception")
        if exception is not None:
            logger.error(message, exc_info=exception)
        else:
            logger.error("%s: %s", message, context)

    loop.set_exception_handler(_handle_asyncio_exception)
