import logging
import sys

from loguru import logger

_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "urllib3",
    "telegram",
    "apscheduler",
    "google_genai",
    "google.auth",
)

_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level:<7}</level> | "
    "<cyan>{name}</cyan> - "
    "<level>{message}</level>"
)


class _InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup(level: str = "INFO") -> None:
    logger.remove()
    logger.add(sys.stderr, level=level, format=_FORMAT, backtrace=True, diagnose=False)
    logging.basicConfig(handlers=[_InterceptHandler()], level=logging.DEBUG, force=True)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
