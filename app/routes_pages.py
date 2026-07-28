# -*- coding: utf-8 -*-
"""網頁頁面路由（設定、註冊、登入、看板首頁、歷史查詢頁）。"""

from datetime import datetime

from flask import Blueprint, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from plate_bid_scanner import CATEGORIES

from .accounts_store import ACCOUNTS, USERS_LOCK, EMAIL_RE, MAX_PENDING_USERS, find_user, save_accounts
from .auth import clear_login_failures, get_lockout_remaining, login_required, record_login_failure
from .config_store import CONFIG
from .logging_setup import logger

bp = Blueprint("pages", __name__)


@bp.route("/setup", methods=["GET", "POST"])
def setup_page():
    """建立第一個帳號──永遠是超級帳號。之後其他帳號要由超級帳號在設定頁新增。"""
    if ACCOUNTS:
        return redirect(url_for("pages.login_page"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username:
            error = "請輸入帳號"
        elif len(password) < 8:
            error = "密碼至少要 8 碼"
        else:
            with USERS_LOCK:
                ACCOUNTS.append({
                    "username": username,
                    "password_hash": generate_password_hash(password),
                    "role": "super",
                    "email": "",
                    "registered_at": datetime.now().isoformat(timespec="seconds"),
                })
                save_accounts(ACCOUNTS)
            session.clear()
            session["username"] = username
            session["role"] = "super"
            session.permanent = True
            return redirect(url_for("pages.index"))
    return render_template("setup.html", error=error)


@bp.route("/register", methods=["GET", "POST"])
def register_page():
    """一般人自行註冊。註冊完是 pending 狀態，要超級帳號核准後才登得進來。"""
    if not ACCOUNTS:
        return redirect(url_for("pages.setup_page"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")

        with USERS_LOCK:
            pending_count = sum(1 for u in ACCOUNTS if u.get("role") == "pending")
            if not username or len(username) > 50:
                error = "帳號請填 1～50 個字"
            elif not EMAIL_RE.match(email):
                error = "Email 格式看起來不正確"
            elif len(password) < 8:
                error = "密碼至少要 8 碼"
            elif password != password2:
                error = "兩次輸入的密碼不一致"
            elif find_user(username):
                error = "這個帳號已經有人用了"
            elif pending_count >= MAX_PENDING_USERS:
                error = "目前待審核的申請過多，請稍後再試"
            else:
                ACCOUNTS.append({
                    "username": username,
                    "password_hash": generate_password_hash(password),
                    "role": "pending",
                    "email": email,
                    "registered_at": datetime.now().isoformat(timespec="seconds"),
                })
                save_accounts(ACCOUNTS)
                logger.info(f"[register] 新註冊待審核：{username} <{email}>（來源 {request.remote_addr}）")
                return render_template("register.html", done=True)

    return render_template("register.html", error=error, done=False)


@bp.route("/login", methods=["GET", "POST"])
def login_page():
    if not ACCOUNTS:
        return redirect(url_for("pages.setup_page"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        locked_seconds = get_lockout_remaining(username)
        if locked_seconds > 0:
            mins_left = int(locked_seconds // 60) + 1
            logger.warning(f"[login] 帳號 {username} 在鎖定期間嘗試登入（來源 {request.remote_addr}）")
            return render_template(
                "login.html",
                error=f"此帳號登入失敗次數過多，已暫時鎖定，請於 {mins_left} 分鐘後再試",
            )

        user = find_user(username)
        if user and check_password_hash(user["password_hash"], password):
            # 密碼是對的，所以失敗計數該歸零——就算帳號還沒核准也一樣，
            # 不然本人會因為多試幾次自己的正確密碼而被鎖住
            clear_login_failures(username)
            if user.get("role") == "pending":
                return render_template(
                    "login.html",
                    error="您的帳號已送出申請，還在等管理員核准，核准後才能登入",
                )
            session.clear()
            session["username"] = user["username"]
            session["role"] = user["role"]
            session.permanent = True
            return redirect(url_for("pages.index"))

        tries_left, just_locked = record_login_failure(username)
        if just_locked:
            lockout = max(1, int(CONFIG.get("login_lockout_minutes", 15)))
            logger.warning(f"[login] 帳號 {username} 連續登入失敗達上限，鎖定 {lockout} 分鐘"
                           f"（來源 {request.remote_addr}）")
            error = f"密碼連續錯誤太多次，此帳號已鎖定 {lockout} 分鐘，請稍後再試"
        else:
            logger.warning(f"[login] 帳號 {username} 登入失敗（來源 {request.remote_addr}），"
                           f"還可嘗試 {tries_left} 次")
            error = f"帳號或密碼錯誤，還可以再試 {tries_left} 次"
    return render_template("login.html", error=error)


@bp.route("/logout")
def logout_page():
    session.clear()
    return redirect(url_for("pages.login_page"))


@bp.route("/")
def index():
    return render_template(
        "index.html",
        categories=CATEGORIES,
        scan_interval_minutes=CONFIG["scan_interval_minutes"],
        alert_before_minutes=CONFIG["alert_before_minutes"],
        logged_in=bool(session.get("username")),
        username=session.get("username"),
    )


@bp.route("/history")
def history_page():
    return render_template(
        "history.html",
        logged_in=bool(session.get("username")),
        username=session.get("username"),
    )


@bp.route("/settings")
@login_required
def settings_page():
    is_super = session.get("role") == "super"
    return render_template("settings.html", config=CONFIG, is_super=is_super, username=session.get("username"))
