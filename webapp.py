# -*- coding: utf-8 -*-
"""台灣監理站車牌競標即時看板（網頁版）

背景每隔 N 分鐘自動掃描全國監理站競標中號牌，
網頁會自動更新，快到期（預設決標前1小時內）的號牌會在頁面上高亮顯示，
並可選擇寄 Email 通知。

用法：
    pip install -r requirements.txt
    python webapp.py
    然後用瀏覽器開啟 http://127.0.0.1:5000

設定：
    編輯 config.json 可調整：
        scan_interval_minutes  背景掃描間隔（分鐘）
        alert_before_minutes   決標前多久算「即將截止」
        email.enabled          是否寄送 Email 通知（true/false）
        email.*                你自己的 SMTP 寄件設定（例如 Gmail 應用程式密碼）

程式碼實際上拆在 app/ 目錄下（config_store／accounts_store／history_db／
auth／notifications／scanner／discord_runner／routes_pages／routes_api／server），
這支檔案只是進入點，保留原本的啟動指令與檔名不變。
"""

from app.server import main

if __name__ == "__main__":
    main()
