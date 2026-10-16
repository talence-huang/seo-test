"""三種網站原型：主題群組、詞彙、預設 URL 範本與陷阱種類。

情境檔只需指定原型與各範本頁數，即可產生完整網站。網站內容以英文產生
（方便字數計算與關鍵字比對）；研究問題與文件為繁體中文。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Archetype:
    clusters: tuple[str, ...]
    modifiers: tuple[str, ...]
    vocab: tuple[str, ...]
    headings: tuple[str, ...]
    fillers: tuple[str, ...]          # SERP 中的其他網域
    templates: dict[str, dict]        # 名稱 → TemplateSpec 欄位
    traps: frozenset[str]             # calendar / pagination / facet
    robots_disallow: tuple[str, ...] = ("/search", "/account/", "/cart")
    brands: tuple[str, ...] = field(default_factory=tuple)


ARCHETYPES: dict[str, Archetype] = {
    "content_publisher": Archetype(
        clusters=("long-term care insurance", "medical insurance", "life insurance", "travel insurance", "car insurance"),
        modifiers=("cost", "guide", "comparison", "for seniors", "how to choose", "claims", "tips", "explained"),
        vocab=("coverage", "premium", "policy", "claim", "provider", "plan", "benefit", "deductible", "rider",
               "eligibility", "insurer", "quote", "exclusion", "payout", "family", "budget", "risk", "monthly"),
        headings=("What it covers", "How much it costs", "Who should buy it", "How claims work", "Comparing providers",
                  "Common exclusions", "Tax considerations", "Choosing a rider", "Mistakes to avoid", "Next steps"),
        fillers=("insurance-wiki.sim", "moneydaily.sim", "govbenefits.sim", "finforum.sim", "policyreviews.sim",
                 "newsdesk.sim", "healthline-ins.sim", "consumerguide.sim"),
        templates={
            "guide": dict(kind="guide", prefix="/guide/", count=200, words=(900, 1500), schema_types=["Article"],
                          sections=(4, 7), archived_rate=0.8, noise_edit_rate=0.15),
            "blog": dict(kind="blog", prefix="/blog/", count=2000, words=(500, 900), schema_types=["BlogPosting"],
                         sections=(3, 5), archived_rate=0.3),
            "tag": dict(kind="tag", prefix="/tag/", count=1500, words=(40, 120), sections=(0, 1),
                        archived_rate=0.05, noise_edit_rate=0.3),
            "faq": dict(kind="faq", prefix="/faq/", count=150, words=(300, 600), sections=(1, 3), archived_rate=0.5),
            "author": dict(kind="author", prefix="/author/", count=60, words=(80, 200), sections=(0, 1), archived_rate=0.1),
            "category": dict(kind="category", prefix="/category/", count=30, words=(150, 300),
                             schema_types=["CollectionPage"], sections=(1, 2), archived_rate=0.6),
        },
        traps=frozenset({"calendar", "pagination"}),
    ),
    "ecommerce": Archetype(
        clusters=("hiking boots", "tents", "backpacks", "sleeping bags", "rain jackets"),
        modifiers=("best", "review", "for beginners", "waterproof", "lightweight", "sale", "vs", "sizing"),
        vocab=("waterproof", "lightweight", "durable", "fit", "size", "trail", "comfort", "warranty", "weight",
               "material", "grip", "seam", "price", "shipping", "outdoor", "season", "capacity", "frame"),
        headings=("Key features", "Sizing and fit", "Materials", "Care instructions", "How to choose",
                  "Best for", "What reviewers say", "Price and value", "Alternatives", "Buying checklist"),
        fillers=("outdoor-wiki.sim", "gearreviews.sim", "trailforum.sim", "dealhunter.sim", "campnews.sim",
                 "bigmart.sim", "hikerblog.sim", "shopcompare.sim"),
        templates={
            "product": dict(kind="product", prefix="/product/", count=3000, words=(150, 400), schema_types=["Product"],
                            sections=(1, 2), archived_rate=0.1, noise_edit_rate=0.25),
            "category": dict(kind="category", prefix="/category/", count=40, words=(150, 300),
                             schema_types=["CollectionPage"], sections=(1, 2), archived_rate=0.6),
            "brand": dict(kind="brand", prefix="/brand/", count=150, words=(100, 250), sections=(1, 2), archived_rate=0.2),
            "blog": dict(kind="blog", prefix="/blog/", count=800, words=(500, 900), schema_types=["BlogPosting"],
                         sections=(3, 5), archived_rate=0.3),
            "tag": dict(kind="tag", prefix="/tag/", count=1000, words=(40, 120), sections=(0, 1), archived_rate=0.05),
            "review": dict(kind="review", prefix="/reviews/", count=1000, words=(200, 500), schema_types=["Review"],
                           sections=(1, 2), archived_rate=0.1),
        },
        traps=frozenset({"facet", "pagination"}),
        brands=("Alpenrock", "Northtrail", "Kestrel", "Summitline", "Riverbend", "Granite Co"),
    ),
    "saas_docs": Archetype(
        clusters=("kanban boards", "time tracking", "integrations", "gantt charts", "team permissions"),
        modifiers=("setup", "best practices", "template", "for agencies", "tutorial", "vs spreadsheets", "api", "pricing"),
        vocab=("workflow", "team", "project", "task", "dashboard", "automation", "permission", "report", "sprint",
               "milestone", "sync", "webhook", "export", "workspace", "deadline", "assignee", "status", "board"),
        headings=("Overview", "Getting started", "Configuration", "Examples", "Limitations", "Troubleshooting",
                  "API reference", "Pricing notes", "Migration", "FAQ"),
        fillers=("pm-wiki.sim", "saasreview.sim", "devforum.sim", "productivitynews.sim", "stackanswers.sim",
                 "techdaily.sim", "teamtools.sim", "workflowhub.sim"),
        templates={
            "docs": dict(kind="docs", prefix="/docs/", count=800, words=(400, 1000), schema_types=["TechArticle"],
                         sections=(3, 6), archived_rate=0.4),
            "blog": dict(kind="blog", prefix="/blog/", count=1000, words=(500, 900), schema_types=["BlogPosting"],
                         sections=(3, 5), archived_rate=0.3),
            "integrations": dict(kind="integrations", prefix="/integrations/", count=50, words=(300, 600),
                                 schema_types=["SoftwareApplication"], sections=(2, 4),
                                 clusters=["integrations"], archived_rate=0.5),
            "templates": dict(kind="templates", prefix="/templates/", count=80, words=(200, 500), sections=(1, 3),
                              clusters=["kanban boards", "gantt charts", "time tracking"], archived_rate=0.3),
            "tag": dict(kind="tag", prefix="/tag/", count=800, words=(40, 120), sections=(0, 1), archived_rate=0.05),
            "changelog": dict(kind="changelog", prefix="/changelog/", count=600, words=(80, 250), sections=(0, 1),
                              archived_rate=0.2),
            "app": dict(kind="app", prefix="/app/", count=500, words=(0, 0), sections=(0, 0), archived_rate=0.0,
                        noise_edit_rate=0.0),
        },
        traps=frozenset({"calendar"}),
    ),
}

# 參與排名、可作為內容證據的頁面種類（標籤頁、作者頁、更新日誌、JS 空殼頁不排名）
CONTENT_KINDS = frozenset({"guide", "blog", "docs", "product", "category", "faq", "integrations", "templates"})
