# -*- coding: utf-8 -*-
"""建立 Flask app、掛上路由，以及程式進入點 main()。"""

import gzip
import threading
from datetime import timedelta

from flask import Flask, request

from . import logging_setup  # noqa: F401  # 要在其他模組開始記 log 之前先設定好 handler
from .accounts_store import ACCOUNTS  # noqa: F401  # 觸發載入，讓啟動流程跟舊版一致
from .config_store import CONFIG
from .discord_runner import start_discord_bot
from .history_db import init_history_db
from .logging_setup import logger
from .paths import BASE_DIR
from .scanner import background_loop, refresh_stations_cache

init_history_db()

# app 現在是從 app/server.py 建立的，Flask 預設會照 __name__ 去找 app/templates、
# app/static——但 templates/、static/ 其實放在專案根目錄，所以要明講路徑。
app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)
app.secret_key = CONFIG["auth"]["secret_key"]
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

# session cookie 加固：擋掉 JavaScript 讀取、擋掉跨站帶 cookie 的請求，
# 並在確定走 HTTPS 時禁止 cookie 用明文連線送出。
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(CONFIG["server"].get("https_only")),
)

if CONFIG["server"].get("behind_proxy"):
    # 只在真的有反向代理時才開——直接對外時開這個等於讓任何人偽造 X-Forwarded-For，
    # 登入失敗紀錄裡的來源 IP 就沒有意義了
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# 會壓縮的型別：只挑文字類。圖片（PNG／JPEG）本身已經壓過，再壓一次只是白花 CPU。
_GZIP_TYPES = {
    "application/json",
    "application/javascript",
    "text/javascript",
    "text/html",
    "text/css",
    "text/plain",
    "image/svg+xml",
}
# 太小的回應壓了反而變大（gzip 自己就有十幾個位元組的標頭），也不值得那個 CPU
_GZIP_MIN_BYTES = 1024


@app.after_request
def _compress(response):
    """把文字類回應壓成 gzip。

    歷史頁一次要抓 /api/history-list 加 /api/decided-list，未壓縮是 2.87 MB；
    這兩份是重複性極高的 JSON，壓完只剩約 120 KB。本機沒感覺，但這個站是走
    cloudflared 對外的，手機網路差很多。waitress 不會自己做這件事。
    """
    ctype = (response.content_type or "").split(";")[0].strip().lower()
    if ctype not in _GZIP_TYPES:
        return response

    # 不管這次有沒有真的壓，只要這個網址「可能」被壓就要標 Vary，否則中間的快取
    # 有機會把壓過的內容餵給不支援 gzip 的客戶端。已經有 Vary（例如 Cookie）就接上去。
    vary = response.headers.get("Vary", "")
    if "accept-encoding" not in vary.lower():
        response.headers["Vary"] = f"{vary}, Accept-Encoding" if vary else "Accept-Encoding"

    if "gzip" not in request.headers.get("Accept-Encoding", "").lower():
        return response
    # direct_passthrough 是 send_file 那種還沒讀進記憶體的回應（static 檔案就是），
    # 這時候 get_data() 會直接丟例外；有 ETag 的也跳過，改了內容 ETag 就對不上，
    # 會弄壞 304。static 檔案本來就有 ETag 快取，少壓這幾十 KB 不影響。
    if (response.direct_passthrough
            or response.status_code < 200 or response.status_code >= 300
            or "Content-Encoding" in response.headers
            or "ETag" in response.headers):
        return response

    data = response.get_data()
    if len(data) < _GZIP_MIN_BYTES:
        return response
    packed = gzip.compress(data, 6)
    if len(packed) >= len(data):
        return response
    response.set_data(packed)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(packed))
    return response


@app.after_request
def _set_security_headers(response):
    # 基本瀏覽器端防護：不要用 MIME 猜測型別、不要被嵌進別人的頁面用 iframe 點擊劫持、
    # 跳到別的網站時不要把完整網址（可能含 session 相關資訊）當 referrer 帶過去。
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


from . import routes_api, routes_pages  # noqa: E402  # 要在 app 建好之後才能掛 blueprint

app.register_blueprint(routes_pages.bp)
app.register_blueprint(routes_api.bp)


def main():
    thread = threading.Thread(target=background_loop, daemon=True)
    thread.start()
    threading.Thread(target=refresh_stations_cache, daemon=True).start()
    start_discord_bot()

    server_cfg = CONFIG["server"]
    host, port = CONFIG["web_host"], CONFIG["web_port"]

    # 背景掃描結果、登入失敗次數這些狀態都存在記憶體裡，所以只能單一程序＋多執行緒，
    # 不能用 gunicorn 那種多 worker 的跑法（每個 worker 會各自掃描、各自算失敗次數）。
    if str(server_cfg.get("mode", "waitress")).lower() == "dev":
        logger.warning("[server] 使用 Flask 開發伺服器──僅供本機測試，請勿對外公開")
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
    else:
        from waitress import serve

        threads = max(1, int(server_cfg.get("threads", 8)))
        serve_kwargs = {}
        if server_cfg.get("behind_proxy"):
            # waitress 預設會把 X-Forwarded-* 標頭整組清掉（clear_untrusted_proxy_headers），
            # 不明講信任來源的話，前面的 ProxyFix 根本收不到標頭。
            # 只信任從 127.0.0.1 進來的連線——cloudflared 正是從本機連進來的，
            # 而區網上直接連 0.0.0.0:5000 的人偽造 X-Forwarded-For 不會被採信。
            serve_kwargs.update(
                trusted_proxy="127.0.0.1",
                trusted_proxy_count=1,
                trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"},
                clear_untrusted_proxy_headers=True,
            )
        logger.info(f"[server] waitress 啟動於 {host}:{port}（{threads} 個執行緒）"
                    f"{'，信任本機反向代理' if serve_kwargs else ''}")
        serve(app, host=host, port=port, threads=threads, ident=None, **serve_kwargs)
