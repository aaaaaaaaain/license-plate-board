# -*- coding: utf-8 -*-
"""背景掃描迴圈：抓競標資料、判斷即將截止、觸發通知、寫入歷史。"""

import json
import threading
from datetime import datetime, timedelta

from plate_bid_scanner import get_all_sections_and_stations, scan

from .config_store import CONFIG
from .history_db import detect_decided, record_bid_changes, seed_relist_if_needed
from .logging_setup import logger
from .notifications import send_broadcast_alerts, send_watchlist_alerts
from .paths import LATEST_PATH

STATE_LOCK = threading.Lock()
STATE = {
    "results": [],
    "last_updated": None,
    "last_error": None,
    "scanning": False,
}

# 「立即重新整理畫面」按鈕用來提早叫醒背景迴圈的訊號、以及最短間隔——
# 讓使用者能主動要求馬上重掃一次，又不會因為連續按而對監理服務網送出重疊的掃描。
SCAN_NOW_EVENT = threading.Event()
MANUAL_SCAN_COOLDOWN_SECONDS = 30

# 兩輪掃描之間至少要隔這麼久，避免掃描耗時超過設定間隔時變成連續打監理服務網
MIN_GAP_SECONDS = 30


def trigger_scan_now():
    """回傳 (是否成功觸發, 錯誤訊息)。

    跟背景迴圈共用同一個掃描函式、同一個執行緒——這裡只是把原本在等待
    下一輪排定掃描的 time.sleep 提早叫醒，不會跟排定的掃描重疊執行，
    也不會因為使用者一直按就真的同時多開一輪掃描去打監理服務網。
    """
    with STATE_LOCK:
        if STATE["scanning"]:
            return False, "目前正在掃描中，請稍候"
        last_updated = STATE["last_updated"]
    if last_updated:
        elapsed = (datetime.now() - datetime.fromisoformat(last_updated)).total_seconds()
        if elapsed < MANUAL_SCAN_COOLDOWN_SECONDS:
            wait = int(MANUAL_SCAN_COOLDOWN_SECONDS - elapsed) + 1
            return False, f"剛掃描過，請 {wait} 秒後再試"
    SCAN_NOW_EVENT.set()
    return True, None

# 全國轄區／監理站目錄快取，給網頁篩選選單用。用 list[:] 就地覆寫（而不是重新綁定名稱），
# 這樣其他模組用 `from .scanner import STATIONS_CACHE` 拿到的參照才會跟著看到更新後的內容。
STATIONS_CACHE = []


def refresh_stations_cache():
    """抓一次全國轄區／監理站目錄（不含競標資料），給網頁篩選選單用。失敗就沿用舊快取。"""
    try:
        fresh = get_all_sections_and_stations()
        STATIONS_CACHE[:] = fresh
        total = sum(len(s["stations"]) for s in STATIONS_CACHE)
        logger.info(f"[stations] 已取得監理站目錄：{len(STATIONS_CACHE)} 個轄區、共 {total} 個監理站")
    except Exception as e:
        logger.error(f"[stations] 取得監理站目錄失敗：{e}")


def parse_deadline(text):
    """把「1150713 11:00:00」（民國年月日+時間）轉成 datetime，失敗回傳 None。"""
    try:
        date_part, time_part = text.split()
        roc_year = int(date_part[:3])
        month = int(date_part[3:5])
        day = int(date_part[5:7])
        year = roc_year + 1911
        hour, minute, second = (int(x) for x in time_part.split(":"))
        return datetime(year, month, day, hour, minute, second)
    except (ValueError, IndexError):
        return None


def build_enriched_results(raw_results, alert_before_minutes):
    """替每個號牌加上剩餘秒數與是否即將截止，回傳新結構＋即將截止清單。"""
    now = datetime.now()
    threshold = timedelta(minutes=alert_before_minutes)
    enriched = []
    urgent_list = []

    for item in raw_results:
        plates = []
        for p in item["plates"]:
            deadline_dt = parse_deadline(p["決標時間"])
            seconds_left = None
            is_urgent = False
            if deadline_dt:
                seconds_left = (deadline_dt - now).total_seconds()
                is_urgent = 0 <= seconds_left <= threshold.total_seconds()
            plate_row = dict(p)
            plate_row["seconds_left"] = seconds_left
            plate_row["is_urgent"] = is_urgent
            plates.append(plate_row)
            if is_urgent:
                urgent_list.append({
                    "section": item["section"],
                    "station": item["station"],
                    **plate_row,
                })
        enriched.append({
            "section": item["section"],
            "station": item["station"],
            "plates": plates,
        })

    return enriched, urgent_list


def run_scan_once():
    alert_before = CONFIG["alert_before_minutes"]
    started = datetime.now()

    with STATE_LOCK:
        STATE["scanning"] = True

    try:
        raw_results, failed_stations = scan(delay=0.5)
        enriched, urgent_list = build_enriched_results(raw_results, alert_before)
        if failed_stations:
            names = "、".join(st or f"{sec}（整個轄區）" for sec, st in sorted(failed_stations, key=str))
            logger.warning(f"[scan] 有 {len(failed_stations)} 個監理站這輪沒抓到：{names}"
                           f"——這些站的號碼這輪不判定決標，等下一輪抓成功再判")

        # 廣播：寄給有開啟「全站廣播」的帳號自己的信箱；追蹤：依各使用者的追蹤清單分別通知。
        # 兩邊的訂閱狀態、收件信箱、即將截止門檻、已通知紀錄都各自獨立，所以要把完整的
        # enriched（含每面號牌的 seconds_left）傳過去，讓各自依自己的門檻篩選，
        # 不能只傳這裡用全站預設值篩出來的 urgent_list。
        send_broadcast_alerts(enriched)
        send_watchlist_alerts(enriched)

        with STATE_LOCK:
            STATE["results"] = enriched
            STATE["last_updated"] = datetime.now().isoformat(timespec="seconds")
            STATE["last_error"] = None

        seed_relist_if_needed(enriched, STATE["last_updated"])
        record_bid_changes(enriched, STATE["last_updated"])
        detect_decided(enriched, STATE["last_updated"], failed_stations)

        LATEST_PATH.write_text(
            json.dumps({"results": enriched, "last_updated": STATE["last_updated"]},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"[scan] 完成，{sum(len(i['plates']) for i in enriched)} 面號牌競標中，"
                    f"其中 {len(urgent_list)} 面即將截止，"
                    f"耗時 {(datetime.now() - started).total_seconds():.1f} 秒")

    except Exception as e:
        with STATE_LOCK:
            STATE["last_error"] = str(e)
        logger.error(f"[scan] 失敗：{e}")
    finally:
        with STATE_LOCK:
            STATE["scanning"] = False


def background_loop():
    # 啟動時先讀舊快取墊檔，讓網頁一開始就有資料可看
    if LATEST_PATH.exists():
        try:
            cached = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
            with STATE_LOCK:
                STATE["results"] = cached.get("results", [])
                STATE["last_updated"] = cached.get("last_updated")
        except (json.JSONDecodeError, OSError):
            pass

    while True:
        started = datetime.now()
        run_scan_once()
        interval = max(1, CONFIG["scan_interval_minutes"]) * 60

        # 下一輪的時刻用牆上時鐘算，不能用 time.monotonic()。這台機器的
        # CLOCK_MONOTONIC 比實際時間慢約 7%（實測：sleep 20 秒，Windows 那邊
        # 過了 21.5 秒；CLOCK_MONOTONIC_RAW 也是 21.4 秒，所以慢的是校正後的
        # monotonic、不是硬體），直接把秒數交給 Event.wait 會讓每輪間隔被同樣
        # 拉長——設 3 分鐘、log 上量到 199 秒就是這麼來的。牆上時鐘由 Hyper-V
        # 每半分鐘跟 Windows 對時，是準的那一個。
        #
        # 扣掉這輪掃描自己花掉的時間，設定的間隔才是真的「每 N 分鐘一輪」；
        # 萬一掃描比間隔還久，也至少留 MIN_GAP_SECONDS 不要接著又打一輪。
        now = datetime.now()
        deadline = max(started + timedelta(seconds=interval),
                       now + timedelta(seconds=MIN_GAP_SECONDS))
        # 分段等：每段重看一次時鐘，兩個時鐘差多少都會自己修正回來。
        # 「立即重新整理畫面」照樣能在任何一段中途把它叫醒。
        while True:
            remaining = (deadline - datetime.now()).total_seconds()
            if remaining <= 0 or SCAN_NOW_EVENT.wait(timeout=min(remaining, 15)):
                break
        SCAN_NOW_EVENT.clear()
