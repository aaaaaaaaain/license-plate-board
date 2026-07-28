# -*- coding: utf-8 -*-
"""帳號儲存（獨立於 config.json）。

帳號資料跟掃描／寄信設定的生命週期不一樣：設定是你自己在調的，
帳號則會被註冊、核准、刪除不斷改寫。分開存可以避免兩邊互相覆蓋，
備份或重置設定時也不會不小心把所有帳號一起弄丟。
"""

import json
import re
import threading

from .logging_setup import logger
from .paths import ACCOUNTS_PATH, CONFIG_PATH

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# waitress 是多執行緒的，兩個人同時註冊／核准會同時改到 ACCOUNTS 再各自寫檔，
# 可能互相蓋掉甚至寫出壞掉的 accounts.json。所有異動帳號的地方都要先拿這把鎖。
USERS_LOCK = threading.RLock()

# 註冊是對整個網際網路開放的（只是要等核准），不設上限的話，
# 機器人可以灌爆 config.json。累積到這個數量的待審帳號就先擋下新的申請。
MAX_PENDING_USERS = 50


def load_accounts():
    """讀 accounts.json；第一次執行時把舊的 config.json['auth']['users'] 搬過來。"""
    if ACCOUNTS_PATH.exists():
        try:
            data = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            # 這個檔壞掉的話，全部帳號都會登不進去。與其默默當成沒有帳號
            # （那會讓 /setup 對全世界開放，任何人都能搶走超級帳號），不如直接停住。
            raise SystemExit(f"accounts.json 讀取失敗，請先修好再啟動：{e}")
        return data.get("users", [])

    # 舊版是把帳號放在 config.json 裡，搬一次過來
    legacy = []
    if CONFIG_PATH.exists():
        try:
            legacy = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("auth", {}).get("users", [])
        except (json.JSONDecodeError, OSError):
            legacy = []
    save_accounts(legacy)
    if legacy:
        logger.info(f"[accounts] 已把 {len(legacy)} 個帳號從 config.json 搬到 accounts.json")
    return legacy


def save_accounts(users):
    ACCOUNTS_PATH.write_text(
        json.dumps({"users": users}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def find_user(username):
    for u in ACCOUNTS:
        if u["username"] == username:
            return u
    return None


ACCOUNTS = load_accounts()
