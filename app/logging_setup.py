# -*- coding: utf-8 -*-
"""全域 logging 設定。要在其他模組開始記 log 之前先 import 這支一次。"""

import logging
import sys
from logging.handlers import TimedRotatingFileHandler

from .paths import LOG_DIR

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_log_handler = TimedRotatingFileHandler(
    LOG_DIR / "webapp.log", when="midnight", backupCount=30, encoding="utf-8"
)
_log_handler.suffix = "%Y-%m-%d"
_log_handler.setFormatter(logging.Formatter(
    "[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
))
logging.getLogger().setLevel(logging.INFO)
logging.getLogger().addHandler(_log_handler)

logger = logging.getLogger("webapp")
