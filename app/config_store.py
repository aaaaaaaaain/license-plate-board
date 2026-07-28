# -*- coding: utf-8 -*-
"""程式設定（掃描間隔、Email/Discord/伺服器參數）的讀寫。"""

import json
import secrets

from .paths import CONFIG_PATH

DEFAULT_CONFIG = {
    "scan_interval_minutes": 10,
    "alert_before_minutes": 60,
    "web_host": "127.0.0.1",
    "web_port": 5000,
    "login_fail_limit": 3,
    "login_lockout_minutes": 15,
    "server": {
        # waitress = 正式用的 WSGI 伺服器；dev = Flask 內建開發伺服器（只該在本機測試時用）
        "mode": "waitress",
        "threads": 8,
        # 掛在 Cloudflare Tunnel／nginx 之類的反向代理後面時要開，
        # 這樣才讀得到真正的來源 IP，而不是一律記成 127.0.0.1
        "behind_proxy": False,
        # 開了之後 session cookie 只會透過 HTTPS 傳送——確定整條路徑都是 HTTPS 才開，
        # 否則用 http:// 連進來會一直登不進去
        "https_only": False,
    },
    "email": {
        "enabled": False,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "username": "",
        "app_password": "",
        "from_addr": "",
    },
    "discord": {
        "enabled": False,
        "bot_token": "",
        "command_prefix": "!",
    },
    # 帳號本身存在另一個檔案 accounts.json，這裡只留伺服器自己用的簽章金鑰
    "auth": {
        "secret_key": "",
    },
}


def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    else:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    merged.update(cfg)
    merged["email"] = {**DEFAULT_CONFIG["email"], **cfg.get("email", {})}
    merged["discord"] = {**DEFAULT_CONFIG["discord"], **cfg.get("discord", {})}
    merged["auth"] = {**DEFAULT_CONFIG["auth"], **cfg.get("auth", {})}
    merged["server"] = {**DEFAULT_CONFIG["server"], **cfg.get("server", {})}
    if not merged["auth"]["secret_key"]:
        merged["auth"]["secret_key"] = secrets.token_hex(32)
        save_config(merged)
    return merged


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


CONFIG = load_config()
