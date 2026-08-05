# -*- coding: utf-8 -*-
"""取得目前對外的公開網址。

用的是 Cloudflare 的 quick tunnel，網址每次重啟都會換一組隨機的，
沒有固定值可以寫死在設定檔裡，所以直接從 cloudflared 的 log 撈最後一組——
tunnel 重開後 log 會再追加一行新的，下次讀就是新網址，不用手動改任何東西。

config.json 裡可以覆寫：
    public_url        直接指定固定網址（之後換成具名 tunnel 或自架網域時用）
    public_url_log    cloudflared 的 log 路徑（預設 ~/cloudflared.log）
"""

import os
import re
from pathlib import Path

from .config_store import CONFIG

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
DEFAULT_LOG = Path.home() / "cloudflared.log"

# log 會一直長大，每次都整份讀太浪費；記住上次讀的時間戳，沒變就直接回快取
_cache = {"mtime": None, "url": None}


def get_public_url():
    """回傳目前的對外網址（字串），拿不到就回 None。"""
    fixed = (CONFIG.get("public_url") or "").strip()
    if fixed:
        return fixed

    log_path = Path(CONFIG.get("public_url_log") or DEFAULT_LOG)
    try:
        mtime = log_path.stat().st_mtime
    except OSError:
        return None
    if _cache["mtime"] == mtime:
        return _cache["url"]

    try:
        # 只讀檔案尾端：網址那行是 cloudflared 剛啟動時印的，但之後的連線訊息會把它
        # 推遠，所以尾端抓大一點（256KB），還是比整份讀進來省。
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 262144))
            text = f.read().decode("utf-8", "ignore")
    except OSError:
        return None

    found = URL_RE.findall(text)
    _cache["mtime"] = mtime
    _cache["url"] = found[-1] if found else None
    return _cache["url"]
