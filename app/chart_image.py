# -*- coding: utf-8 -*-
"""把出價歷史畫成一張 PNG，給 Discord 直接貼圖用。

Discord 只認得 PNG／JPG 這類點陣圖，網頁那張趨勢圖是 SVG，貼過去只會變成
一個要另外點開的附件。這裡用 Pillow 重畫一張，規則跟網頁版
（static/history-modal.js）一致：橫軸是「第幾次出價」而且依次數等比例，
縱軸是價格，每一筆抓到的紀錄都是一個點。
"""

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# WSL 底下直接借 Windows 的字型檔，不用另外在 Linux 裡裝中文字型。
# 找不到就退回 Pillow 內建點陣字（中文會變方框，但至少不會整個炸掉）。
FONT_CANDIDATES = [
    "/mnt/c/Windows/Fonts/msjh.ttc",      # 微軟正黑體
    "/mnt/c/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msjh.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]

WIDTH, HEIGHT = 900, 520
PAD_LEFT, PAD_RIGHT, PAD_TOP, PAD_BOTTOM = 110, 40, 110, 70

BG = (28, 28, 30)
FG = (255, 255, 255)
DIM = (152, 152, 157)
GRID = (58, 58, 60)
ACCENT = (10, 132, 255)
GREEN = (48, 209, 88)


def _font(size, bold=False):
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                # msjh.ttc 的 index 1 是 Bold
                return ImageFont.truetype(path, size, index=1 if bold else 0)
            except OSError:
                continue
    return ImageFont.load_default()


def _x_positions(history):
    """橫軸座標＝累計出價次數，跟網頁版同一套算法。

    流標後換字首重新上架時出價次數會從頭數起，累加上一輪的次數當偏移量，
    新一輪才會接在舊的右邊，而不是把線拉回頭。
    """
    xs = []
    offset = 0
    prev = None
    for h in history:
        try:
            bid = int(h.get("bid_count") or 0)
        except (TypeError, ValueError):
            bid = 0
        if prev is not None and bid < prev:
            offset += prev + 1
        xs.append(offset + bid)
        prev = bid
    return xs


def render_history_png(plate, category, section, station, history, decided=None):
    """回傳 PNG 的 bytes；history 是 get_plate_history 的第一個回傳值。"""
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    d = ImageDraw.Draw(img)

    f_title = _font(30, bold=True)
    f_sub = _font(19)
    f_tick = _font(16)
    f_label = _font(22, bold=True)

    d.text((36, 30), f"號碼 {plate}（{category}）", font=f_title, fill=FG)
    d.text((36, 70), f"{section or ''} {station or ''}".strip(), font=f_sub, fill=DIM)

    if not history:
        d.text((36, 200), "尚無出價紀錄", font=f_sub, fill=DIM)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    prices = [int(h.get("price") or 0) if str(h.get("price") or "").isdigit() else 0 for h in history]
    plot_w = WIDTH - PAD_LEFT - PAD_RIGHT
    plot_h = HEIGHT - PAD_TOP - PAD_BOTTOM

    # 只有一筆、或每筆金額都一樣時上下範圍會是 0，改用價格的一成當範圍，
    # 並把這個唯一的價格擺在圖的正中間（不然點會黏在底線上）。網頁版同樣處理。
    min_p, max_p = min(prices), max(prices)
    flat = max_p == min_p
    range_p = max(round(max_p * 0.1), 100) if flat else max_p - min_p
    base_p = min_p - range_p / 2 if flat else min_p

    xs = _x_positions(history)
    min_x = min(xs)
    span_x = (max(xs) - min_x) or 1
    n = len(history)

    def px(i):
        return PAD_LEFT + (plot_w / 2 if n == 1 else (xs[i] - min_x) / span_x * plot_w)

    def py(v):
        return PAD_TOP + plot_h - (v - base_p) / range_p * plot_h

    for s in range(5):
        val = base_p + range_p * s / 4
        yy = py(val)
        d.line([(PAD_LEFT, yy), (WIDTH - PAD_RIGHT, yy)], fill=GRID, width=1)
        text = f"{int(round(val)):,}"
        d.text((PAD_LEFT - 12, yy), text, font=f_tick, fill=DIM, anchor="rm")

    pts = [(px(i), py(prices[i])) for i in range(n)]
    if n > 1:
        d.line(pts, fill=ACCENT, width=4, joint="curve")
    for i, (cx, cy) in enumerate(pts):
        color = GREEN if decided and i == n - 1 else ACCENT
        d.ellipse([cx - 7, cy - 7, cx + 7, cy + 7], fill=color, outline=BG, width=3)

    last_price = prices[-1]
    last_text = f"✅ {last_price:,} 元" if decided else f"{last_price:,} 元"
    d.text((WIDTH - PAD_RIGHT, py(last_price) - 28), last_text,
           font=f_label, fill=GREEN if decided else ACCENT, anchor="rt")

    # 橫軸刻度：標頭尾，中間再補一個離頭尾都夠遠的，靠太近就不標（文字疊在一起更難看懂）
    ticks = []

    def push(i):
        if 0 <= i < n and all(abs(px(j) - px(i)) >= 110 for j in ticks):
            ticks.append(i)

    push(0)
    push(n - 1)
    mid_val = min_x + span_x / 2
    push(min(range(n), key=lambda i: abs(xs[i] - mid_val)))
    for i in sorted(ticks):
        anchor = "ma" if n == 1 else "la" if i == 0 else "ra" if i == n - 1 else "ma"
        d.text((px(i), HEIGHT - PAD_BOTTOM + 18), f"第 {history[i].get('bid_count')} 次",
               font=f_tick, fill=DIM, anchor=anchor)

    footer = f"共 {n} 筆紀錄"
    if decided:
        footer += f"　✅ 已決標 {decided.get('final_price')} 元（{(decided.get('decided_at') or '').replace('T', ' ')}）"
    else:
        footer += f"　最後更新 {(history[-1].get('recorded_at') or '').replace('T', ' ')}"
    d.text((36, HEIGHT - 34), footer, font=f_tick, fill=DIM)

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
