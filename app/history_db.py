# -*- coding: utf-8 -*-
"""SQLite 出價歷史、決標紀錄、個人追蹤清單。"""

import json
import re
import sqlite3
from bisect import bisect_right
from datetime import datetime

from .logging_setup import logger
from .paths import HISTORY_DB_PATH, PREV_ACTIVE_PATH


def extract_plate_number(plate):
    """從號牌字串取出結尾數字部分（例如 PJY-1111 -> 1111）。

    存成 number_key 只是為了「打 8888 找得到所有 8888」和判斷號碼型態，不是身分。
    身分一律是「轄區＋監理站＋車種＋完整號牌（字首＋數字）」：PKC-8888 和 PLY-8888
    是兩面不同的號牌、各自喊價，混在一起看會把上一面的成交價當成這一面的行情。
    """
    m = re.search(r"(\d+)\s*$", plate)
    return m.group(1) if m else plate


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


# 決標紀錄的表結構。搬遷函式和 init 兩邊都要建，寫成一份才不會漂掉。
# plate 允許 NULL：舊資料回推不出字首的那幾列只能留空。
_DECIDED_DDL = """
    CREATE TABLE IF NOT EXISTS decided_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plate TEXT,
        number_key TEXT NOT NULL,
        category TEXT,
        section TEXT,
        station TEXT,
        final_price TEXT,
        decided_at TEXT NOT NULL,
        UNIQUE(number_key, category, section, station, decided_at, plate)
    )
"""


def _migrate_decided_add_plate(conn):
    """舊的決標紀錄只有數字，補上完整號牌。

    字首從 bid_history 回推，三步由準到不準：
      1. 決標時間就是那面號牌最後一列出價紀錄的時間（4340b39 之後的寫法），
         所以「最後一次出價剛好等於決標時間」而且只有一面對得上的就是它。
      2. 整組（數字＋車種＋轄區＋監理站）從頭到尾只有一面號牌，那就是那面。
      3. 取決標時間之前最後一次出現、時間最接近的那面——4340b39 以前
         decided_at 記的是「發現它不見的那一輪」，會比最後一次出價晚一個掃描間隔。
    三步都推不出來就留 NULL。寧可空著，也不要掛一個猜的字首上去：決標紀錄是
    事後要人工看的東西，錯的字首比沒有字首更難發現。
    """
    spans = {}
    for nk, cat, sec, stn, plate, last in conn.execute(
        "SELECT number_key, category, section, station, plate, MAX(recorded_at) "
        "FROM bid_history GROUP BY number_key, category, section, station, plate"
    ):
        spans.setdefault((nk, cat, sec, stn), []).append((last, plate))

    def guess(nk, cat, sec, stn, decided_at):
        candidates = spans.get((nk, cat, sec, stn))
        if not candidates:
            return None
        exact = [plate for last, plate in candidates if last == decided_at]
        if len(exact) == 1:
            return exact[0]
        if exact:
            return None  # 兩面同時結標，分不出哪一列是哪一面
        if len(candidates) == 1:
            return candidates[0][1]
        before = [(last, plate) for last, plate in candidates if last <= decided_at]
        if not before:
            return None
        newest = max(last for last, _ in before)
        names = [plate for last, plate in before if last == newest]
        return names[0] if len(names) == 1 else None

    rows = conn.execute(
        "SELECT id, number_key, category, section, station, final_price, decided_at "
        "FROM decided_results"
    ).fetchall()
    conn.execute("DROP TABLE decided_results")
    conn.execute(_DECIDED_DDL)
    filled = 0
    for rid, nk, cat, sec, stn, price, decided_at in rows:
        plate = guess(nk, cat, sec, stn, decided_at)
        filled += plate is not None
        conn.execute(
            "INSERT INTO decided_results "
            "(id, plate, number_key, category, section, station, final_price, decided_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, plate, nk, cat, sec, stn, price, decided_at),
        )
    conn.commit()
    logger.info(
        f"[migrate] 決標紀錄加上完整號牌：{len(rows)} 列，回填 {filled} 列，"
        f"{len(rows) - filled} 列推不出字首留空"
    )


_WATCHLIST_DDL = """
    CREATE TABLE IF NOT EXISTS {name} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        plate TEXT NOT NULL,
        number_key TEXT NOT NULL,
        category TEXT NOT NULL,
        section TEXT NOT NULL,
        station TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(username, plate, category, section, station)
    )
"""


def _migrate_watchlist_add_station(conn):
    """追蹤清單補上轄區＋監理站。舊表只存「完整號牌＋車種」，從出價歷史找那面號牌
    最後出現的站補上——追蹤只能從看板上點，被追蹤的號牌一定有出價紀錄。
    找不到的（理論上不會有）就不搬，記在 log，不猜一個站掛上去。
    搬遷時實測同號牌同車種出現在兩個站的組數是 0，所以「最後出現的站」不會選錯。
    """
    conn.execute("DROP TABLE IF EXISTS watchlist_v3")
    conn.execute(_WATCHLIST_DDL.format(name="watchlist_v3"))
    rows = conn.execute("SELECT username, plate, number_key, category, created_at FROM watchlist").fetchall()
    moved = 0
    for username, plate, number_key, category, created_at in rows:
        where = conn.execute(
            "SELECT section, station FROM bid_history WHERE plate=? AND category=? "
            "ORDER BY recorded_at DESC, id DESC LIMIT 1",
            (plate, category),
        ).fetchone()
        if not where:
            logger.warning(f"[migrate] 追蹤清單 {username} 的 {plate}（{category}）找不到所在監理站，不搬")
            continue
        conn.execute(
            "INSERT OR IGNORE INTO watchlist_v3 "
            "(username, plate, number_key, category, section, station, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (username, plate, number_key, category, where[0], where[1], created_at),
        )
        moved += 1
    conn.execute("DROP TABLE watchlist")
    conn.execute("ALTER TABLE watchlist_v3 RENAME TO watchlist")
    logger.info(f"[migrate] 追蹤清單補上轄區＋監理站：{len(rows)} 列，搬過去 {moved} 列")


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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bid_history_identity "
                 "ON bid_history(plate, category, section, station)")

    # 決標紀錄連完整號牌一起存，身分跟出價歷史一樣是「轄區＋監理站＋車種＋完整號牌」。
    # 只留數字的話，同一個監理站、同車種同時上架兩面尾數相同、字首不同的號牌
    # （桃園 2026-08-06 的 CDR-1688 和 CFG-1688）各自結標後，兩列長得一模一樣。
    # UNIQUE 也含 plate：不含的話，兩面同時結標（最後一次出價落在同一輪）只會寫進
    # 一列，另一面被 INSERT OR IGNORE 默默吃掉。
    old_cols = [r[1] for r in conn.execute("PRAGMA table_info(decided_results)").fetchall()]
    if old_cols and "plate" not in old_cols:
        _migrate_decided_add_plate(conn)
    conn.execute(_DECIDED_DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decided_number ON decided_results(number_key, category, section, station)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decided_identity "
                 "ON decided_results(plate, category, section, station)")

    # 每個使用者自己收藏想追蹤的號牌，身分跟出價歷史一樣是「轄區＋監理站＋車種＋完整號牌」：
    # 追蹤了哪一面就只提醒那一面。number_key 只是順便存著，不參與比對。
    # 搬遷分兩代：最舊的表只有數字（沒有 plate）→ 整張丟掉重建；V2.7 以前有 plate
    # 但沒有轄區／監理站 → 從出價歷史補上那面號牌所在的站（見 _migrate_watchlist_add_station）。
    old_watch_cols = [r[1] for r in conn.execute("PRAGMA table_info(watchlist)").fetchall()]
    if old_watch_cols and "plate" not in old_watch_cols:
        conn.execute("DROP TABLE watchlist")
    elif old_watch_cols and "station" not in old_watch_cols:
        _migrate_watchlist_add_station(conn)
    conn.execute(_WATCHLIST_DDL.format(name="watchlist"))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(username)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_identity "
                 "ON watchlist(plate, category, section, station)")
    conn.commit()
    conn.close()


# 一個帳號最多能收藏幾個號碼——避免有人寫腳本無限塞爆資料庫
MAX_WATCHLIST_PER_USER = 200


def get_watchlist(username):
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT plate, number_key, category, section, station, created_at FROM watchlist "
            "WHERE username=? ORDER BY created_at DESC",
            (username,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"plate": r[0], "number_key": r[1], "category": r[2], "section": r[3],
         "station": r[4], "created_at": r[5]}
        for r in rows
    ]


def add_watchlist(username, plate, category, section, station):
    """加入追蹤。身分是完整號牌（例如 PJY-8888）＋車種＋轄區＋監理站。回傳 (成功與否, 錯誤訊息)。"""
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM watchlist WHERE username=?", (username,)
        ).fetchone()
        already = conn.execute(
            "SELECT 1 FROM watchlist WHERE username=? AND plate=? AND category=? AND section=? AND station=?",
            (username, plate, category, section, station),
        ).fetchone()
        if not already and count >= MAX_WATCHLIST_PER_USER:
            return False, f"追蹤清單最多只能放 {MAX_WATCHLIST_PER_USER} 個號碼"
        conn.execute(
            "INSERT OR IGNORE INTO watchlist "
            "(username, plate, number_key, category, section, station, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                username,
                plate,
                extract_plate_number(plate),
                category,
                section,
                station,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return True, None


def remove_watchlist(username, plate, category, section, station):
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        conn.execute(
            "DELETE FROM watchlist WHERE username=? AND plate=? AND category=? AND section=? AND station=?",
            (username, plate, category, section, station),
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


def get_watchers(plate, category, section, station):
    """誰在追蹤這面號牌——寄個人化截止提醒時用。比對轄區＋監理站＋車種＋完整號牌，
    所以追蹤 PJY-8888 的人不會收到 BSA-8888 的提醒，也不會收到別站同號牌的提醒。"""
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT username FROM watchlist WHERE plate=? AND category=? AND section=? AND station=?",
            (plate, category, section, station),
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

# 整批消失時要撐幾輪才認定是真的結標（掃描間隔 5 分鐘＋一輪約 1 分鐘，3 輪約 18 分鐘）。
# 撐得夠久才蓋得住維護時段，又不會讓真的結標拖太久才記錄。
MASS_MISSING_HOLD_ROUNDS = 3
_MASS_MISSING_SINCE = None
_MASS_MISSING_ROUNDS = 0


def detect_decided(enriched, recorded_at, failed_stations=None):
    """比對這次掃描跟上一次掃描，找出「上次還在、這次不見了」的號碼＝剛決標，記下最後出價當作決標價。

    決標紀錄記完整號牌＋車種＋轄區＋監理站＋決標金額＋時間，跟出價歷史同一個身分。

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

    # 整批消失有兩種可能，而且當下分不出來：
    #   維護——2026-08-01 01:44 連續兩輪回傳 0 面（HTTP 沒報錯，不算抓取失敗），
    #         315 面被誤判成決標，13 分鐘後全部回來。
    #   真的整批結標——同一個梯次的號牌截止時間一樣，時間到就一起消失，
    #         2026-08-07 10:42 全國 698 面就是這樣，一小時後網站仍然是空的。
    # 用「撐幾輪」分辨：先按住不判，連續 MASS_MISSING_HOLD_ROUNDS 輪都還是空的
    # 就當作真的結標。決標時間用第一輪消失的時間，不是接受的那一輪，
    # 這樣紀錄的時間才貼近實際截止時間。
    global _MASS_MISSING_SINCE, _MASS_MISSING_ROUNDS
    if PREV_ACTIVE_KEYS and len(current_keys) < len(PREV_ACTIVE_KEYS) * 0.5:
        _MASS_MISSING_ROUNDS += 1
        if _MASS_MISSING_SINCE is None:
            _MASS_MISSING_SINCE = recorded_at
        if _MASS_MISSING_ROUNDS < MASS_MISSING_HOLD_ROUNDS:
            logger.warning(
                f"[decided] 這輪只剩 {len(current_keys)} 面（上一輪 {len(PREV_ACTIVE_KEYS)} 面）"
                f"——先不判定決標，連續 {MASS_MISSING_HOLD_ROUNDS} 輪都這樣才視為真的整批結標"
                f"（目前第 {_MASS_MISSING_ROUNDS} 輪）"
            )
            # 按住不判，但這輪新看到的號牌還是要收進基準。少了這一步，在按住期間
            # 才上架、又在按住結束前就結標的號牌從頭到尾不在基準裡，missing_keys
            # 永遠不會包含它們，於是決標紀錄一直沒寫、歷史頁把它們永遠列在
            # 「競標中」（2026-08-21 一次清出 14 面這種幽靈）。
            # 用聯集而不是覆寫：基準本身要保留，不然就等於把整批消失的號牌
            # 直接放生，那正是按住這幾輪要防的事。
            PREV_ACTIVE_KEYS |= current_keys
            save_prev_active_keys(PREV_ACTIVE_KEYS)
            return
        logger.warning(
            f"[decided] 已經連續 {_MASS_MISSING_ROUNDS} 輪只剩 {len(current_keys)} 面，"
            f"視為真的整批結標（第一輪消失是 {_MASS_MISSING_SINCE}）"
        )

    _MASS_MISSING_SINCE = None
    _MASS_MISSING_ROUNDS = 0

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
                    "SELECT price, recorded_at, deadline FROM bid_history "
                    "WHERE plate=? AND category=? AND section=? AND station=? "
                    "ORDER BY id DESC LIMIT 1",
                    key,
                ).fetchone()
                if last is None:
                    continue
                price, last_seen, deadline = last
                # 決標時間記「最後一次看到這面號牌」的時間，不是我們發現它不見的時間。
                # 兩者都在一個掃描間隔內，但最後一次看到的時間不受重啟、也不受
                # 整批消失要按住幾輪影響——按住三輪才確認的話，用發現時間會晚快 20 分鐘。
                conn.execute(
                    "INSERT OR IGNORE INTO decided_results "
                    "(plate, number_key, category, section, station, final_price, decided_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (plate, number_key, category, section, station_name, price,
                     last_seen or recorded_at),
                )
                # 結標了就從所有人的追蹤清單拿掉：看板只列競標中的號牌，留著的話
                # ★ 不會再出現、使用者也取消不了，只會默默佔掉 200 個名額。
                # 同一面號牌之後流標重新上架，要重新追蹤（使用者選的取捨）。
                # 但只在決標時間已經過了才拿：還沒到截止就從清單消失的，是掃描時短暫
                # 不見（RGC-1725 在 10-20 被判決標、10-22 才截止，現在還在競標），
                # 拿掉的話使用者的追蹤就這樣不見了。
                deadline_dt = parse_deadline(deadline or "")
                if deadline_dt and deadline_dt > datetime.fromisoformat(recorded_at):
                    logger.warning(f"[decided] {plate}（{section} {station_name}）還沒到決標時間 "
                                   f"{deadline} 就不見了，不從追蹤清單移除")
                    continue
                removed = conn.execute(
                    "DELETE FROM watchlist WHERE plate=? AND category=? AND section=? AND station=?",
                    key,
                ).rowcount
                if removed:
                    logger.info(f"[decided] {plate}（{section} {station_name}）已決標，"
                                f"從 {removed} 份追蹤清單移除")
            conn.commit()
        finally:
            conn.close()

    PREV_ACTIVE_KEYS = current_keys | held_keys
    save_prev_active_keys(PREV_ACTIVE_KEYS)


def get_decided_results():
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        rows = conn.execute("""
            SELECT number_key, category, section, station, final_price, decided_at, plate
            FROM decided_results
            ORDER BY decided_at DESC
        """).fetchall()
        bids = conn.execute(
            "SELECT plate, category, section, station, bid_count, deadline, recorded_at "
            "FROM bid_history ORDER BY recorded_at ASC, id ASC"
        ).fetchall()
    finally:
        conn.close()
    # 每筆決標是哪一輪的：取決標當下最後一筆出價的 round，跟看板、趨勢圖同一份換輪規則。
    # round > 0 就是流標後重新上架那一輪的決標，標「（重新開標）」，不然歷史頁會出現兩列
    # 一模一樣的號牌，看起來像重複紀錄（PJY-8888 07-28 22000、10-15 16000）。
    # 不能用「前面有沒有別筆決標」判斷：63 筆有前一筆決標的裡面 39 筆跟前一筆是同一輪，
    # 前一筆是掃描時短暫消失被誤判的假決標。
    histories = {}
    for plate, category, section, station, bid_count, deadline, recorded_at in bids:
        histories.setdefault((plate, category, section, station), []).append(
            {"bid_count": bid_count, "deadline": deadline or "", "recorded_at": recorded_at})
    for history in histories.values():
        _tag_rounds(history)
    relisted = []
    for r in rows:
        history = histories.get((r[6], r[1], r[2], r[3]), [])
        i = bisect_right([h["recorded_at"] for h in history], r[5]) if history else 0
        relisted.append(i > 0 and history[i - 1]["round"] > 0)
    return [
        {
            "number_key": r[0], "category": r[1], "section": r[2], "station": r[3],
            "final_price": r[4], "decided_at": r[5], "plate": r[6],
            "relisted": relisted[i],
        }
        for i, r in enumerate(rows)
    ]


def record_bid_changes(enriched, recorded_at):
    """出價、出價次數或決標時間跟上一筆紀錄不同才寫入一筆新的，沒變就跳過
    （趨勢圖看的是真實的出價變化，不是固定「一天一筆」——不然同一天內
    價格真的動了好幾次會被壓成一筆，看不出走勢；沒動的話也不用重複記）。

    先一次查出每面號牌「最新一筆紀錄的出價/次數」，再逐面比對是否有變化——
    避免每一面號牌都各自查一次資料庫。

    寫入後要同步更新 last_values，不能只在函式一開始查一次：分頁抓取在清單
    被同時大量增修時可能把同一面號牌重複收進同一次 enriched（2026-08-06 那批
    108 面好字號集體上架時就發生過，一面重複到 25 次）。last_values 沒跟著更新
    的話，同一次掃描裡第 2 筆以後比對到的還是掃描前的舊值，會被誤判成「又變了」
    而重複寫入。
    """
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        # 決標時間也要比：沒人出價（停在第 1 次、底價）的號牌流標後重新上架，價格和
        # 次數都跟上一輪最後一筆一樣，只有決標時間換成新的日期。不比的話新一輪一列都
        # 不會寫，_tag_rounds 分不出換輪、看板也標不出「重新開標」。防搶標延長一定伴隨
        # 出價，本來就會寫，所以這裡不會多寫延長造成的列。
        last_values = {
            (plate, category, section, station_name): (price, bid_count, deadline or "")
            for plate, category, section, station_name, price, bid_count, deadline in conn.execute("""
                SELECT plate, category, section, station, price, bid_count, deadline
                FROM (
                    SELECT plate, category, section, station, price, bid_count, deadline,
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
                deadline = p["決標時間"] or ""
                key = (plate, category, section, station_name)
                if last_values.get(key) == (price, bid_count, deadline):
                    continue
                number_key = extract_plate_number(plate)
                conn.execute(
                    "INSERT INTO bid_history "
                    "(plate, number_key, section, station, category, price, bid_count, deadline, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (plate, number_key, section, station_name, category,
                     price, bid_count, deadline, recorded_at),
                )
                last_values[key] = (price, bid_count, deadline)
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


def _tag_rounds(history):
    """幫每一筆紀錄標上 round（第幾輪，從 0 起算），網頁和 Discord 的趨勢圖都只畫最大的那輪，
    看板的「重新開標」也是看這個（round > 0）。

    同一面號牌（同一個身分）也會流標後重新上架，出價次數從頭數起、價格回到底價，
    跟上一輪畫在同一張圖上看起來像崩盤。換輪的判斷：
      - 出價次數比上一筆少（重新數起）；或
      - 決標時間換了一天。防搶標延長只會往後推幾分鐘、不會跨日，換日期就是新的一場。
        上一輪沒人出價（停在第 1 次）又重新上架時次數不會變少，只能靠這條分。
    不拿「兩筆之間夾著一次決標」當依據：2026-10-21 實測那樣切出的 39 個斷點，決標時間
    全都沒換日期——是號牌掃描時短暫消失被誤判成決標（例如 RGC-1725），不是重新開標。
    在這裡算一次、隨資料送出去，兩份畫圖程式就不用各自維護一份換輪規則。
    """
    rnd = 0
    prev = None
    for h in history:
        if prev is not None:
            try:
                restarted = int(h["bid_count"] or 0) < int(prev["bid_count"] or 0)
            except (TypeError, ValueError):
                restarted = False
            # 決標時間是「1151022 11:00:00」，前 7 碼是民國年月日；舊資料有幾列是空的就不比
            new_day = bool(h["deadline"] and prev["deadline"]
                           and h["deadline"][:7] != prev["deadline"][:7])
            if restarted or new_day:
                rnd += 1
        h["round"] = rnd
        prev = h
    return history


def _load_histories(where_sql, params):
    """照條件撈出出價紀錄和決標紀錄，依身分（完整號牌＋車種＋轄區＋監理站）分開，
    回傳 {身分: (history, decided)}，照最後一筆紀錄的時間由舊到新排。

    固定兩次查詢，不管符合幾面號牌：Discord 只打數字可能符合好幾十面，一面查一次
    會超過互動的 3 秒期限（資料庫還在 /mnt/d 走 9p 時就是這樣；搬到 ext4 後單次快很多，
    但查詢次數照樣不要跟著符合的面數長）。
    """
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        rows = conn.execute(
            f"SELECT plate, price, bid_count, recorded_at, category, section, station, deadline "
            f"FROM bid_history WHERE {where_sql} ORDER BY recorded_at ASC, id ASC",
            params,
        ).fetchall()
        decided_rows = conn.execute(
            f"SELECT plate, category, section, station, final_price, decided_at "
            f"FROM decided_results WHERE {where_sql} ORDER BY decided_at ASC",
            params,
        ).fetchall()
    finally:
        conn.close()

    histories = {}
    for r in rows:
        histories.setdefault((r[0], r[4], r[5], r[6]), []).append({
            "plate": r[0], "price": r[1], "bid_count": r[2], "recorded_at": r[3],
            "category": r[4], "section": r[5], "station": r[6], "deadline": r[7] or "",
        })
    decided_by = {}
    for plate, category, section, station, final_price, decided_at in decided_rows:
        decided_by.setdefault((plate, category, section, station), []).append(
            {"final_price": final_price, "decided_at": decided_at, "plate": plate})

    result = {}
    for identity, history in sorted(histories.items(), key=lambda kv: kv[1][-1]["recorded_at"]):
        decided_list = decided_by.get(identity, [])
        _tag_rounds(history)
        decided = None
        # 同一面號牌決標之後又有更新的出價紀錄＝流標後重新上架、還在競標，不算已決標
        if decided_list and not any(h["recorded_at"] > decided_list[-1]["decided_at"] for h in history):
            decided = decided_list[-1]
        result[identity] = (history, decided)
    return result


def get_relisted_identities(identities):
    """這幾面號牌裡，哪些是「重新開標」：同一面號牌流標後重新上架、現在是第 2 輪以上。

    換輪規則跟趨勢圖同一份（_tag_rounds），看板上標「重新開標」的號牌，點開趨勢圖
    一定也是重新開始的那一輪。identities 是 (完整號牌, 車種, 轄區, 監理站) 的集合，
    回傳其中最新一筆 round > 0 的那些。分批查，避免一次塞太多參數給 SQLite。
    """
    identities = list(identities)
    relisted = set()
    for i in range(0, len(identities), 200):
        chunk = identities[i:i + 200]
        where = ("(plate, category, section, station) IN (VALUES "
                 + ", ".join(["(?, ?, ?, ?)"] * len(chunk)) + ")")
        found = _load_histories(where, [v for ident in chunk for v in ident])
        relisted |= {ident for ident, (history, _) in found.items() if history and history[-1]["round"] > 0}
    return relisted


def get_plate_history(plate, category, section, station):
    """回傳一面號牌的 (history, decided)：history 是出價紀錄（舊到新），decided 是最近一次決標。

    身分是「轄區＋監理站＋車種＋完整號牌」，四個都要給、一律精確比對。以前是用
    數字＋車種查，PKC-8888、PGT-8888、PLY-8888 三面號牌會被接成同一條線，
    每一面從底價起標的地方都畫成崩盤（2026-10-21 板橋 8888）。
    """
    identity = (plate, category, section, station)
    found = _load_histories("plate=? AND category=? AND section=? AND station=?", identity)
    return found.get(identity, ([], None))


def search_plate_histories(keyword, category=None, station=None):
    """Discord 的 /歷史 用：打完整號牌（PJY-1111）或只打數字（1111），每一面號牌分開回傳。

    回傳 [(身分, history, decided), ...]，最近有紀錄的排最後
    （Discord 分頁預設翻到最後一頁，最新的那面剛好在眼前）。
    """
    keyword = (keyword or "").strip().upper()
    if not keyword:
        return []
    conditions = ["plate=?" if re.search(r"[A-Z]", keyword) else "number_key=?"]
    params = [keyword]
    if category:
        conditions.append("category=?")
        params.append(category)
    if station:
        conditions.append("station=?")
        params.append(station)
    found = _load_histories(" AND ".join(conditions), params)
    return [(identity, history, decided) for identity, (history, decided) in found.items()]


def get_history_filters():
    """回傳歷史紀錄裡實際出現過的監理站與車種，給 Discord 的自動完成選單用。

    刻意不用「目前競標中」的清單：歷史查的是過去，某個監理站今天沒有標的
    不代表它沒有歷史紀錄，拿現況當選單會讓那些站永遠選不到。
    只是兩個 DISTINCT，每次按鍵都查也還好。
    """
    conn = sqlite3.connect(HISTORY_DB_PATH)
    try:
        stations = [r[0] for r in conn.execute(
            "SELECT DISTINCT station FROM bid_history WHERE station IS NOT NULL AND station != '' ORDER BY station"
        ).fetchall()]
        categories = [r[0] for r in conn.execute(
            "SELECT DISTINCT category FROM bid_history WHERE category IS NOT NULL AND category != '' ORDER BY category"
        ).fetchall()]
    finally:
        conn.close()
    return {"stations": stations, "categories": categories}
