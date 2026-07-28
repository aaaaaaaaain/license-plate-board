# -*- coding: utf-8 -*-
"""SQLite 出價歷史、決標紀錄、個人追蹤清單。"""

import json
import re
import sqlite3
from datetime import datetime

from .paths import HISTORY_DB_PATH, PREV_ACTIVE_PATH


def extract_plate_number(plate):
    """從號牌字串取出結尾數字部分（例如 PJY-1111 -> 1111）。

    同一個好號流標後重新上架，字首字母常會換掉，但大家真正在意、
    持續追蹤的是這個數字本身，所以歷史紀錄用數字＋車種當作同一標的的 key，
    不會因為換字首就被當成不同號牌、歷史斷開。
    """
    m = re.search(r"(\d+)\s*$", plate)
    return m.group(1) if m else plate


def init_history_db():
    conn = sqlite3.connect(HISTORY_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bid_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plate TEXT NOT NULL,
            number_key TEXT NOT NULL,
            section TEXT,
            station TEXT,
            category TEXT,
            price TEXT,
            bid_count TEXT,
            deadline TEXT,
            recorded_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bid_history_number ON bid_history(number_key, category)")

    # 決標紀錄只留數字、金額、時間，不需要字首字母（換字首不影響是不是同一個號碼）
    old_cols = [r[1] for r in conn.execute("PRAGMA table_info(decided_results)").fetchall()]
    if old_cols and "plate" in old_cols:
        conn.execute("DROP TABLE decided_results")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS decided_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number_key TEXT NOT NULL,
            category TEXT,
            section TEXT,
            station TEXT,
            final_price TEXT,
            decided_at TEXT NOT NULL,
            UNIQUE(number_key, category, section, station, decided_at)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decided_number ON decided_results(number_key, category, section, station)")

    # 每個使用者自己收藏想追蹤的號碼。用「數字＋車種」當 key，跟歷史查詢一致，
    # 這樣同一個號碼流標後換字首重新上架，使用者的追蹤不會斷掉。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            number_key TEXT NOT NULL,
            category TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(username, number_key, category)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(username)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_target ON watchlist(number_key, category)")
    conn.commit()
    conn.close()


# 一個帳號最多能收藏幾個號碼——避免有人寫腳本無限塞爆資料庫
MAX_WATCHLIST_PER_USER = 200


def get_watchlist(username):
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT number_key, category, created_at FROM watchlist "
            "WHERE username=? ORDER BY created_at DESC",
            (username,),
        ).fetchall()
    finally:
        conn.close()
    return [{"number_key": r[0], "category": r[1], "created_at": r[2]} for r in rows]


def add_watchlist(username, number_key, category):
    """加入追蹤。回傳 (成功與否, 錯誤訊息)。"""
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM watchlist WHERE username=?", (username,)
        ).fetchone()
        already = conn.execute(
            "SELECT 1 FROM watchlist WHERE username=? AND number_key=? AND category=?",
            (username, number_key, category),
        ).fetchone()
        if not already and count >= MAX_WATCHLIST_PER_USER:
            return False, f"追蹤清單最多只能放 {MAX_WATCHLIST_PER_USER} 個號碼"
        conn.execute(
            "INSERT OR IGNORE INTO watchlist (username, number_key, category, created_at) "
            "VALUES (?, ?, ?, ?)",
            (username, number_key, category, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()
    return True, None


def remove_watchlist(username, number_key, category):
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        conn.execute(
            "DELETE FROM watchlist WHERE username=? AND number_key=? AND category=?",
            (username, number_key, category),
        )
        conn.commit()
    finally:
        conn.close()


def delete_user_watchlist(username):
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        conn.execute("DELETE FROM watchlist WHERE username=?", (username,))
        conn.commit()
    finally:
        conn.close()


def get_watchers(number_key, category):
    """誰在追蹤這個號碼——寄個人化截止提醒時用。"""
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT username FROM watchlist WHERE number_key=? AND category=?",
            (number_key, category),
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def load_prev_active_keys():
    if PREV_ACTIVE_PATH.exists():
        try:
            return {tuple(k) for k in json.loads(PREV_ACTIVE_PATH.read_text(encoding="utf-8"))}
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_prev_active_keys(keys):
    PREV_ACTIVE_PATH.write_text(
        json.dumps([list(k) for k in keys], ensure_ascii=False, indent=2), encoding="utf-8"
    )


# 上一次掃描時，還在「競標中」的 (plate, category, section, station) 組合，存檔在
# data/prev_active.json——原本只放在記憶體裡，程式一重啟這份清單就會被清空，
# 剛好在重啟前後消失的號碼就永遠不會被判定成決標，卡在「曾出現過」但沒有決標
# 紀錄。存檔後就算重啟也還記得上一輪的狀態，不會漏。
PREV_ACTIVE_KEYS = load_prev_active_keys()


def detect_decided(enriched, recorded_at, failed_stations=None):
    """比對這次掃描跟上一次掃描，找出「上次還在、這次不見了」的號碼＝剛決標，記下最後出價當作決標價。

    決標紀錄只記數字＋車種＋監理站＋決標金額＋時間，不記字首字母
    （同一個號碼換字首上架，決標紀錄還是算同一輪）。

    failed_stations 是這一輪抓取失敗的 (轄區, 監理站)（整個轄區失敗時是 (轄區, None)）。
    那些站這次「沒有資料」是因為沒抓到，不是因為號碼結標了，所以不能判成決標，
    而且要把它們留在 PREV_ACTIVE_KEYS 裡，等下一輪抓成功再判——不然這一輪
    先被清掉，下一輪就變成「上次本來就不在」，那批號碼會永遠不會被記錄決標。
    """
    global PREV_ACTIVE_KEYS

    failed_stations = failed_stations or set()

    def is_failed(section, station):
        return (section, station) in failed_stations or (section, None) in failed_stations

    # 用完整號牌（含字首）當 key，不能只用數字——同一個監理站、同車種，
    # 曾經同時出現過兩面尾數相同但字首不同的號牌（例如 PJX-6677 跟 PJY-6677），
    # 只用數字當 key 會把兩面互相蓋掉、憑空少一筆。
    current_keys = set()
    for station in enriched:
        for p in station["plates"]:
            current_keys.add((p["號牌"], p["號牌類別"], station["section"], station["station"]))

    missing_keys = PREV_ACTIVE_KEYS - current_keys
    held_keys = {k for k in missing_keys if is_failed(k[2], k[3])}
    decided_keys = missing_keys - held_keys
    if decided_keys:
        conn = sqlite3.connect(HISTORY_DB_PATH)
        try:
            for key in decided_keys:
                plate, category, section, station_name = key
                number_key = extract_plate_number(plate)
                last = conn.execute(
                    "SELECT price FROM bid_history "
                    "WHERE plate=? AND category=? AND section=? AND station=? "
                    "ORDER BY id DESC LIMIT 1",
                    key,
                ).fetchone()
                if last is None:
                    continue
                (price,) = last
                conn.execute(
                    "INSERT OR IGNORE INTO decided_results "
                    "(number_key, category, section, station, final_price, decided_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (number_key, category, section, station_name, price, recorded_at),
                )
            conn.commit()
        finally:
            conn.close()

    PREV_ACTIVE_KEYS = current_keys | held_keys
    save_prev_active_keys(PREV_ACTIVE_KEYS)


def seed_relist_if_needed(enriched, recorded_at):
    """號碼決標後，如果同一個監理站又重新上架同一個數字，
    先把上次的決標價補成這一輪趨勢圖的起點，這樣新舊兩輪的價格才能接得起來看。

    這裡用完整號牌判斷「是不是剛出現」，但用數字去找上一輪的決標價
    （換字首也找得到），兩種 key 各司其職。
    """
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        for station in enriched:
            for p in station["plates"]:
                plate = p["號牌"]
                category = p["號牌類別"]
                section = station["section"]
                station_name = station["station"]
                key = (plate, category, section, station_name)
                if key in PREV_ACTIVE_KEYS:
                    continue  # 上次掃描就在了，不是剛重新上架
                number_key = extract_plate_number(plate)
                decided = conn.execute(
                    "SELECT final_price, decided_at FROM decided_results "
                    "WHERE number_key=? AND category=? AND section=? AND station=? "
                    "ORDER BY decided_at DESC LIMIT 1",
                    (number_key, category, section, station_name),
                ).fetchone()
                if not decided:
                    continue
                final_price, decided_at = decided
                already_seeded = conn.execute(
                    "SELECT 1 FROM bid_history "
                    "WHERE number_key=? AND category=? AND section=? AND station=? AND recorded_at=? "
                    "LIMIT 1",
                    (number_key, category, section, station_name, decided_at),
                ).fetchone()
                if already_seeded:
                    continue
                conn.execute(
                    "INSERT INTO bid_history "
                    "(plate, number_key, section, station, category, price, bid_count, deadline, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (plate, number_key, section, station_name, category, final_price, "0", "", decided_at),
                )
        conn.commit()
    finally:
        conn.close()


def get_decided_results():
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        rows = conn.execute("""
            SELECT number_key, category, section, station, final_price, decided_at
            FROM decided_results
            ORDER BY decided_at DESC
        """).fetchall()
    finally:
        conn.close()
    return [
        {
            "number_key": r[0], "category": r[1], "section": r[2], "station": r[3],
            "final_price": r[4], "decided_at": r[5],
        }
        for r in rows
    ]


def record_bid_changes(enriched, recorded_at):
    """出價或出價次數跟上一筆紀錄不同才寫入一筆新的，沒變就跳過
    （趨勢圖看的是真實的出價變化，不是固定「一天一筆」——不然同一天內
    價格真的動了好幾次會被壓成一筆，看不出走勢；沒動的話也不用重複記）。

    先一次查出每面號牌「最新一筆紀錄的出價/次數」，再逐面比對是否有變化——
    避免每一面號牌都各自查一次資料庫。
    """
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        last_values = {
            (plate, category, section, station_name): (price, bid_count)
            for plate, category, section, station_name, price, bid_count in conn.execute("""
                SELECT plate, category, section, station, price, bid_count
                FROM (
                    SELECT plate, category, section, station, price, bid_count,
                        ROW_NUMBER() OVER (
                            PARTITION BY plate, category, section, station
                            ORDER BY recorded_at DESC
                        ) AS rn
                    FROM bid_history
                )
                WHERE rn = 1
            """).fetchall()
        }
        for station in enriched:
            for p in station["plates"]:
                plate = p["號牌"]
                category = p["號牌類別"]
                section = station["section"]
                station_name = station["station"]
                price = str(p["目前出價"])
                bid_count = str(p["出價次數"])
                key = (plate, category, section, station_name)
                if last_values.get(key) == (price, bid_count):
                    continue
                number_key = extract_plate_number(plate)
                conn.execute(
                    "INSERT INTO bid_history "
                    "(plate, number_key, section, station, category, price, bid_count, deadline, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (plate, number_key, section, station_name, category,
                     price, bid_count, p["決標時間"], recorded_at),
                )
        conn.commit()
    finally:
        conn.close()


def get_history_summary():
    """列出目前資料庫裡追蹤過的每一面號牌的最新狀態，供歷史查詢頁使用。

    用完整號牌分組（不是只用數字）——同一個監理站、同車種偶爾會同時出現
    尾數相同、字首不同的兩面號牌，只用數字分組會把兩面合併成一筆，憑空少一面。

    每一筆另外附上 days＝這面號牌有出價紀錄的所有日期。歷史查詢頁的日期篩選
    要靠它才會準：只看 last_seen（最後一次更新的日期）的話，一面從 07-26 掛到
    07-28 的號牌，選 07-26 就找不到——但它那天確實在競標。
    """
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        day_rows = conn.execute("""
            SELECT plate, category, section, station, DATE(recorded_at) AS day
            FROM bid_history
            GROUP BY plate, category, section, station, day
            ORDER BY day
        """).fetchall()
        days_map = {}
        for plate, category, section, station, day in day_rows:
            days_map.setdefault((plate, category, section, station), []).append(day)

        rows = conn.execute("""
            SELECT number_key, category, section, station, plate, price, bid_count, recorded_at, cnt
            FROM (
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY plate, category, section, station
                        ORDER BY recorded_at DESC
                    ) AS rn,
                    COUNT(*) OVER (
                        PARTITION BY plate, category, section, station
                    ) AS cnt
                FROM bid_history
            )
            WHERE rn = 1
            ORDER BY recorded_at DESC
        """).fetchall()
    finally:
        conn.close()
    return [
        {
            "number_key": r[0], "category": r[1], "section": r[2], "station": r[3],
            "plate": r[4], "price": r[5], "bid_count": r[6], "last_seen": r[7], "points": r[8],
            "days": days_map.get((r[4], r[1], r[2], r[3]), []),
        }
        for r in rows
    ]


def get_plate_history(plate, category=None, section=None, station=None):
    """回傳 (history, decided)：history 是出價走勢，decided 是這個號碼決標後的最終結果。

    如果決標之後又出現更新的出價紀錄（代表已經重新上架、新一輪還在競標中），
    就不算「已決標」了，decided 會是 None（但舊的決標價還是留在 history 裡當趨勢起點）。

    只給 plate/category（不給 section/station）的話，是照數字＋車種找、不分監理站——
    Discord 查詢指令用的是這種寬鬆比對。但網頁的趨勢圖一定要連 section/station 一起帶，
    否則不同監理站剛好尾數相同的號碼（例如台北的 PJY-8888 跟桃園的 PKL-8888）會被
    誤連成同一條趨勢線，價格看起來像忽然跳漲或跳跌。
    """
    number_key = extract_plate_number(plate)
    conditions = ["number_key=?"]
    params = [number_key]
    if category:
        conditions.append("category=?")
        params.append(category)
    if section:
        conditions.append("section=?")
        params.append(section)
    if station:
        conditions.append("station=?")
        params.append(station)
    where_sql = " AND ".join(conditions)

    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        rows = conn.execute(
            f"SELECT plate, price, bid_count, recorded_at FROM bid_history "
            f"WHERE {where_sql} ORDER BY recorded_at ASC",
            params,
        ).fetchall()
        decided_row = conn.execute(
            f"SELECT final_price, decided_at FROM decided_results "
            f"WHERE {where_sql} ORDER BY decided_at DESC LIMIT 1",
            params,
        ).fetchone()
    finally:
        conn.close()

    history = [{"plate": r[0], "price": r[1], "bid_count": r[2], "recorded_at": r[3]} for r in rows]
    decided = None
    if decided_row:
        final_price, decided_at = decided_row
        has_newer_activity = any(h["recorded_at"] > decided_at for h in history)
        if not has_newer_activity:
            decided = {"final_price": final_price, "decided_at": decided_at}
    return history, decided
