# -*- coding: utf-8 -*-
"""共用路徑常數。"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
ACCOUNTS_PATH = BASE_DIR / "accounts.json"

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
LATEST_PATH = DATA_DIR / "latest.json"
ALERTED_PATH = DATA_DIR / "alerted.json"
PREV_ACTIVE_PATH = DATA_DIR / "prev_active.json"
HISTORY_DB_PATH = DATA_DIR / "history.db"

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
