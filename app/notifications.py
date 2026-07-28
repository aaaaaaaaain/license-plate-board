# -*- coding: utf-8 -*-
"""Email 寄送與「已通知過」的去重清單。"""

import json
import smtplib
from email.mime.text import MIMEText

from .accounts_store import ACCOUNTS, USERS_LOCK, find_user
from .config_store import CONFIG
from .history_db import extract_plate_number, get_watchers
from .logging_setup import logger
from .paths import ALERTED_PATH


def load_alerted():
    if ALERTED_PATH.exists():
        try:
            return set(json.loads(ALERTED_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_alerted(keys):
    ALERTED_PATH.write_text(json.dumps(sorted(keys), ensure_ascii=False, indent=2), encoding="utf-8")


ALERTED_KEYS = load_alerted()


def _flatten_plates(enriched):
    """把 enriched（依監理站分組）攤平成單一號牌清單，每筆帶著 section/station。"""
    for station in enriched:
        for p in station["plates"]:
            yield {"section": station["section"], "station": station["station"], **p}


def _user_alert_minutes(user):
    """這個帳號自己設定的「即將截止」門檻（分鐘）；沒設定就跟著全站預設值走。"""
    custom = user.get("alert_before_minutes")
    return custom if custom else CONFIG["alert_before_minutes"]


# 提醒分兩階段：剩餘時間跌破門檻先提醒一次，跌破門檻的一半再提醒一次當最後通知。
# 例如門檻設 1 小時，就是決標前 1 小時提醒一次、決標前 30 分再提醒一次——
# 兩階段各自只會觸發一次（各自有自己的去重 key），不會每輪掃描都重複寄。
STAGE_LABELS = {"first": "提醒", "final": "最後提醒"}


def _due_stage(prefix, username, u, threshold_minutes):
    """回傳這面號牌現在該寄的 (階段, 這次要一起標記進 ALERTED_KEYS 的 key 集合)，
    沒有該寄的就回傳 None。

    如果完整門檻跟一半門檻同時跌破（例如剛好在很接近決標時才第一次抓到這面
    號牌），只會回傳「final」、不會兩階段各寄一封——但 first 那個 key 還是會
    一起標記掉，不然下一輪會補寄一封時間點比最後提醒還晚、沒有意義的「提醒」。
    """
    seconds_left = u.get("seconds_left")
    if seconds_left is None or seconds_left < 0:
        return None
    base = f"{prefix}|{username}|{u['section']}|{u['station']}|{u['號牌']}|{u['決標時間']}"
    first_key = f"{base}|first"
    final_key = f"{base}|final"
    if seconds_left <= threshold_minutes * 30 and final_key not in ALERTED_KEYS:  # 門檻的一半
        return "final", {final_key, first_key}
    if seconds_left <= threshold_minutes * 60 and first_key not in ALERTED_KEYS:
        return "first", {first_key}
    return None


def deliver_email(email_cfg, subject, body):
    """實際寄信，失敗時直接拋出例外讓呼叫端決定如何處理。"""
    if not email_cfg.get("to_addrs"):
        raise ValueError("尚未設定收件人 (to_addrs)")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = email_cfg["from_addr"]
    msg["To"] = ", ".join(email_cfg["to_addrs"])
    with smtplib.SMTP(email_cfg["smtp_host"], int(email_cfg["smtp_port"]), timeout=15) as server:
        server.starttls()
        server.login(email_cfg["username"], email_cfg["app_password"])
        server.sendmail(email_cfg["from_addr"], email_cfg["to_addrs"], msg.as_string())


def send_broadcast_alerts(enriched):
    """把「所有」即將截止的號碼（不只是自己追蹤的）廣播給有開啟這個選項的帳號。

    每個帳號的訂閱狀態、收件信箱、即將截止門檻、已通知過的紀錄都各自獨立——
    這裡不再有單一一組「全站共用收件人」或單一門檻，同一面號牌對 A 帳號來說
    可能已經算「即將截止」，對設定門檻比較短的 B 帳號來說可能還沒到。
    """
    email_cfg = CONFIG["email"]
    if not email_cfg.get("enabled") or not email_cfg.get("app_password"):
        return

    with USERS_LOCK:
        recipients = [
            (u["username"], u.get("email", ""), _user_alert_minutes(u))
            for u in ACCOUNTS
            if u.get("broadcast_enabled") and u.get("email") and u.get("role") != "pending"
        ]
    if not recipients:
        return

    plates = list(_flatten_plates(enriched))

    for username, recipient, threshold_minutes in recipients:
        by_stage = {"first": [], "final": []}
        keys_by_stage = {"first": set(), "final": set()}
        for u in plates:
            due = _due_stage("broadcast", username, u, threshold_minutes)
            if due is None:
                continue
            stage, keys = due
            mins_left = int(u["seconds_left"] // 60)
            by_stage[stage].append(
                f"【{u['section']} {u['station']}】號牌 {u['號牌']}（{u['號牌類別']}）"
                f" 目前出價 {u['目前出價']}，決標時間 {u['決標時間']}，剩餘約 {mins_left} 分鐘"
            )
            keys_by_stage[stage].update(keys)

        for stage, lines in by_stage.items():
            if not lines:
                continue
            label = STAGE_LABELS[stage]
            try:
                deliver_email(
                    {**email_cfg, "to_addrs": [recipient]},
                    subject=f"[車牌競標警示] {label}：{len(lines)} 面號牌即將截止",
                    body="以下號牌即將截止競標：\n\n" + "\n".join(lines),
                )
            except Exception as e:
                # 寄失敗就不要記成「已通知」，下一輪掃描會再試一次
                logger.error(f"[email] 寄廣播{label}給 {username} 失敗：{e}")
                continue
            logger.info(f"[email] 已寄出廣播{label}給 {username}（{len(lines)} 面）")
            ALERTED_KEYS.update(keys_by_stage[stage])
            save_alerted(ALERTED_KEYS)


def send_watchlist_alerts(enriched):
    """把即將截止的號碼，分別寄給有追蹤它的使用者自己的信箱。

    用的是超級帳號設定的那組 SMTP 憑證寄出，但收件人換成各使用者註冊時填的 Email，
    所以使用者不會、也不需要接觸到寄件憑證。「即將截止」的門檻也是每個帳號自己的，
    沒自訂就跟著全站預設值走。
    """
    email_cfg = CONFIG["email"]
    if not email_cfg.get("enabled") or not email_cfg.get("app_password"):
        return

    per_user = {}   # username -> {"first": [描述], "final": [描述]}
    new_keys = {}    # username -> {"first": {key}, "final": {key}}
    for u in _flatten_plates(enriched):
        number_key = extract_plate_number(u["號牌"])
        category = u["號牌類別"]
        watchers = get_watchers(number_key, category)
        if not watchers:
            continue
        for username in watchers:
            with USERS_LOCK:
                watcher = find_user(username)
            if not watcher:
                continue
            threshold_minutes = _user_alert_minutes(watcher)
            due = _due_stage("watch", username, u, threshold_minutes)
            if due is None:
                continue
            stage, keys = due
            mins_left = int(u["seconds_left"] // 60)
            per_user.setdefault(username, {"first": [], "final": []})[stage].append(
                f"【{u['section']} {u['station']}】號牌 {u['號牌']}（{category}）"
                f" 目前出價 {u['目前出價']}，決標時間 {u['決標時間']}，剩餘約 {mins_left} 分鐘"
            )
            new_keys.setdefault(username, {"first": set(), "final": set()})[stage].update(keys)

    for username, by_stage in per_user.items():
        with USERS_LOCK:
            user = find_user(username)
            recipient = user.get("email", "") if user else ""
            role = user.get("role") if user else None
            # 舊帳號沒有這個欄位，預設視為要收提醒
            wants_notify = bool(user.get("notify_enabled", True)) if user else False
        # 帳號被刪掉、還沒核准、自己關掉提醒、或沒填信箱的都跳過
        if not recipient or role == "pending" or not wants_notify:
            continue
        for stage, lines in by_stage.items():
            if not lines:
                continue
            label = STAGE_LABELS[stage]
            try:
                deliver_email(
                    {**email_cfg, "to_addrs": [recipient]},
                    subject=f"[車牌競標追蹤] {label}：你追蹤的 {len(lines)} 面號牌即將截止",
                    body="你追蹤的號牌即將截止競標：\n\n" + "\n".join(lines),
                )
            except Exception as e:
                # 寄失敗就不要記成「已通知」，下一輪掃描會再試一次
                logger.error(f"[email] 寄追蹤{label}給 {username} 失敗：{e}")
                continue
            logger.info(f"[email] 已寄出追蹤{label}給 {username}（{len(lines)} 面）")
            ALERTED_KEYS.update(new_keys[username][stage])

    if per_user:
        save_alerted(ALERTED_KEYS)
