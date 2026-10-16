"""不連網的假 LLM，用於測試迴圈邏輯。

依角色回傳預先寫好的輸出（字典、Pydantic 物件、例外，或依輸入動態產生的函式），
並以字數估算 token 與成本，讓預算邏輯也能在測試中被驗證。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pydantic
from pydantic import BaseModel

from seo_intel.config import ModelsConfig
from seo_intel.llm.cost import compute_usd
from seo_intel.llm.types import CallMeta, LLMFatalError, LLMResult, LLMSchemaError, T, TokenUsage

Responder = Callable[[str, str, type[BaseModel]], Any]
ScriptEntry = list[Any] | Responder


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class FakeLLM:
    def __init__(self, models: ModelsConfig, script: dict[str, ScriptEntry]) -> None:
        self.models = models
        self.script = {role: (list(v) if isinstance(v, list) else v) for role, v in script.items()}
        self.calls: list[dict[str, Any]] = []

    def call(self, role: str, *, system: str, user: str, schema: type[T]) -> LLMResult[T]:
        if role not in self.models.roles:
            raise LLMFatalError(f"未定義的 LLM 角色：{role}")
        entry = self.script.get(role)
        if entry is None:
            raise LLMFatalError(f"FakeLLM 沒有為角色 {role} 準備回應")
        if callable(entry):
            item = entry(system, user, schema)
        elif entry:
            item = entry.pop(0)
        else:
            raise LLMFatalError(f"FakeLLM 角色 {role} 的回應已用完")
        self.calls.append({"role": role, "system": system, "user": user, "schema": schema.__name__})

        if isinstance(item, Exception):
            raise item
        payload = item.model_dump() if isinstance(item, BaseModel) else item
        cfg = self.models.roles[role]
        usage = TokenUsage(
            input_tokens=_estimate_tokens(system) + _estimate_tokens(user),
            output_tokens=_estimate_tokens(str(payload)),
        )
        meta = CallMeta(
            role=role,
            model=cfg.model,
            usage=usage,
            usd=compute_usd(self.models.pricing[cfg.model], usage),
            stop_reason="end_turn",
        )
        try:
            parsed = schema.model_validate(payload)
        except pydantic.ValidationError as e:
            raise LLMSchemaError(f"輸出不符合 {schema.__name__}：{e}", meta) from e
        return LLMResult(parsed=parsed, meta=meta)
