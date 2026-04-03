import logging
import colorlog

def get_logger(name: str) -> logging.Logger:
    handler = colorlog.StreamHandler()
    handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s] %(name)s%(reset)s — %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG":    "cyan",
            "INFO":     "green",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        },
    ))
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return logger
