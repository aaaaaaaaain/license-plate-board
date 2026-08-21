# -*- coding: utf-8 -*-
"""網頁頁面路由（設定、註冊、登入、看板首頁、歷史查詢頁）。"""

import secrets
from datetime import datetime

from flask import Blueprint, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from plate_bid_scanner import CATEGORIES

from .accounts_store import (
    ACCOUNTS,
    EMAIL_RE,
    MAX_UNVERIFIED_USERS,
    USERS_LOCK,
    VERIFY_RESEND_COOLDOWN_SECONDS,
    VERIFY_TTL_HOURS,
    find_user,
    find_user_by_verify_token,
    purge_expired_unverified,
    save_accounts,
)
from .auth import clear_login_failures, get_lockout_remaining, login_required, record_login_failure
from .config_store import CONFIG
from .logging_setup import logger
from .notifications import deliver_email
from .public_url import get_public_url

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

        token = None
        with USERS_LOCK:
            purge_expired_unverified()
            existing = find_user(username)
            # 還沒驗證的帳號等於還沒有人「擁有」它，同一個名字可以再送一次
            # （等於重寄驗證信）；已經驗證過的名字就是別人的了
            reusable = bool(existing) and existing.get("role") == "unverified"
            waiting = sum(1 for u in ACCOUNTS if u.get("role") in ("pending", "unverified"))
            resend_wait = _verify_resend_wait(existing) if reusable else 0

            if not username or len(username) > 50:
                error = "帳號請填 1～50 個字"
            elif not EMAIL_RE.match(email):
                error = "Email 格式看起來不正確"
            elif len(password) < 8:
                error = "密碼至少要 8 碼"
            elif password != password2:
                error = "兩次輸入的密碼不一致"
            elif existing and not reusable:
                error = "這個帳號已經有人用了"
            elif resend_wait:
                error = f"驗證信剛寄出過，請 {resend_wait} 秒後再試，或先去信箱收信"
            elif not existing and waiting >= MAX_UNVERIFIED_USERS:
                error = "目前等待驗證的申請過多，請稍後再試"
            else:
                token = secrets.token_urlsafe(32)
                now = datetime.now().isoformat(timespec="seconds")
                user = existing if existing else {"username": username}
                user.update({
                    "password_hash": generate_password_hash(password),
                    "role": "unverified",
                    "email": email,
                    "registered_at": now,
                    "verify_token": token,
                    "verify_sent_at": now,
                })
                if not existing:
                    ACCOUNTS.append(user)
                save_accounts(ACCOUNTS)

        if token:
            # 寄信放在鎖外面：SMTP 可能要好幾秒，占著 USERS_LOCK 會讓其他人
            # 連登入都卡住
            try:
                _send_verify_email(email, username, token)
            except Exception as e:
                logger.error(f"[register] 驗證信寄不出去（{username} <{email}>）：{e}")
                error = "驗證信寄不出去，請稍後再試一次；一直失敗的話請聯絡管理員"
            else:
                logger.info(f"[register] 已寄出驗證信：{username} <{email}>"
                            f"（來源 {request.remote_addr}）")
                return render_template("register.html", done=True, email=email)

    return render_template("register.html", error=error, done=False)


def _verify_resend_wait(user):
    """距離可以重寄驗證信還要等幾秒，0 代表現在就能寄。"""
    try:
        sent = datetime.fromisoformat(user.get("verify_sent_at", ""))
    except ValueError:
        return 0
    elapsed = (datetime.now() - sent).total_seconds()
    if elapsed >= VERIFY_RESEND_COOLDOWN_SECONDS:
        return 0
    return int(VERIFY_RESEND_COOLDOWN_SECONDS - elapsed) + 1


def _send_verify_email(recipient, username, token):
    """寄註冊驗證信。寄不出去就讓例外往外丟，由呼叫端決定要顯示什麼。"""
    email_cfg = CONFIG["email"]
    if not email_cfg.get("app_password") or not email_cfg.get("smtp_host"):
        raise RuntimeError("尚未設定寄件帳號")
    # 有固定網址就用固定網址；還在用隨機網址時退回這次請求的來源網址
    base = (get_public_url() or request.url_root).rstrip("/")
    deliver_email(
        {**email_cfg, "to_addrs": [recipient]},
        subject="[車牌競標看板] 請驗證你的註冊",
        body=(
            f"哈囉 {username}：\n\n"
            "點下面的連結就完成註冊，之後可以直接用你設定的帳號密碼登入：\n\n"
            f"{base}/verify?token={token}\n\n"
            f"連結 {VERIFY_TTL_HOURS} 小時內有效，過期的話重新註冊一次就會再寄一封。\n"
            "如果這不是你申請的，忽略這封信即可，那個帳號會自己失效。"
        ),
    )


@bp.route("/verify")
def verify_page():
    """點驗證信裡的連結。驗證過就直接開通，不需要再等管理員核准。"""
    token = request.args.get("token", "").strip()
    with USERS_LOCK:
        purge_expired_unverified()
        user = find_user_by_verify_token(token) if token else None
        if not user:
            # 連結打錯、已經過期，或這個帳號已經驗證過了（token 驗完就刪掉）
            return render_template("verify.html", ok=False), 400
        user["role"] = "normal"
        user.pop("verify_token", None)
        user.pop("verify_sent_at", None)
        user["verified_at"] = datetime.now().isoformat(timespec="seconds")
        username = user["username"]
        save_accounts(ACCOUNTS)
    logger.info(f"[verify] {username} 完成 Email 驗證，帳號已開通")
    return render_template("verify.html", ok=True, username=username)


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
            if user.get("role") == "unverified":
                return render_template(
                    "login.html",
                    error="這個帳號還沒完成 Email 驗證，請到信箱點驗證連結"
                          "（沒收到的話，用同一組帳號密碼再註冊一次就會重寄）",
                )
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
        # 歷史頁的自動更新跟著背景掃描的節奏走，間隔改設定就一起改
        scan_interval_minutes=CONFIG["scan_interval_minutes"],
        logged_in=bool(session.get("username")),
        username=session.get("username"),
    )


@bp.route("/settings")
@login_required
def settings_page():
    is_super = session.get("role") == "super"
    return render_template("settings.html", config=CONFIG, is_super=is_super, username=session.get("username"))
