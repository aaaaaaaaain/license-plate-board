# -*- coding: utf-8 -*-
"""帳號儲存（獨立於 config.json）。

帳號資料跟掃描／寄信設定的生命週期不一樣：設定是你自己在調的，
帳號則會被註冊、核准、刪除不斷改寫。分開存可以避免兩邊互相覆蓋，
備份或重置設定時也不會不小心把所有帳號一起弄丟。
"""

import json
import re
import secrets
import threading
from datetime import datetime, timedelta

from .logging_setup import logger
from .paths import ACCOUNTS_PATH, CONFIG_PATH

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# waitress 是多執行緒的，兩個人同時註冊／核准會同時改到 ACCOUNTS 再各自寫檔，
# 可能互相蓋掉甚至寫出壞掉的 accounts.json。所有異動帳號的地方都要先拿這把鎖。
USERS_LOCK = threading.RLock()

# 註冊改成「點驗證信就直接開通」之後，沒有人工把關那一關了，所以還沒驗證的
# 暫存帳號一定要有上限跟保存期限：註冊對整個網際網路開放，少了這兩道就是讓
# 機器人拿隨機信箱灌爆 accounts.json，順便把 Gmail 每日寄信額度耗光。
# 上限同時涵蓋舊的 pending（人工核准年代留下來的帳號）與新的 unverified。
MAX_UNVERIFIED_USERS = 50
VERIFY_TTL_HOURS = 24
VERIFY_RESEND_COOLDOWN_SECONDS = 300


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


def find_user_by_verify_token(token):
    """用驗證連結上的 token 找帳號。

    用 compare_digest 而不是 ==：後者比到不同的字元就結束，回應時間會隨著
    猜對幾個字元而變化，等於把 token 一個字元一個字元洩漏出去。
    """
    for u in ACCOUNTS:
        stored = u.get("verify_token")
        if stored and secrets.compare_digest(stored, token):
            return u
    return None


def purge_expired_unverified():
    """清掉超過 VERIFY_TTL_HOURS 還沒點驗證信的帳號，回傳清掉幾個。

    這同時也是「帳號名稱被卡住」的解法——沒完成驗證的名字過期會自己釋放出來，
    不需要管理員手動去刪。呼叫前要先持有 USERS_LOCK。
    """
    cutoff = datetime.now() - timedelta(hours=VERIFY_TTL_HOURS)
    stale = []
    for u in ACCOUNTS:
        if u.get("role") != "unverified":
            continue
        try:
            registered = datetime.fromisoformat(u.get("registered_at", ""))
        except ValueError:
            registered = None
        # 沒有時間或時間格式壞掉的一律當成過期，不然會永遠卡在清單裡
        if registered is None or registered < cutoff:
            stale.append(u)
    for u in stale:
        ACCOUNTS.remove(u)
    if stale:
        save_accounts(ACCOUNTS)
        logger.info(f"[accounts] 清掉 {len(stale)} 個過期未驗證的註冊："
                    + "、".join(u["username"] for u in stale))
    return len(stale)


ACCOUNTS = load_accounts()
