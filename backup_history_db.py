# -*- coding: utf-8 -*-
"""把出價歷史資料庫備份回專案的 data/，每天由 systemd timer（eve-db-backup.timer）跑一次。

資料庫本體在 WSL 本機（見 app/paths.py 的 HISTORY_DB_PATH），只存在 WSL 的虛擬磁碟裡：
distro 被移除或虛擬磁碟壞掉就整份沒了，Windows 那邊平常也看不到。這支把它複製一份到
data/history.db.daily-YYYYMMDD，只保留最新 KEEP 份。

- 用 SQLite 的 backup API，服務在寫入時照樣可以備份，拿到的是一致的快照。
- 先在 ext4 上做好、檢查完，再整個檔案複製到 /mnt/d 後改名：SQLite 不直接在 9p 上寫，
  中途失敗也不會在 data/ 留下半份檔案。
- 只清 history.db.daily-* 這種自動備份；手動留的 history.db.bak-* 一律不碰。
"""

import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime

from app.paths import DAILY_BACKUP_PREFIX, DATA_DIR, HISTORY_DB_PATH

KEEP = 7


def main():
    if not HISTORY_DB_PATH.exists():
        print(f"找不到資料庫：{HISTORY_DB_PATH}", file=sys.stderr)
        return 1

    dest = DATA_DIR / f"{DAILY_BACKUP_PREFIX}{datetime.now():%Y%m%d}"
    with tempfile.TemporaryDirectory(dir=HISTORY_DB_PATH.parent) as tmp:
        snapshot = os.path.join(tmp, "history.db")
        src = sqlite3.connect(f"file:{HISTORY_DB_PATH}?mode=ro", uri=True)
        dst = sqlite3.connect(snapshot)
        try:
            src.backup(dst)
            check = dst.execute("PRAGMA integrity_check").fetchone()[0]
            counts = {t: dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("bid_history", "decided_results")}
        finally:
            dst.close()
            src.close()
        if check != "ok":
            print(f"備份的 integrity_check 失敗：{check}，不寫進 data/", file=sys.stderr)
            return 1

        partial = dest.with_name(dest.name + ".partial")
        shutil.copyfile(snapshot, partial)
        os.replace(partial, dest)

    olds = sorted(DATA_DIR.glob(f"{DAILY_BACKUP_PREFIX}*[0-9]"))
    for old in olds[:-KEEP]:
        old.unlink()

    size_mb = dest.stat().st_size / 1024 / 1024
    print(f"已備份到 {dest}（{size_mb:.1f} MB，bid_history {counts['bid_history']} 筆、"
          f"decided_results {counts['decided_results']} 筆）；刪除舊備份 {len(olds[:-KEEP])} 份")
    return 0


if __name__ == "__main__":
    sys.exit(main())
