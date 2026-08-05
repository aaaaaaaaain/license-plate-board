# -*- coding: utf-8 -*-
"""JSON API 路由。"""

import json
import threading
from datetime import datetime

from flask import Blueprint, jsonify, request, session
from werkzeug.security import generate_password_hash

from plate_bid_scanner import CATEGORIES

from .accounts_store import ACCOUNTS, USERS_LOCK, EMAIL_RE, find_user, save_accounts
from .auth import login_required, super_required
from .config_store import CONFIG, save_config
from .history_db import (
    add_watchlist,
    delete_user_watchlist,
    get_decided_results,
    get_history_summary,
    get_plate_history,
    get_watchlist,
    remove_watchlist,
)
from .logging_setup import logger
from .notifications import deliver_email
from .scanner import STATE, STATE_LOCK, STATIONS_CACHE, trigger_scan_now

bp = Blueprint("api", __name__)


@bp.route("/api/data")
def api_data():
    with STATE_LOCK:
        return jsonify({
            "results": STATE["results"],
            "last_updated": STATE["last_updated"],
            "last_error": STATE["last_error"],
            "scanning": STATE["scanning"],
            "alert_before_minutes": CONFIG["alert_before_minutes"],
        })


@bp.route("/api/scan-now", methods=["POST"])
def api_scan_now():
    """「立即重新整理畫面」實際觸發背景馬上重新掃描一次（不是只重讀伺服器現有的快取）。

    不需要登入——效果跟等排定的掃描自然輪到一樣，只是提早，公開資料本來就看得到。
    真正的節流靠 trigger_scan_now() 裡的「正在掃描中／30 秒內剛掃過」檢查，
    不是靠登入與否擋。
    """
    ok, error = trigger_scan_now()
    if not ok:
        return jsonify({"ok": False, "error": error}), 429
    return jsonify({"ok": True})


@bp.route("/api/stations")
def api_stations():
    return jsonify({"sections": STATIONS_CACHE})


@bp.route("/api/history/<plate>")
def api_history(plate):
    category = request.args.get("category")
    section = request.args.get("section")
    station = request.args.get("station")
    history, decided = get_plate_history(plate, category, section, station)
    return jsonify({"plate": plate, "history": history, "decided": decided})


@bp.route("/api/history-list")
def api_history_list():
    return jsonify({"items": get_history_summary()})


@bp.route("/api/decided-list")
def api_decided_list():
    return jsonify({"items": get_decided_results()})


@bp.route("/api/watchlist", methods=["GET", "POST", "DELETE"])
@login_required
def api_watchlist():
    """個人追蹤清單。一律只操作自己的資料——使用者名稱取自 session，
    不看請求內容，所以沒辦法藉由改參數去讀或改別人的清單。
    """
    username = session["username"]

    if request.method == "GET":
        return jsonify({"items": get_watchlist(username)})

    data = request.get_json(force=True, silent=True) or {}
    number_key = str(data.get("number_key", "")).strip()
    category = str(data.get("category", "")).strip()
    if not number_key or not category:
        return jsonify({"ok": False, "error": "缺少號碼或車種"}), 400
    if len(number_key) > 20 or len(category) > 50:
        return jsonify({"ok": False, "error": "號碼或車種格式不正確"}), 400

    if request.method == "POST":
        ok, err = add_watchlist(username, number_key, category)
        if not ok:
            return jsonify({"ok": False, "error": err}), 400
        return jsonify({"ok": True})

    remove_watchlist(username, number_key, category)
    return jsonify({"ok": True})


@bp.route("/api/my-settings", methods=["GET", "POST"])
@login_required
def api_my_settings():
    """使用者自己的通知設定。帳號一律取自 session，不看請求內容，
    所以沒辦法藉由改參數去讀或改別人的信箱。
    """
    username = session["username"]

    if request.method == "GET":
        with USERS_LOCK:
            user = find_user(username)
            return jsonify({
                "username": username,
                "email": user.get("email", "") if user else "",
                # 舊帳號沒有這個欄位，預設視為要收提醒
                "notify_enabled": bool(user.get("notify_enabled", True)) if user else False,
                # 全站廣播是新功能、不是舊行為延續，預設關閉，要自己開
                "broadcast_enabled": bool(user.get("broadcast_enabled", False)) if user else False,
                # None 代表沒有自訂，跟著全站預設值走
                "alert_before_minutes": (user.get("alert_before_minutes") if user else None),
                "default_alert_before_minutes": CONFIG["alert_before_minutes"],
                # 廣播要收哪些車種；空清單＝全部都收（舊帳號沒這個欄位就是全收）
                "broadcast_categories": list(user.get("broadcast_categories") or []) if user else [],
                "all_categories": list(CATEGORIES),
            })

    data = request.get_json(force=True, silent=True) or {}
    email = str(data.get("email", "")).strip()
    notify_enabled = bool(data.get("notify_enabled", True))
    broadcast_enabled = bool(data.get("broadcast_enabled", False))
    if email and not EMAIL_RE.match(email):
        return jsonify({"ok": False, "error": "Email 格式不正確"}), 400
    if (notify_enabled or broadcast_enabled) and not email:
        return jsonify({"ok": False, "error": "要接收提醒的話，請先填寫通知信箱"}), 400

    # 車種篩選：只留系統認得的車種，避免有人塞任意字串進帳號檔。
    # 全選跟全不選都存成空清單＝不篩選，語意一致（不然全不選會變成什麼都收不到，
    # 但使用者以為自己只是還沒選）。
    raw_categories = data.get("broadcast_categories")
    broadcast_categories = []
    if isinstance(raw_categories, list):
        broadcast_categories = [c for c in CATEGORIES if c in raw_categories]
        if len(broadcast_categories) == len(CATEGORIES):
            broadcast_categories = []

    # 空值／0／null 都代表「不要自訂，跟著全站預設值走」
    alert_before_raw = data.get("alert_before_minutes")
    alert_before_minutes = None
    if alert_before_raw not in (None, "", 0):
        try:
            alert_before_minutes = int(alert_before_raw)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "即將截止門檻格式不正確"}), 400
        if alert_before_minutes < 1:
            return jsonify({"ok": False, "error": "即將截止門檻必須是正整數（分鐘）"}), 400

    with USERS_LOCK:
        user = find_user(username)
        if not user:
            return jsonify({"ok": False, "error": "找不到你的帳號"}), 404
        user["email"] = email
        user["notify_enabled"] = notify_enabled
        user["broadcast_enabled"] = broadcast_enabled
        if broadcast_categories:
            user["broadcast_categories"] = broadcast_categories
        else:
            user.pop("broadcast_categories", None)
        if alert_before_minutes is None:
            user.pop("alert_before_minutes", None)
        else:
            user["alert_before_minutes"] = alert_before_minutes
        save_accounts(ACCOUNTS)
    logger.info(f"[my-settings] {username} 更新了個人通知設定"
                f"（追蹤提醒{'開' if notify_enabled else '關'}、全站廣播{'開' if broadcast_enabled else '關'}、"
                f"廣播車種{'／'.join(broadcast_categories) if broadcast_categories else '全部'}、"
                f"即將截止門檻{alert_before_minutes if alert_before_minutes is not None else '預設'}）")
    return jsonify({"ok": True})


# 測試信是用管理員那組 Gmail 憑證寄的，Gmail 每天有寄送額度上限。
# 不設冷卻的話，任何登入者狂按按鈕就能把當天的額度燒光，害真正的截止提醒寄不出去。
MY_TEST_EMAIL_COOLDOWN = 60  # 秒
LAST_TEST_EMAIL_LOCK = threading.Lock()
LAST_TEST_EMAIL = {}  # username -> datetime


@bp.route("/api/my-test-email", methods=["POST"])
@login_required
def api_my_test_email():
    """寄一封測試信給自己。

    跟 /api/test-email 的關鍵差別：這支完全不看請求內容。
    寄件主機與憑證一律用伺服器上存好的設定，收件人一律是 session 對應帳號
    自己填的信箱，所以沒有「把 smtp_host 指到自己主機、騙走寄件憑證」的空間，
    也不可能拿來寄信給任意第三方。
    """
    username = session["username"]

    now = datetime.now()
    with LAST_TEST_EMAIL_LOCK:
        last = LAST_TEST_EMAIL.get(username)
        if last and (now - last).total_seconds() < MY_TEST_EMAIL_COOLDOWN:
            wait = int(MY_TEST_EMAIL_COOLDOWN - (now - last).total_seconds()) + 1
            return jsonify({"ok": False, "error": f"寄太頻繁了，請 {wait} 秒後再試"}), 429
        # 先卡住時間再寄，避免同時按兩次時兩個執行緒都通過檢查
        LAST_TEST_EMAIL[username] = now

    with USERS_LOCK:
        user = find_user(username)
        recipient = user.get("email", "") if user else ""
    if not recipient:
        return jsonify({"ok": False, "error": "請先填好通知信箱並按「儲存我的通知設定」，再寄測試信"}), 400

    email_cfg = CONFIG["email"]
    if not email_cfg.get("app_password") or not email_cfg.get("smtp_host"):
        return jsonify({"ok": False, "error": "管理員還沒設定好寄件帳號，暫時無法寄送"}), 400

    try:
        deliver_email(
            {**email_cfg, "to_addrs": [recipient]},
            subject="[車牌競標看板] 測試郵件",
            body=(
                "這是一封測試郵件。\n\n"
                f"你收到這封信，代表 {recipient} 可以正常接收你追蹤號碼的截止提醒。"
            ),
        )
    except Exception as e:
        logger.error(f"[my-test-email] {username} 寄送失敗：{e}")
        return jsonify({"ok": False, "error": str(e)}), 400

    logger.info(f"[my-test-email] 已寄測試信給 {username}")
    return jsonify({
        "ok": True,
        # 管理員把通知總開關關掉時，測試信寄得出去但實際上不會有截止提醒，要講清楚
        "warning": None if email_cfg.get("enabled") else
                   "測試信已寄出，但管理員目前關閉了 Email 通知，實際的截止提醒不會發送",
    })


@bp.route("/api/settings", methods=["GET", "POST"])
@login_required
def api_settings():
    is_super = session.get("role") == "super"

    if request.method == "GET":
        cfg = json.loads(json.dumps(CONFIG))
        cfg["auth"] = {}  # 帳密資訊不透過這支 API 外流
        if not is_super:
            cfg["email"]["app_password"] = ""
            cfg["email"]["username"] = ""
            cfg["email"]["from_addr"] = ""
            cfg["discord"]["bot_token"] = ""
        return jsonify(cfg)

    data = request.get_json(force=True, silent=True) or {}
    try:
        if is_super:
            scan_interval = int(data.get("scan_interval_minutes", CONFIG["scan_interval_minutes"]))
            alert_before = int(data.get("alert_before_minutes", CONFIG["alert_before_minutes"]))
            if scan_interval < 1 or alert_before < 1:
                raise ValueError("掃描間隔與警示時間必須是正整數")

            email_in = data.get("email", {})
            new_email_cfg = {
                "enabled": bool(email_in.get("enabled", False)),
                "smtp_host": str(email_in.get("smtp_host", "")).strip(),
                "smtp_port": int(email_in.get("smtp_port", 587)),
                "username": str(email_in.get("username", "")).strip(),
                "app_password": str(email_in.get("app_password", "")),
                "from_addr": str(email_in.get("from_addr", "")).strip(),
            }
            discord_in = data.get("discord", {})
            new_discord_cfg = {
                "enabled": bool(discord_in.get("enabled", False)),
                "bot_token": str(discord_in.get("bot_token", "")).strip(),
                "command_prefix": str(discord_in.get("command_prefix", "!")).strip() or "!",
            }
        else:
            # 一般帳號整組沿用原本的值，不管前端送了什麼都不會被採用——
            # 掃描間隔／即將截止預設值現在只有超級帳號能改（一般帳號要調整
            # 自己的即將截止門檻，請用「我的通知設定」）；寄件設定（SMTP 憑證）
            # 跟 Discord 設定不開放的原因同理，特別是 smtp_host／smtp_port，
            # 那組值會被拿去做真正的 SMTP 登入，讓一般帳號指定等於把寄件憑證
            # 送到對方架的主機上。
            scan_interval = CONFIG["scan_interval_minutes"]
            alert_before = CONFIG["alert_before_minutes"]
            new_email_cfg = dict(CONFIG["email"])
            new_discord_cfg = dict(CONFIG["discord"])
    except (TypeError, ValueError) as e:
        return jsonify({"ok": False, "error": f"設定格式錯誤：{e}"}), 400

    CONFIG["scan_interval_minutes"] = scan_interval
    CONFIG["alert_before_minutes"] = alert_before
    CONFIG["email"] = new_email_cfg
    CONFIG["discord"] = new_discord_cfg
    save_config(CONFIG)
    return jsonify({"ok": True, "discord_restart_required": True})


@bp.route("/api/test-email", methods=["POST"])
@login_required
@super_required
def api_test_email():
    # 這支會拿設定裡的寄件憑證去登入 smtp_host 指定的主機。
    # 如果讓非超級帳號呼叫，對方只要把 smtp_host 指向自己的伺服器，
    # 就能讓程式主動把 Gmail 應用程式密碼送過去──所以只有超級帳號能用。
    data = request.get_json(force=True, silent=True) or {}
    email_in = data.get("email", {})
    to_addrs = email_in.get("to_addrs", [])
    if isinstance(to_addrs, str):
        to_addrs = [a.strip() for a in to_addrs.split(",") if a.strip()]

    test_cfg = {
        "smtp_host": str(email_in.get("smtp_host", "")).strip(),
        "smtp_port": int(email_in.get("smtp_port", 587) or 587),
        "username": str(email_in.get("username", "")).strip(),
        "app_password": str(email_in.get("app_password", "")),
        "from_addr": str(email_in.get("from_addr", "")).strip(),
        "to_addrs": to_addrs,
    }
    try:
        deliver_email(
            test_cfg,
            subject="[車牌競標看板] 測試郵件",
            body="這是一封測試郵件，如果你收到這封信，代表 Email 設定成功。",
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@bp.route("/api/accounts", methods=["GET", "POST"])
@login_required
@super_required
def api_accounts():
    if request.method == "GET":
        with USERS_LOCK:
            users = [
                {
                    "username": u["username"],
                    "role": u["role"],
                    "email": u.get("email", ""),
                    "registered_at": u.get("registered_at", ""),
                }
                for u in ACCOUNTS
            ]
        # 待審核的排在最前面，才不用在一長串帳號裡找哪些要處理
        users.sort(key=lambda u: (u["role"] != "pending", u["username"]))
        return jsonify({"users": users})

    data = request.get_json(force=True, silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    email = str(data.get("email", "")).strip()
    if not username or len(password) < 8:
        return jsonify({"ok": False, "error": "帳號不可空白，密碼至少 8 碼"}), 400

    with USERS_LOCK:
        if find_user(username):
            return jsonify({"ok": False, "error": "這個帳號已經存在"}), 400
        ACCOUNTS.append({
            "username": username,
            "password_hash": generate_password_hash(password),
            "role": "normal",
            "email": email,
            "registered_at": datetime.now().isoformat(timespec="seconds"),
        })
        save_accounts(ACCOUNTS)
    logger.info(f"[accounts] 超級帳號直接建立帳號：{username}")
    return jsonify({"ok": True})


@bp.route("/api/accounts/<username>/approve", methods=["POST"])
@login_required
@super_required
def api_approve_account(username):
    """核准待審帳號，role 從 pending 變成 normal。"""
    with USERS_LOCK:
        target = find_user(username)
        if not target:
            return jsonify({"ok": False, "error": "找不到這個帳號"}), 404
        if target["role"] != "pending":
            return jsonify({"ok": False, "error": "這個帳號不是待審核狀態"}), 400
        target["role"] = "normal"
        target["approved_at"] = datetime.now().isoformat(timespec="seconds")
        save_accounts(ACCOUNTS)
    logger.info(f"[accounts] 已核准帳號：{username}")
    return jsonify({"ok": True})


@bp.route("/api/accounts/<username>", methods=["DELETE"])
@login_required
@super_required
def api_delete_account(username):
    with USERS_LOCK:
        target = find_user(username)
        if not target:
            return jsonify({"ok": False, "error": "找不到這個帳號"}), 404
        if target["role"] == "super" and sum(1 for u in ACCOUNTS if u["role"] == "super") <= 1:
            return jsonify({"ok": False, "error": "至少要保留一個超級帳號"}), 400
        # 用切片就地改寫，不要重新綁定 ACCOUNTS——其他地方都是直接參照這個 list
        ACCOUNTS[:] = [u for u in ACCOUNTS if u["username"] != username]
        save_accounts(ACCOUNTS)
    # 帳號沒了，追蹤清單也一起清掉，不然資料庫會留下永遠沒人看的孤兒紀錄
    delete_user_watchlist(username)
    logger.info(f"[accounts] 已刪除帳號：{username}")
    return jsonify({"ok": True})
