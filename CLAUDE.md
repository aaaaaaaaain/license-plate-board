# CLAUDE.md

台灣監理站車牌競標即時看板：背景每幾分鐘掃描 mvdis 的競標清單，存成出價歷史，
用網頁看板、Email 提醒、Discord bot 三種方式呈現。維運指令一律看
[OPERATIONS.md](OPERATIONS.md)，那份是唯一來源，這裡不重複。

## 改完程式後怎麼生效

跑在 WSL 的 systemd 使用者服務（`eve-webapp`、`eve-tunnel`），**不要**用
`python webapp.py` 另外開一支，會跟服務搶 5000 埠。

| 改了什麼 | 要做什麼 |
|---|---|
| `*.py`、`templates/*.html` | `systemctl --user restart eve-webapp` |
| `static/*`（CSS／JS／圖片） | 瀏覽器 Ctrl+F5 就好 |
| `config.json` | 重啟（`CONFIG` 在啟動時讀進記憶體） |

樣板一定要重啟才會換：waitress 不是開發模式，Jinja 樣板第一次載入後就被快取，
只重新整理瀏覽器看到的還是舊版。

```bash
wsl -e bash -lc 'cd /mnt/d/Users/Tester_APK.TW/Desktop/license-plate-board && systemctl --user restart eve-webapp && sleep 15 && tail -3 logs/webapp.log'
```

改完後看 log 的最後三行確認：`waitress 啟動於`、`已登入：車牌競標查詢`、`已同步 N 個斜線指令`。

**停止服務時 `pkill` 要寫成 `pkill -TERM -f "[w]ebapp[.]py"`**，而且停止和啟動要分兩次
執行。`-f` 比對整條命令列，`wsl -e bash -lc "...webapp.py..."` 這條命令列本身就含
`webapp.py`，沒加方括號會把自己殺掉（exit 15）；就算加了方括號，同一條命令列後面
接啟動指令又會出現字面的 `webapp.py`，一樣自殺。

**WSL 要有人掛著才不會被收掉。** 最後一個連線關掉時，Windows 端會把整個 distro
關機，服務跟著停（`loginctl enable-linger` 擋不住，它管的是登出、不是 distro 關機）。
啟動資料夾的 `start-wsl-eve.vbs` 固定掛一個 `wsl -e sleep infinity`。確認方式：

```bash
wsl -e bash -lc 'ps -eo cmd | grep "[s]leep infinity"'
```

那支 vbs 必須是純 ASCII：WSH 用系統 ANSI 讀 `.vbs`，UTF-8 中文註解會把行尾換行
吃掉，害下一行程式被併進註解。

## 來源網站的限制

這些限制決定了資料能做到什麼，改任何跟掃描、歷史、決標有關的東西前先讀：

- **只公布「目前出價」和「出價次數」兩個當下的數字**，沒有出價明細。兩次掃描之間
  發生的出價，金額永遠拿不到——3 分鐘間隔實測仍有約 44% 的出價次數是跳過的。
  熱門號碼常在截止前幾分鐘連喊好幾口，再縮短間隔也補不回來。
- **決標時間會被延後**：截止前有人出價就往後推 3 分鐘（防搶標），同一面號牌一天
  被延好幾次很常見。提醒的去重 key 含決標時間，所以延一次就會重寄一次提醒。
- **會整批回傳 0 筆而且 HTTP 不報錯**（維護時段）。這不是「全部結標」，見下面的
  決標判定。
- **清單分頁**，每頁 10 筆，`plate_bid_scanner.scan()` 會自己抓完所有頁。

## 資料規則

改 `app/history_db.py` 前務必理解這幾條，它們都是踩過坑之後定下來的：

- **`number_key` 是號牌結尾的數字**（`extract_plate_number`）。好號流標後重新上架
  常換字首，大家追蹤的是數字，所以歷史、追蹤清單、決標紀錄都用「數字＋車種」當
  身分；但 `bid_history` 仍用完整號牌＋轄區＋監理站分組，因為同站同車種曾同時出現
  尾數相同、字首不同的兩面。
- **`record_bid_changes` 只在出價或次數變動時寫一列**。所以資料庫裡不會有連續重複
  的快照，任何「合併重複紀錄」的邏輯都是多餘的，而且會弄丟真實金額。
- **決標判定有三層保護**（`detect_decided`）：抓取失敗的站不判、單輪消失超過一半的
  號牌不判（整批消失＝網站維護）、`PREV_ACTIVE_KEYS` 存檔到 `data/prev_active.json`
  所以重啟不會產生判定盲區。少了任何一層就會寫進假決標，而決標紀錄是寫進資料庫、
  事後要人工清的。
- **`seed_relist_if_needed`** 在號碼決標後又重新上架時，補一列 `bid_count=0`、價格是
  上一輪決標價的種子列當新一輪趨勢起點。看到 `第 0 次` 的紀錄就是它。

## 趨勢圖的兩份實作要一致

`static/history-modal.js`（網頁 SVG）和 `app/chart_image.py`（Discord PNG）畫的是同一
張圖，改規則要兩邊一起改：

- 橫軸是**出價次數**，而且點的位置依次數等比例——中間有幾次沒抓到，那段就該比較寬。
- 流標重新上架時次數從頭數，累加上一輪的次數當偏移量，讓新一輪接在舊的右邊。
- 所有紀錄都停在同一次出價時退回照筆數平均分佈，否則每個點會疊在同一個 x。
- 價格全部相同時，用價格的一成當縱軸範圍並置中，不然格線會擠成幾乎一樣的數字。

## Discord bot

- **任何會阻塞的呼叫都要走 `off_loop`**（`asyncio.to_thread`）。資料庫在 `/mnt/d`
  走 9p，單次查詢約 1.1 秒，而 Discord 只給互動 3 秒回應時間；自動完成更是每打一個
  字查一次。直接寫在 async 函式裡會塞住事件迴圈，所有指令卡在「命令發送中」。
- 選單最多 25 個選項、訊息上限 2000 字，分頁每頁筆數要留在這兩個範圍內。
- 指令改名或改參數要重啟才會同步，Discord 端還要再等幾分鐘才看得到。
- 狀態列只放數字：個人資料卡大約只顯示 30 個字，接網址會被截成「...」。

## 網頁

- 配色由 `static/theme.js` 在繪製前寫進 `data-theme`（自動／淺色／深色），CSS 只認
  `:root[data-theme="dark"]`，深色那組變數只維護一份。任何需要判斷深淺的 JS 要讀
  `document.documentElement.dataset.theme`，讀 `prefers-color-scheme` 會跟切換鈕不同步。
- iOS 加入主畫面吃的是 `apple-touch-icon`（PNG、不透明底），SVG favicon 它不認。

## 機密與版控

- `config.json`（Gmail 應用程式密碼、Discord token、session 金鑰）、`accounts.json`、
  `data/`、`logs/` 都在 `.gitignore` 裡，提交前確認暫存區沒有它們。
- 提交訊息用英文標題＋詳細內文，寫清楚「為什麼」和「驗證了什麼」，結尾加
  `Co-Authored-By`。看 `git log` 就是範例。
- GitHub 上的 `main` 是每個版本一個快照提交（`v1.0`、`v2.0` 標籤），本地 `master`
  保留完整開發歷史，兩邊歷史獨立。發版時從 `origin/main` 開分支、`git read-tree -u
  --reset master` 套用目前程式碼、提交成 `V2.x: ...`，不要 force push。

## 驗證

這個專案的慣例是**拿真實資料驗證再說做完了**，提交訊息裡也要寫出驗證數字：

- 後端改動：用 `.venv/bin/python -c` 直接呼叫函式跑真實資料庫，或打 `/api/...`。
- 前端改動：用瀏覽器工具讀 DOM／SVG 節點的實際值，不要只看截圖。
- 資料相關的判斷：先用 SQL 統計佐證，再下結論（例如「跳過幾次出價」「幾筆重複」）。
