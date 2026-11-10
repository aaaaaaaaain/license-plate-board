# -*- coding: utf-8 -*-
"""共用路徑常數。"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
ACCOUNTS_PATH = BASE_DIR / "accounts.json"

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
LATEST_PATH = DATA_DIR / "latest.json"
ALERTED_PATH = DATA_DIR / "alerted.json"
PREV_ACTIVE_PATH = DATA_DIR / "prev_active.json"
# 出價歷史資料庫放在 WSL 本機的 ext4，不放專案裡的 data/：專案在 /mnt/d，走 9p，
# 2026-11-10 實測同一批查詢在 9p 上慢 3～45 倍（歷史頁摘要 1.8 秒 → 0.3 秒、
# 每輪掃描寫入 0.9 秒 → 0.1 秒）。每天由 backup_history_db.py 備份回 data/。
# 設 EVE_HISTORY_DB 可以改用別的檔案（例如測試時指向一份複本）。
HISTORY_DB_PATH = Path(os.environ.get("EVE_HISTORY_DB") or Path.home() / "eve-data" / "history.db")
HISTORY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
DAILY_BACKUP_PREFIX = "history.db.daily-"

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
