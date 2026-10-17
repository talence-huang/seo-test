"""SEO 策略維度：固定的詞彙與量測（On-page 逐頁量測、Off-page 來自反向連結輪廓）。

研究目標的三個問題都以這套詞彙回答：
- On-page：競品「有排名的頁面」和同網站其他主題頁比，哪些做法明顯更普遍（對比），以及是不是研究期間內才採用（時序）
- Off-page：反向連結的成長、錨點文字、連結指向（來自使用者提供的 CSV）
- 優先行動：自家網站在同一個維度上的差距（core/priority.py）
LLM 只能從這個詞彙中挑選維度提出假設；數字一律由程式計算。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date
from typing import Literal
from urllib.parse import unquote, urlsplit

from seo_intel.tools.text import tokens

Side = Literal["onpage", "offpage"]
Kind = Literal["bool", "numeric", "site"]


@dataclass(frozen=True)
class Dimension:
    key: str
    label: str
    side: Side
    kind: Kind          # bool / numeric：逐頁量測；site：網站層級（新增頁面、反向連結）
    effort: int         # 實作難度：1 低、2 中、3 高
    action: str         # 自家缺少時的建議行動


DIMENSIONS: dict[str, Dimension] = {d.key: d for d in (
    Dimension("content_depth", "內容深度（字數）", "onpage", "numeric", 2, "擴充主題頁的內容深度"),
    Dimension("sections", "段落結構（H2 段落數）", "onpage", "numeric", 2, "依搜尋意圖重整主題頁的段落結構"),
    Dimension("faq", "FAQ 區塊", "onpage", "bool", 1, "在主題頁加入 FAQ 區塊"),
    Dimension("schema_faq", "FAQPage 結構化資料", "onpage", "bool", 1, "為 FAQ 加上 FAQPage 結構化資料"),
    Dimension("schema_howto", "HowTo 結構化資料", "onpage", "bool", 1, "為教學內容加上 HowTo 結構化資料"),
    Dimension("schema_product", "Product 結構化資料", "onpage", "bool", 1, "為商品頁加上 Product 結構化資料"),
    Dimension("schema_review", "評分 / Review 結構化資料", "onpage", "bool", 1, "加上評分（Review / AggregateRating）結構化資料"),
    Dimension("schema_breadcrumb", "麵包屑結構化資料", "onpage", "bool", 1, "加上 BreadcrumbList 結構化資料"),
    Dimension("kw_title", "title 含關鍵字", "onpage", "bool", 1, "在主題頁 title 放入目標關鍵字"),
    Dimension("kw_h1", "H1 含關鍵字", "onpage", "bool", 1, "在主題頁 H1 放入目標關鍵字"),
    Dimension("kw_url", "網址含關鍵字", "onpage", "bool", 2, "新頁面使用含關鍵字的網址（既有網址不建議更動）"),
    Dimension("meta_description", "meta description", "onpage", "bool", 1, "為主題頁撰寫 meta description"),
    Dimension("comparison_table", "比較表", "onpage", "bool", 1, "在主題頁加入比較表"),
    Dimension("author", "作者資訊（E-E-A-T）", "onpage", "bool", 1, "標示作者與專業背景（E-E-A-T）"),
    Dimension("fresh", "研究期間內有更新", "onpage", "bool", 2, "定期更新主題頁並標示更新日期"),
    Dimension("internal_links", "內部連結（連入數）", "onpage", "numeric", 2, "從相關頁面增加指向主題頁的內部連結"),
    Dimension("new_content", "期間內大量新增主題頁", "onpage", "site", 3, "規劃並持續新增主題內容"),
    Dimension("link_growth", "新增引用網域（反向連結成長）", "offpage", "site", 3, "經營主題相關網站的引用與合作（反向連結）"),
    Dimension("anchor_keyword", "錨點文字含關鍵字", "offpage", "site", 3, "爭取含主題關鍵字錨點的自然引用"),
    Dimension("link_to_ranking", "外部連結集中指向排名頁", "offpage", "site", 3, "讓外部引用集中指向主題核心頁"),
)}
PAGE_DIMENSIONS = [d.key for d in DIMENSIONS.values() if d.kind != "site"]
OFFPAGE = [d.key for d in DIMENSIONS.values() if d.side == "offpage"]


def label(key: str) -> str:
    d = DIMENSIONS.get(key)
    return d.label if d else key


# ---------- 逐頁量測 ----------

def covers(text: str, phrases: list[set[str]]) -> bool:
    """文字是否完整包含任一組關鍵字片語（容許 25% 缺漏，與主題頁判定一致）。"""
    toks = tokens(text or "")
    return any(p and len(p & toks) / len(p) >= 0.75 for p in phrases)


def phrases_of(keywords: list[str]) -> list[set[str]]:
    return [t for t in (tokens(k) for k in keywords) if t]


def page_values(url: str, info: dict, phrases: list[set[str]], window: tuple[date, date],
                inlinks: int | None = None) -> dict:
    """一個已抓取頁面在各維度上的值（bool 或數字）。info 是 EpisodeState.fetched 的頁面摘要。
    inlinks（內部連入數）取決於已抓了哪些頁面，抓取當下不知道；None 時不量這個維度，比較輪廓時再補上。"""
    schema = set(info.get("schema") or [])
    modified = (info.get("modified") or "")[:10]
    try:
        fresh = window[0] <= date.fromisoformat(modified) <= window[1]
    except ValueError:
        fresh = False
    values = {
        "content_depth": info.get("words", 0),
        "sections": len(info.get("h2") or []),
        "faq": (info.get("faq") or 0) > 0,
        "schema_faq": "FAQPage" in schema,
        "schema_howto": "HowTo" in schema,
        "schema_product": "Product" in schema,
        "schema_review": bool(schema & {"Review", "AggregateRating"}),
        "schema_breadcrumb": "BreadcrumbList" in schema,
        "kw_title": covers(info.get("title", ""), phrases),
        "kw_h1": covers(info.get("h1", ""), phrases),
        "kw_url": covers(unquote(urlsplit(url).path), phrases),
        "meta_description": bool(info.get("meta_desc")),
        "comparison_table": bool(info.get("table")),
        "author": bool(info.get("author")),
        "fresh": fresh,
    }
    if inlinks is not None:
        values["internal_links"] = inlinks
    return values


# ---------- 對比與檢定 ----------

def fisher_p(a: int, b: int, c: int, d: int) -> float:
    """2×2 表 [[a, b], [c, d]] 的 Fisher 精確檢定（雙尾）。"""
    n1, n2, k = a + b, c + d, a + c
    n = n1 + n2

    def p(x: int) -> float:
        return math.comb(n1, x) * math.comb(n2, k - x) / math.comb(n, k)

    lo, hi = max(0, k - n2), min(k, n1)
    observed = p(a)
    return min(1.0, sum(p(x) for x in range(lo, hi + 1) if p(x) <= observed * (1 + 1e-9)))


def compare(group: list[dict], others: list[dict], key: str) -> dict:
    """一個維度在兩組頁面（例如有排名 vs 同網站其他主題頁）的差異。數字型以合併中位數做中位數檢定。"""
    dim = DIMENSIONS[key]
    a_vals, b_vals = [v[key] for v in group if key in v], [v[key] for v in others if key in v]
    n_a, n_b = len(a_vals), len(b_vals)
    if dim.kind == "numeric":
        med_a = statistics.median(a_vals) if a_vals else 0
        med_b = statistics.median(b_vals) if b_vals else 0
        pooled = statistics.median(a_vals + b_vals) if a_vals + b_vals else 0
        hi_a, hi_b = sum(1 for x in a_vals if x > pooled), sum(1 for x in b_vals if x > pooled)
        p = fisher_p(hi_a, n_a - hi_a, hi_b, n_b - hi_b) if n_a and n_b else 1.0
        ratio = med_a / med_b if med_b else (float("inf") if med_a else 1.0)
        return {"dimension": key, "kind": "numeric", "group": med_a, "others": med_b, "n_group": n_a, "n_others": n_b,
                "ratio": round(ratio, 2) if ratio != float("inf") else None, "p": round(p, 4)}
    k_a, k_b = sum(1 for x in a_vals if x), sum(1 for x in b_vals if x)
    share_a, share_b = (k_a / n_a if n_a else 0.0), (k_b / n_b if n_b else 0.0)
    p = fisher_p(k_a, n_a - k_a, k_b, n_b - k_b) if n_a and n_b else 1.0
    return {"dimension": key, "kind": "bool", "group": round(share_a, 3), "others": round(share_b, 3),
            "n_group": n_a, "n_others": n_b, "diff": round(share_a - share_b, 3), "p": round(p, 4)}


ADOPTION_IGNORED = {"fresh", "internal_links"}   # 更新日期本身、連入數（取決於抽樣）不算「做法的採用」
GREW = {"content_depth": (1.5, 200), "sections": (1.5, 2)}   # 數字型：至少變成 1.5 倍且增加這麼多才算新採用


def adoption(before: dict, after: dict) -> tuple[list[str], list[str]]:
    """同一頁改版前後（Wayback 快照 vs 目前）：期間內新採用與拿掉的做法。"""
    adopted, dropped = [], []
    for key in PAGE_DIMENSIONS:
        if key in ADOPTION_IGNORED or key not in before or key not in after:
            continue
        b, a = before[key], after[key]
        if DIMENSIONS[key].kind == "numeric":
            ratio, delta = GREW[key]
            grew, shrank = a >= max(b * ratio, b + delta), b >= max(a * ratio, a + delta)
        else:
            grew, shrank = bool(a) and not b, bool(b) and not a
        if grew:
            adopted.append(key)
        elif shrank:
            dropped.append(key)
    return adopted, dropped


def describe(values: dict) -> str:
    """一頁的量測寫成一行，給研究紀錄看：數字、有做的、沒做的。"""
    flags = [k for k in PAGE_DIMENSIONS if DIMENSIONS[k].kind == "bool" and k in values]
    schema = [DIMENSIONS[k].label.replace(" 結構化資料", "") for k in flags if k.startswith("schema_") and values[k]]
    have = [DIMENSIONS[k].label for k in flags if not k.startswith("schema_") and values[k]]
    lack = [DIMENSIONS[k].label for k in flags if not k.startswith("schema_") and not values[k]]
    nums = f"{values.get('content_depth', 0):,} 字、{values.get('sections', 0)} 段 H2"
    return (f"{nums}；結構化資料：{'、'.join(schema) or '無'}；有：{'、'.join(have) or '—'}；"
            f"沒有：{'、'.join(lack) or '—'}")


def profile(pages: list[dict]) -> dict:
    """一組頁面在各維度的普及率（bool）或中位數（numeric）。"""
    out = {}
    for key in PAGE_DIMENSIONS:
        vals = [v[key] for v in pages if key in v]
        if not vals:
            continue
        out[key] = (statistics.median(vals) if DIMENSIONS[key].kind == "numeric"
                    else round(sum(1 for x in vals if x) / len(vals), 3))
    return out


def gap(key: str, competitor: float | None, own: float | None) -> float | None:
    """自家在某維度上落後競品多少（0＝沒有差距，1＝完全沒做）。數字型以中位數比例計。None＝無法比較。"""
    if competitor is None or own is None:
        return None
    if DIMENSIONS[key].kind == "numeric":
        return round(max(0.0, 1 - own / competitor), 3) if competitor else 0.0
    return round(max(0.0, competitor - own), 3)
