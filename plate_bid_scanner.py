# -*- coding: utf-8 -*-
"""
台灣監理服務網（MVDIS）車牌競標掃描核心邏輯。

掃描全國各監理站的「競標中號牌查詢」頁面，列出目前正在進行網路競標的
監理站與號牌明細。給 webapp.py 匯入使用，本身不是可獨立執行的程式。

資料來源：https://www.mvdis.gov.tw/m3-emv-plate/bid/queryBiding
（公開查詢頁面，不需登入/憑證）
"""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

BASE_URL = "https://www.mvdis.gov.tw/m3-emv-plate/bid/queryBiding"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": BASE_URL,
}

# 監理服務網「號牌標售公告」頁面 plateType 篩選欄位使用的車種名稱列表
CATEGORIES = [
    "自用小客貨車", "租賃小客貨車",
    "大型重機車(550cc以上)", "大型重機車(550cc以下)",
    "普通重型機車", "普通輕型機車",
    "電動550cc以下重機", "電動550cc以上重機",
    "電動普通重型機車", "電動普通輕型機車", "電動小型輕型機車",
    "營業小客車", "營業小客車(民營市區客運)", "營業小貨車",
    "自用大客車", "自用大貨車", "營業大客車", "營業大貨車",
    "營業貨櫃曳引", "遊覽大客車", "交通車", "租賃大客貨車",
    "身障自用小客貨車", "自用拖車", "營業拖車",
    "電動自小客", "電動租賃車", "電動小營客車", "電動大營客車",
    "古董機車", "古董汽車", "微型電動二輪車", "電動身障自用小客貨",
]

CSRF_RE = re.compile(r'name="CSRFToken"\s+value="([^"]+)"')
SELECT_RE_TMPL = r'<select name="{name}"[^>]*>(.*?)</select>'
OPTION_RE = re.compile(r'<option value="([^"]+)"[^>]*>\s*([^<]*?)\s*</option>', re.S)
TBODY_RE = re.compile(r'<tbody>(.*?)</tbody>', re.S)
ROW_RE = re.compile(r'<tr class="(?:odd|even)">(.*?)</tr>', re.S)
TD_RE = re.compile(r'<td[^>]*>(.*?)</td>', re.S)
GRID_ID_RE = re.compile(r'd-(\d+)-p=\d+')
TOTAL_PAGES_RE = re.compile(r'name="total"\s+id="total"\s+type="hidden"\s+value="(\d+)"')

COLUMNS = ["號牌", "號牌類別", "底價", "目前出價", "出價次數", "決標時間"]


def clean_cell(raw):
    text = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", text).strip()


def extract_csrf(html):
    m = CSRF_RE.search(html)
    if not m:
        raise RuntimeError("找不到 CSRFToken，頁面格式可能已變更")
    return m.group(1)


def extract_select_options(html, field_name, skip_values=("_", "__")):
    block_re = re.compile(SELECT_RE_TMPL.format(name=field_name), re.S)
    m = block_re.search(html)
    if not m:
        return []
    options = []
    for value, label in OPTION_RE.findall(m.group(1)):
        if value in skip_values:
            continue
        options.append((value, label.strip()))
    return options


def extract_bidding_rows(html):
    tbody_m = TBODY_RE.search(html)
    if not tbody_m:
        return []
    rows = []
    for row_html in ROW_RE.findall(tbody_m.group(1)):
        cells = [clean_cell(c) for c in TD_RE.findall(row_html)]
        if len(cells) == len(COLUMNS):
            rows.append(dict(zip(COLUMNS, cells)))
    return rows


def extract_total_pages(html):
    """某些監理站的競標清單會分好幾頁，回傳總頁數（找不到分頁資訊就當作只有 1 頁）。"""
    m = TOTAL_PAGES_RE.search(html)
    return int(m.group(1)) if m else 1


def extract_grid_id(html):
    """分頁連結長得像 d-5481-p=2，這裡取出那組數字 id，後面自己組其他頁的查詢參數用。"""
    m = GRID_ID_RE.search(html)
    return m.group(1) if m else None


# 同一站第 2 頁以後的分頁請求彼此獨立——不帶 CSRFToken，每次都自己指定
# sectionCode/stationCode——所以可以並行抓。實測臺北市區監理所的 37 頁：
# 序列 22.6 秒、4 執行緒 1.4 秒。上限刻意留在個位數，這是公開查詢頁面，
# 不是拿來壓測的。
PAGE_WORKERS = 4


def fetch_extra_pages(session, section_code, station_code, grid_id, total_pages):
    """並行抓第 2 頁到最後一頁，回傳依頁序接起來的資料列。

    任何一頁失敗都把例外往外丟，讓呼叫端把整站標成 failed。這裡絕對不能
    「漏一頁就當作那頁沒號牌」：少掉的號牌在 detect_decided 眼裡跟決標消失
    長得一模一樣，會寫進假的決標紀錄，而決標紀錄要人工清。
    """
    local = threading.local()

    def fetch(page_num):
        if not hasattr(local, "session"):
            # requests.Session 沒有保證多執行緒安全，所以每個工作執行緒自己一份，
            # 但沿用主 session 的 cookie（伺服器認的是那組 session cookie）
            local.session = requests.Session()
            local.session.cookies.update(session.cookies)
        resp = local.session.get(BASE_URL, params={
            f"d-{grid_id}-p": str(page_num),
            "stationCode": station_code,
            "onChangeItem": "2",
            "method": "doChangeStation",
            "sectionCode": section_code,
        }, headers=HEADERS, timeout=15)
        resp.encoding = "utf-8"
        return extract_bidding_rows(resp.text)

    pages = list(range(2, total_pages + 1))
    with ThreadPoolExecutor(max_workers=min(PAGE_WORKERS, len(pages))) as pool:
        # map 會照頁序回傳，任何一頁的例外在這裡展開時就會丟出來
        return [row for rows in pool.map(fetch, pages) for row in rows]


def post(session, data):
    resp = session.post(BASE_URL, data=data, headers=HEADERS, timeout=15)
    resp.encoding = "utf-8"
    return resp.text


def get_all_sections_and_stations(delay=0.3):
    """回傳全國所有轄區＋監理站的目錄清單（不含競標資料，只查詢站名，比完整掃描快很多）。

    給網頁的地區／監理站篩選選單用，這樣就算某個監理站目前沒有任何競標中的
    號牌，也能在選單裡看到、選得到。
    回傳：[{"section": 轄區名, "stations": [監理站名, ...]}, ...]
    """
    session = requests.Session()
    resp = session.get(BASE_URL, headers=HEADERS, timeout=15)
    resp.encoding = "utf-8"
    html = resp.text
    token = extract_csrf(html)
    sections = extract_select_options(html, "sectionCode")

    result = []
    for section_code, section_name in sections:
        html = post(session, {
            "onChangeItem": "1",
            "method": "doChangeStation",
            "sectionCode": section_code,
            "stationCode": "__",
            "CSRFToken": token,
        })
        token = extract_csrf(html)
        stations = extract_select_options(html, "stationCode")
        result.append({
            "section": section_name,
            "stations": [name for _, name in stations],
        })
        time.sleep(delay)

    return result


def scan(delay=0.6, categories=None, on_station_done=None):
    """
    掃描全國監理站的競標中號牌。

    delay: 每查完一個轄區／監理站之間等待的秒數。同一站的分頁是並行抓的
        （見 fetch_extra_pages），不套用這個間隔。
    categories: 要保留的車種名稱集合（None 或空集合＝不篩選，全部保留）。
    on_station_done(section_name, station_name, rows): 每查完一站就會被呼叫一次，
        方便呼叫端即時顯示進度，rows 已依 categories 篩選過。

    回傳 (results, failed)：
        results = [{"section":..., "station":..., "plates":[...]}, ...]（只包含有結果的站）
        failed  = {(轄區, 監理站), ...}；整個轄區就失敗時記成 (轄區, None)。

    單一監理站（或整個轄區）抓取失敗時不再讓整輪掃描中斷，改成記進 failed 繼續掃
    其他站。呼叫端一定要看 failed——失敗的站在 results 裡會長得跟「這站現在沒有任何
    競標號牌」一模一樣，如果直接拿來比對「上次還在、這次不見了＝已決標」，那一站的
    號碼會被整批誤判成決標，而且決標紀錄是寫進資料庫、之後補不回來的。
    """
    category_filter = set(categories) if categories else None

    session = requests.Session()
    resp = session.get(BASE_URL, headers=HEADERS, timeout=15)
    resp.encoding = "utf-8"
    html = resp.text
    token = extract_csrf(html)
    sections = extract_select_options(html, "sectionCode")

    results = []
    failed = set()

    for section_code, section_name in sections:
        try:
            html = post(session, {
                "onChangeItem": "1",
                "method": "doChangeStation",
                "sectionCode": section_code,
                "stationCode": "__",
                "CSRFToken": token,
            })
            token = extract_csrf(html)
            stations = extract_select_options(html, "stationCode")
        except (requests.RequestException, RuntimeError):
            # 連監理站清單都拿不到，這個轄區底下有哪些站都不知道，只能整個轄區標成失敗
            failed.add((section_name, None))
            continue
        time.sleep(delay)

        for station_code, station_name in stations:
            try:
                html = post(session, {
                    "onChangeItem": "2",
                    "method": "doChangeStation",
                    "sectionCode": section_code,
                    "stationCode": station_code,
                    "CSRFToken": token,
                })
                token = extract_csrf(html)
                rows = extract_bidding_rows(html)

                total_pages = extract_total_pages(html)
                grid_id = extract_grid_id(html)
                if grid_id and total_pages > 1:
                    rows.extend(fetch_extra_pages(
                        session, section_code, station_code, grid_id, total_pages))
            except (requests.RequestException, RuntimeError):
                failed.add((section_name, station_name))
                time.sleep(delay)
                continue

            if category_filter:
                rows = [r for r in rows if r["號牌類別"] in category_filter]
            time.sleep(delay)

            if on_station_done:
                on_station_done(section_name, station_name, rows)

            if rows:
                results.append({
                    "section": section_name,
                    "station": station_name,
                    "plates": rows,
                })

    return results, failed
