"""文字比對：關鍵字分詞（英數字以詞、中文以雙字詞）與主題判斷。"""

from __future__ import annotations

import re
from urllib.parse import unquote

_TOKEN = re.compile(r"[a-z0-9]+|[㐀-䶿一-鿿豈-﫿]+")
STOPWORDS = frozenset({"the", "a", "an", "and", "or", "for", "of", "to", "in", "on", "vs", "how", "what", "best"})


def tokens(text: str) -> set[str]:
    """英數字以詞為單位；中文沒有空白分詞，改用相鄰兩字（雙字詞），單字詞則保留單字。網址先解碼百分比編碼。"""
    out: set[str] = set()
    for t in _TOKEN.findall(unquote(text).lower()):
        if t.isascii():
            if t not in STOPWORDS:
                out.add(t)
        elif len(t) == 1:
            out.add(t)
        else:
            out |= {t[i:i + 2] for i in range(len(t) - 1)}
    return out


def matches_topic(query: str, keywords: list[str]) -> bool:
    """查詢包含任一關鍵字片語的全部詞彙即視為同主題。"""
    q = tokens(query)
    return any(kw and tokens(kw) <= q for kw in keywords)
