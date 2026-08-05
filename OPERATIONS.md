# 維運指令速查

適用環境：WSL（Ubuntu-24.04），專案位於 `/mnt/d/Users/Tester_APK.TW/Desktop/eve`
（Windows 端路徑：`D:\Users\Tester_APK.TW\Desktop\eve`）。

以下指令預設在 **WSL 終端機**裡執行。如果你人在 Windows PowerShell / cmd，
在每一段指令外面包一層：

```powershell
wsl.exe bash -lc "這裡放下面的指令"
```

---

## 啟動服務

**一定要用 `setsid ... & disown`，讓程序真正跟終端機分離**，
不然關掉終端機（或某些工具的背景執行機制回收）時會把服務一起殺掉：

```bash
cd /mnt/d/Users/Tester_APK.TW/Desktop/eve
setsid nohup .venv/bin/python webapp.py >> logs/webapp_stdout.log 2>&1 < /dev/null &
disown
```

啟動後確認真的脫離成功（`STAT` 欄要有 `s`，代表是獨立 session leader）：

```bash
ps -o pid,ppid,sid,stat,cmd -C python
```

---

## 停止服務

```bash
pkill -TERM -f "webapp.py"
```

或先查 PID 再單獨關（比較保險，不會誤殺其他 python 程序）：

```bash
ps aux | grep webapp.py | grep -v grep
kill -TERM <PID>
```

等幾秒讓它自己收尾，確認埠號釋放：

```bash
ss -ltnp | grep 5000 || echo "已停止"
```

---

## 重啟服務

= 先「停止」再「啟動」（依序做上面兩段）。重啟前後可以用下面這段比對資料有沒有斷：

```bash
curl -s http://127.0.0.1:5000/api/data | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(d['last_updated'], sum(len(s['plates']) for s in d['results']))"
```

---

## 確認服務目前狀態

```bash
# 程序在不在跑
ps aux | grep webapp.py | grep -v grep

# 埠號有沒有在聽
ss -ltnp | grep 5000

# 對外網址通不通
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5000/
```

---

## 看 log

| 檔案 | 內容 |
|---|---|
| `logs/webapp.log` | 主要應用程式 log（掃描、登入、Email、Discord、錯誤），每天午夜自動輪替，保留 30 天 |
| `logs/webapp_stdout.log` | 標準輸出/錯誤（啟動訊息、沒被 logging 接住的例外） |

```bash
cd /mnt/d/Users/Tester_APK.TW/Desktop/eve

# 即時看
tail -f logs/webapp.log

# 只看錯誤
grep -i "error\|traceback" logs/webapp.log | tail -30

# 看最近 N 筆
tail -n 50 logs/webapp.log
```

沒有先 `cd` 也可以，直接用完整路徑：

```bash
tail -f /mnt/d/Users/Tester_APK.TW/Desktop/eve/logs/webapp.log
```

正常開機應該要看到這幾行（沒有 `ERROR` / `Traceback` 就是好的）：

```
[server] waitress 啟動於 0.0.0.0:5000（8 個執行緒），信任本機反向代理
waitress: Serving on http://0.0.0.0:5000
[stations] 已取得監理站目錄：7 個轄區、共 36 個監理站
[discord] 已登入：車牌競標查詢#6957
[scan] 完成，NN 面號牌競標中，其中 N 面即將截止
```

---

## 對外網址（Cloudflare Quick Tunnel）

目前用 `cloudflared tunnel --url http://127.0.0.1:5000` 這種「臨時通道」，
**網址每次重啟 cloudflared 都會換**，要用下面指令查目前那組：

```bash
grep -o 'https://[a-zA-Z0-9.-]*trycloudflare.com' ~/cloudflared.log | tail -1
```

確認 cloudflared 本身有在跑：

```bash
ps aux | grep cloudflared | grep -v grep
```

**502 Bad Gateway 代表什麼：** cloudflared（通道）還活著，但連不到後面的 Flask
（`webapp.py` 沒在跑，或剛好在重啟中間）。先用上面「確認服務目前狀態」查，
通常是服務掛了，照「啟動服務」那段重開即可，不需要動 cloudflared。

想要固定不變的網址（不用每次重開都換），要改用 Cloudflare 具名 Tunnel
（需要網域＋設定檔），跟現在的臨時模式是兩回事。
