# -*- coding: utf-8 -*-
"""號碼型態（鐵支／豹子／順子／對子／回文）的判定規則。

跟 `static/plate-patterns.js` 是同一套規則的兩份實作（網頁下拉選單用 JS 那份、
Discord `/查詢` 用這份），改任何一種型態的定義要兩邊一起改，否則同一個號碼在
網頁和 Discord 會被歸到不同型態。

一律只看號牌結尾的那串數字（跟 `extract_plate_number` 同一條規則），字首字母
不算進型態：大家講的「8888」指的是數字，換字首重新上架還是同一個型態。
"""

import re

_DIGITS_RE = re.compile(r"(\d+)\s*$")


def plate_digits(plate):
    m = _DIGITS_RE.search(str(plate or ""))
    return m.group(1) if m else ""


def _longest_run(d):
    """連續重複的最長長度：8880 是 3、8888 是 4、1234 是 1。"""
    best = run = 1 if d else 0
    for i in range(1, len(d)):
        run = run + 1 if d[i] == d[i - 1] else 1
        best = max(best, run)
    return best


def _is_run(d, step):
    """整串是不是等差 ±1（1234 遞增、9876 遞減）。

    沒有跨 9→0 接回去，9012 這種在市場上算不算順子見仁見智，這裡採嚴格定義。
    """
    if len(d) < 3:
        return False
    return all(int(d[i]) - int(d[i - 1]) == step for i in range(1, len(d)))


def _is_pairs(d):
    # AABB 與 ABAB 兩種，兩對必須是不同數字，否則 8888 也會被算成對子
    if len(d) != 4:
        return False
    return (d[0] == d[1] and d[2] == d[3] and d[0] != d[2]) or \
           (d[0] == d[2] and d[1] == d[3] and d[0] != d[1])


# (value, 選單標籤, 判定函式)。value 就是使用者在 Discord 選到的值，
# 直接用中文短名，前綴指令 `!查詢 ... 鐵支` 也打得出來。
PLATE_PATTERNS = [
    ("鐵支", "鐵支（四同，如 8888）", lambda d: len(d) >= 4 and _longest_run(d) >= 4),
    # 四同已經有自己的選項，豹子只留剛好三個的，兩個選項才不會互相蓋掉
    ("豹子", "豹子（三同，如 1888）", lambda d: _longest_run(d) == 3),
    ("順子", "順子（如 1234／9876）", lambda d: _is_run(d, 1) or _is_run(d, -1)),
    ("對子", "對子（如 1122／1212）", _is_pairs),
    ("回文", "回文（如 1221）", lambda d: len(d) == 4 and d[0] == d[3] and d[1] == d[2] and d[0] != d[1]),
]

PATTERN_VALUES = [value for value, _, _ in PLATE_PATTERNS]
PATTERN_LABELS = {value: label for value, label, _ in PLATE_PATTERNS}

# 打字打得出來但不是正式名稱的講法，都導到同一個型態
_ALIASES = {
    "鐵枝": "鐵支",
    "四同": "鐵支",
    "三同": "豹子",
    "aabb": "對子",
    "abab": "對子",
    "abba": "回文",
    "對稱": "回文",
}


def resolve_pattern(text):
    """把使用者輸入的字轉成型態 value，認不出來就回 None（＝不篩型態）。"""
    text = (text or "").strip()
    if not text or text == "全部":
        return None
    if text in PATTERN_LABELS:
        return text
    key = _ALIASES.get(text.lower())
    if key:
        return key
    # 選單標籤整串被貼回來（例如「鐵支（四同，如 8888）」）也接受
    for value, label in PATTERN_LABELS.items():
        if text == label or text.startswith(value):
            return value
    return None


def matches_pattern(plate, value):
    if not value:
        return True
    for v, _, test in PLATE_PATTERNS:
        if v == value:
            return test(plate_digits(plate))
    return True
