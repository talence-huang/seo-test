"""情境定義的 schema 與一致性驗證。情境檔位於 evals/scenarios/*.yaml。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from simweb.archetypes import ARCHETYPES

Kind = Literal[
    "guide", "blog", "tag", "faq", "author", "category", "product", "brand", "review",
    "docs", "integrations", "templates", "changelog", "app",
]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TemplateSpec(_M):
    name: str
    kind: Kind
    prefix: str
    count: int = Field(ge=0)
    words: tuple[int, int]
    schema_types: tuple[str, ...] = ()
    sections: tuple[int, int] = (2, 4)
    clusters: tuple[str, ...] | None = None
    archived_rate: float = Field(default=0.2, ge=0, le=1)
    noise_edit_rate: float = Field(default=0.1, ge=0, le=1)
    has_table: bool = False
    launched_by: str | None = None

    @model_validator(mode="after")
    def _prefix_shape(self) -> TemplateSpec:
        if not (self.prefix.startswith("/") and self.prefix.endswith("/") and len(self.prefix) > 2):
            raise ValueError(f"範本 {self.name} 的 prefix 必須形如 /xxx/：{self.prefix}")
        return self


class InjectionSpec(_M):
    template: str
    cluster: str | None = None


class SiteSpec(_M):
    domain: str
    role: Literal["own", "competitor"]
    archetype: str
    counts: dict[str, int] = {}
    extra_templates: tuple[TemplateSpec, ...] = ()
    crawl_delay: float = Field(default=2.0, gt=0)
    waf_prefixes: tuple[str, ...] = ()
    flaky_rate: float = Field(default=0.0, ge=0, le=1)
    malformed_rate: float = Field(default=0.0, ge=0, le=1)
    injection: InjectionSpec | None = None

    def templates(self) -> dict[str, TemplateSpec]:
        arch = ARCHETYPES[self.archetype]
        unknown = set(self.counts) - set(arch.templates)
        if unknown:
            raise ValueError(f"{self.domain}：counts 指定了原型中不存在的範本 {sorted(unknown)}")
        out = {name: TemplateSpec(name=name, **fields) for name, fields in arch.templates.items()}
        out = {n: t.model_copy(update={"count": self.counts[n]}) if n in self.counts else t for n, t in out.items()}
        for t in self.extra_templates:
            if t.name in out:
                raise ValueError(f"{self.domain}：額外範本 {t.name} 與原型範本重名")
            out[t.name] = t
        return out


class ChangeSpec(_M):
    words_mult: float = Field(default=1.0, gt=0)
    add_sections: int = Field(default=0, ge=0)
    add_schema: tuple[str, ...] = ()
    add_faq: int = Field(default=0, ge=0)
    add_table: bool = False


class EventSpec(_M):
    """植入的網站變動。refresh：改寫既有頁面；bulk_add：在範本尾端大量新增頁面；launch：整個新範本上線。"""

    id: str
    site: str
    template: str
    kind: Literal["refresh", "bulk_add", "launch"]
    date: date
    pages: int | None = Field(default=None, gt=0)
    cluster: str | None = None
    spread_days: int = Field(default=7, ge=0)
    changes: ChangeSpec = ChangeSpec()

    def change_types(self, template: TemplateSpec) -> tuple[str, ...]:
        if self.kind == "launch":
            return ("new_template_launch",)
        if self.kind == "bulk_add":
            return ("bulk_pages",)
        c = self.changes
        types: list[str] = []
        if c.words_mult >= 1.2:
            types.append("content_expansion")
        if c.add_sections:
            types.append("new_sections")
        types += [f"schema:{s}" for s in c.add_schema if s not in template.schema_types]
        if c.add_faq:
            types.append("faq_content")
        if c.add_table:
            types.append("comparison_table")
        return tuple(types) or ("minor_update",)


class EffectSpec(_M):
    """事件對排名的因果影響：自家站在該群組查詢上排名變差、事件網站排名變好。"""

    id: str
    cause: str
    cluster: str
    own_loss: float = Field(default=0.0, ge=0)
    competitor_gain: float = Field(default=0.0, ge=0)
    lag_days: int = Field(default=7, ge=0)
    ramp_days: int = Field(default=14, ge=1)


class ConfounderSpec(_M):
    """全站性波動（例如演算法更新）：自家站所有查詢一起變差，可用對照組識別。"""

    id: str
    date: date
    own_shift: float
    ramp_days: int = Field(default=5, ge=1)


class GoalSpec(_M):
    type: Literal["ranking_drop_cause", "competitor_content_strategy"]
    question: str
    own_site: str
    competitors: tuple[str, ...]
    cluster: str
    window: tuple[date, date]
    focus_competitor: str | None = None


class ScenarioSpec(_M):
    id: str
    split: Literal["train", "test"]
    archetype: str
    seed: int
    now: date
    goal: GoalSpec
    sites: tuple[SiteSpec, ...]
    events: tuple[EventSpec, ...]
    effects: tuple[EffectSpec, ...]
    confounders: tuple[ConfounderSpec, ...] = ()

    def site(self, domain: str) -> SiteSpec:
        return next(s for s in self.sites if s.domain == domain)

    def event(self, event_id: str) -> EventSpec:
        return next(e for e in self.events if e.id == event_id)

    @model_validator(mode="after")
    def _consistent(self) -> ScenarioSpec:
        errors: list[str] = []
        if self.archetype not in ARCHETYPES:
            raise ValueError(f"未知的網站原型：{self.archetype}")
        clusters = set(ARCHETYPES[self.archetype].clusters)
        domains = [s.domain for s in self.sites]
        if len(set(domains)) != len(domains):
            errors.append("網域重複")
        owns = [s.domain for s in self.sites if s.role == "own"]
        if owns != [self.goal.own_site]:
            errors.append(f"自家站必須恰好一個且等於 goal.own_site：{owns}")
        if set(self.goal.competitors) != {s.domain for s in self.sites if s.role == "competitor"}:
            errors.append("goal.competitors 必須等於所有競品網域")
        if self.goal.cluster not in clusters:
            errors.append(f"goal.cluster 不在原型主題中：{self.goal.cluster}")
        if self.goal.type == "competitor_content_strategy" and self.goal.focus_competitor not in self.goal.competitors:
            errors.append("競品內容策略類目標必須指定 focus_competitor")

        templates = {s.domain: s.templates() for s in self.sites if s.archetype in ARCHETYPES}
        event_ids = {e.id for e in self.events}
        for s in self.sites:
            if s.archetype != self.archetype:
                errors.append(f"{s.domain} 的原型與情境不同")
            for t in s.extra_templates:
                if t.launched_by and t.launched_by not in event_ids:
                    errors.append(f"{s.domain}/{t.name} 的 launched_by 指向不存在的事件")
            if s.injection and s.injection.template not in templates.get(s.domain, {}):
                errors.append(f"{s.domain} 的注入頁範本不存在")
        for e in self.events:
            t = templates.get(e.site, {}).get(e.template)
            if t is None:
                errors.append(f"事件 {e.id} 指向不存在的網站或範本")
                continue
            if e.cluster and e.cluster not in clusters:
                errors.append(f"事件 {e.id} 的主題不存在：{e.cluster}")
            if e.kind in ("refresh", "bulk_add") and e.pages is None:
                errors.append(f"事件 {e.id}（{e.kind}）必須指定 pages")
            if e.kind == "launch" and t.launched_by != e.id:
                errors.append(f"事件 {e.id} 是 launch，但範本 {t.name} 的 launched_by 不是它")
            if e.date + timedelta(days=e.spread_days) > self.now - timedelta(days=14):
                errors.append(f"事件 {e.id} 結束日距離 now 不足 14 天，效應無法觀察")
        for fx in self.effects:
            if fx.cause not in event_ids:
                errors.append(f"效應 {fx.id} 的 cause 不存在：{fx.cause}")
            if fx.cluster not in clusters:
                errors.append(f"效應 {fx.id} 的主題不存在：{fx.cluster}")
        if errors:
            raise ValueError(f"情境 {self.id} 不一致：" + "；".join(errors))
        return self
