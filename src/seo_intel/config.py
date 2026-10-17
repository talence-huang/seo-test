"""載入並驗證 config/ 下的設定檔。設定錯誤在啟動時就報出，而不是執行到一半才發現。"""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def config_dir() -> Path:
    return Path(os.environ.get("SEO_INTEL_CONFIG_DIR", PROJECT_ROOT / "config"))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------- models.yaml ----------

class RoleConfig(_Strict):
    model: str
    effort: Literal["low", "medium", "high", "xhigh", "max"]
    max_tokens: int = Field(gt=0)
    fallback: bool = False


class LongContextPricing(_Strict):
    threshold_tokens: int = Field(gt=0)
    input: float = Field(ge=0)
    output: float = Field(ge=0)


class ModelPricing(_Strict):
    """每百萬 token 的美元價格。"""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float = Field(ge=0)
    cache_write: float = Field(ge=0)
    long_context: LongContextPricing | None = None


class ModelsConfig(_Strict):
    roles: dict[str, RoleConfig]
    pricing: dict[str, ModelPricing]

    @model_validator(mode="after")
    def _every_role_is_priced(self) -> ModelsConfig:
        missing = {r.model for r in self.roles.values()} - self.pricing.keys()
        if missing:
            raise ValueError(f"以下模型缺少價格設定：{sorted(missing)}")
        return self


# ---------- budget.yaml ----------

class PhaseShares(_Strict):
    discovery: float = Field(ge=0, le=1)
    sampling: float = Field(ge=0, le=1)
    targeted: float = Field(ge=0, le=1)
    verification: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _sum_to_one(self) -> PhaseShares:
        total = self.discovery + self.sampling + self.targeted + self.verification
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"階段預算比例總和必須為 1，目前為 {total}")
        return self


class BudgetConfig(_Strict):
    max_fetches: int = Field(gt=0)
    max_fetches_per_domain: int = Field(gt=0)
    max_api_calls: int = Field(gt=0)
    max_llm_usd: float = Field(gt=0)
    max_wall_time_s: int = Field(gt=0)
    max_actions: int = Field(gt=0)
    phases: PhaseShares


# ---------- policy.yaml ----------

class PolicyConfig(_Strict):
    rate_limit_seconds_per_domain: float = Field(gt=0)
    max_pages_per_domain: int = Field(gt=0)
    bulk_crawl_threshold: int = Field(gt=0)
    block_rate_threshold: float = Field(gt=0, le=1)
    approval_ttl_hours: int = Field(gt=0)
    require_approval: frozenset[str]
    forbidden: frozenset[str]

    @model_validator(mode="after")
    def _disjoint(self) -> PolicyConfig:
        overlap = self.require_approval & self.forbidden
        if overlap:
            raise ValueError(f"同一動作不能同時是「需核准」與「禁止」：{sorted(overlap)}")
        return self


class Settings(_Strict):
    models: ModelsConfig
    budget: BudgetConfig
    policy: PolicyConfig


def _read_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings(directory: Path | None = None) -> Settings:
    d = directory or config_dir()
    return Settings(
        models=ModelsConfig.model_validate(_read_yaml(d / "models.yaml")),
        budget=BudgetConfig.model_validate(_read_yaml(d / "budget.yaml")),
        policy=PolicyConfig.model_validate(_read_yaml(d / "policy.yaml")),
    )


@cache
def get_settings() -> Settings:
    return load_settings()
