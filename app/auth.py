# -*- coding: utf-8 -*-
"""登入所需的裝飾器與登入失敗次數／鎖定追蹤。"""

import threading
from datetime import datetime, timedelta
from functools import wraps

from flask import jsonify, redirect, session, url_for

from .accounts_store import ACCOUNTS, find_user
from .config_store import CONFIG


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not ACCOUNTS:
            return redirect(url_for("pages.setup_page"))
        username = session.get("username")
        if not username:
            return redirect(url_for("pages.login_page"))
        user = find_user(username)
        if not user:
            # 帳號可能被刪掉了或設定被重置過，舊的 session cookie 不該再算數
            session.clear()
            return redirect(url_for("pages.login_page"))
        if user["role"] != session.get("role"):
            # 保險起見：如果帳號權限被改過，session 裡的舊權限也要跟著失效
            session.clear()
            return redirect(url_for("pages.login_page"))
        if user["role"] == "pending":
            # 核准後又被改回待審（或核准被撤銷）時，既有的 session 也要立刻失效
            session.clear()
            return redirect(url_for("pages.login_page"))
        return view(*args, **kwargs)
    return wrapped


def super_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("role") != "super":
            return jsonify({"ok": False, "error": "只有超級帳號可以執行這個操作"}), 403
        return view(*args, **kwargs)
    return wrapped


# 登入失敗次數只記在記憶體裡，重啟程式就會清空——這同時也是唯一的「手動解鎖」方式：
# 萬一自己被鎖住又不想等，重跑 webapp.py 即可。
LOGIN_FAILS_LOCK = threading.Lock()
LOGIN_FAILS = {}  # username -> {"count": int, "locked_until": datetime|None}


def _prune_login_fails(now):
    """清掉鎖定時間已過的紀錄，避免有人亂打帳號讓這個 dict 無限長大。呼叫前要先持有 lock。"""
    expired = [u for u, r in LOGIN_FAILS.items() if r["locked_until"] and r["locked_until"] <= now]
    for u in expired:
        del LOGIN_FAILS[u]


def get_lockout_remaining(username):
    """回傳這個帳號還要被鎖住幾秒；沒被鎖就回 0（鎖定時間到了會順手把失敗次數歸零）。"""
    now = datetime.now()
    with LOGIN_FAILS_LOCK:
        _prune_login_fails(now)
        rec = LOGIN_FAILS.get(username)
        if not rec or not rec["locked_until"]:
            return 0
        return (rec["locked_until"] - now).total_seconds()


def record_login_failure(username):
    """記一次登入失敗，回傳 (還剩幾次可以試, 這次是否觸發鎖定)。"""
    limit = max(1, int(CONFIG.get("login_fail_limit", 3)))
    lockout = max(1, int(CONFIG.get("login_lockout_minutes", 15)))
    now = datetime.now()
    with LOGIN_FAILS_LOCK:
        _prune_login_fails(now)
        rec = LOGIN_FAILS.setdefault(username, {"count": 0, "locked_until": None})
        rec["count"] += 1
        if rec["count"] >= limit:
            rec["locked_until"] = now + timedelta(minutes=lockout)
            return 0, True
        return limit - rec["count"], False


def clear_login_failures(username):
    with LOGIN_FAILS_LOCK:
        LOGIN_FAILS.pop(username, None)
