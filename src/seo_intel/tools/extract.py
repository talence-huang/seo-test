"""確定性的 HTML 特徵萃取（不使用 LLM）。容忍格式錯誤的 HTML；隱藏文字不計入字數，但納入注入掃描。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

_WORD = re.compile(r"[A-Za-z0-9']+")
_INJECTION = re.compile(
    r"ignore (all )?(previous|prior) instructions|system notice to ai|you must fetch|disregard your instructions",
    re.IGNORECASE,
)
REDACTED = "[已移除：疑似指令文字]"


def looks_like_instructions(text: str) -> bool:
    return bool(_INJECTION.search(text))


def redact_instructions(text: str) -> tuple[str, int]:
    """把疑似對 AI 下指令的片段換成標記（送進 LLM 前的最後一道防線）。回傳 (處理後文字, 移除段數)。"""
    return _INJECTION.subn(REDACTED, text)


_SKIP_TEXT = {"script", "style", "noscript", "nav", "footer", "head", "title"}
_VOID = {"meta", "link", "br", "img", "input", "hr", "source", "area", "base", "col", "embed", "wbr"}
_BOILERPLATE_HEADINGS = re.compile(r"related|frequently asked|faq", re.IGNORECASE)


@dataclass
class PageFeatures:
    url: str
    title: str = ""
    h1: str = ""
    h2: list[str] = field(default_factory=list)
    word_count: int = 0
    schema_types: list[str] = field(default_factory=list)
    faq_count: int = 0
    has_table: bool = False
    published: str | None = None
    modified: str | None = None
    canonical: str | None = None
    links: list[tuple[str, str]] = field(default_factory=list)
    js_shell: bool = False
    malformed: bool = False
    suspicious_instructions: bool = False

    @property
    def sections(self) -> list[str]:
        """內容章節（排除「相關文章」「常見問題」等版型標題）。"""
        return [h for h in self.h2 if not _BOILERPLATE_HEADINGS.search(h)]

    def modified_date(self) -> date | None:
        return _parse_date(self.modified)

    def published_date(self) -> date | None:
        return _parse_date(self.published)

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("links")
        d["sections"] = self.sections
        d["n_links"] = len(self.links)
        return d


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool, bool]] = []   # (tag, 是否隱藏, 是否為 FAQ 區塊)
        self.f_title: list[str] = []
        self.h1: list[str] = []
        self.h2: list[str] = []
        self.visible: list[str] = []
        self.all_text: list[str] = []
        self.meta: dict[str, str] = {}
        self.canonical: str | None = None
        self.ldjson: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.has_table = False
        self.faq_h3 = 0
        self.root_div = False
        self._cur: str | None = None
        self._buf: list[str] = []
        self._href: str | None = None
        self._anchor: list[str] = []
        self._in_faq = 0

    def _hidden(self) -> bool:
        return any(h for _, h, _ in self.stack)

    def _inside(self, names: set[str]) -> bool:
        return any(t in names for t, _, _ in self.stack)

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        hidden = "display:none" in a.get("style", "").replace(" ", "")
        is_faq = tag == "div" and "faq" in a.get("class", "")
        if tag == "meta":
            key = a.get("property") or a.get("name")
            if key:
                self.meta[key] = a.get("content", "")
        elif tag == "link" and a.get("rel") == "canonical":
            self.canonical = a.get("href")
        elif tag == "a":
            self._href, self._anchor = a.get("href"), []
        elif tag == "table":
            self.has_table = True
        elif tag == "div" and a.get("id") == "root":
            self.root_div = True
        elif is_faq:
            self._in_faq += 1
        elif tag == "script" and a.get("type") == "application/ld+json":
            self._cur, self._buf = "ldjson", []
        elif tag in ("title", "h1", "h2", "h3"):
            self._cur, self._buf = tag, []
        if tag not in _VOID:
            self.stack.append((tag, hidden, is_faq))

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._anchor).strip()))
            self._href = None
        if self._cur == tag or (self._cur == "ldjson" and tag == "script"):
            text = "".join(self._buf).strip()
            if self._cur == "title":
                self.f_title.append(text)
            elif self._cur == "h1":
                self.h1.append(text)
            elif self._cur == "h2":
                self.h2.append(text)
            elif self._cur == "h3" and self._in_faq:
                self.faq_h3 += 1
            elif self._cur == "ldjson":
                self.ldjson.append(text)
            self._cur = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:   # 容忍未關閉的標籤：一併彈出其上的元素
                self._in_faq -= sum(1 for _, _, faq in self.stack[i:] if faq)
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self._cur:
            self._buf.append(data)
        if self._href is not None:
            self._anchor.append(data.strip())
        self.all_text.append(data)
        if not self._hidden() and not self._inside(_SKIP_TEXT):
            self.visible.append(data)


def extract(html: str, url: str) -> PageFeatures:
    p = _Parser()
    try:
        p.feed(html)
        p.close()
    except Exception:  # 極端損壞的輸入：保留已解析的部分
        pass
    schema: list[str] = []
    faq_entities = 0
    for raw in p.ldjson:
        try:
            ld = json.loads(raw)
        except json.JSONDecodeError:
            continue
        t = ld.get("@type")
        schema += t if isinstance(t, list) else [t] if t else []
        faq_entities = max(faq_entities, len(ld.get("mainEntity") or []))
    host = urlsplit(url).hostname
    links = []
    for href, anchor in p.links:
        absolute = urljoin(url, href)
        if urlsplit(absolute).hostname == host:
            links.append((absolute.split("#")[0], anchor))
    words = len(_WORD.findall(" ".join(p.visible)))
    return PageFeatures(
        url=url,
        title=(p.f_title[0] if p.f_title else ""),
        h1=(p.h1[0] if p.h1 else ""),
        h2=p.h2,
        word_count=words,
        schema_types=sorted(set(schema)),
        faq_count=faq_entities or p.faq_h3,
        has_table=p.has_table,
        published=p.meta.get("article:published_time"),
        modified=p.meta.get("article:modified_time"),
        canonical=p.canonical,
        links=links,
        js_shell=p.root_div and words < 30,
        malformed="</html>" not in html[-200:].lower(),
        suspicious_instructions=bool(_INJECTION.search(" ".join(p.all_text))),
    )
