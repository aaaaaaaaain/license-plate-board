# -*- coding: utf-8 -*-
"""啟動 Discord bot（獨立執行緒，共用掃描結果與歷史查詢）。"""

import threading

from plate_bid_scanner import CATEGORIES

from .config_store import CONFIG
from .history_db import get_plate_history
from .logging_setup import logger
from .scanner import STATE, STATE_LOCK


def start_discord_bot():
    discord_cfg = CONFIG["discord"]
    if not discord_cfg.get("enabled") or not discord_cfg.get("bot_token"):
        return

    def get_state():
        with STATE_LOCK:
            return {"results": STATE["results"], "last_updated": STATE["last_updated"]}

    def runner():
        from discord_bot import build_bot
        bot = build_bot(get_state, get_plate_history, categories=CATEGORIES, prefix=discord_cfg.get("command_prefix") or "!")
        try:
            bot.run(discord_cfg["bot_token"])
        except Exception as e:
            logger.error(f"[discord] 啟動失敗：{e}")

    threading.Thread(target=runner, daemon=True).start()
