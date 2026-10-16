"""真實的 Anthropic API client。

設計重點：
- 一律用 `beta.messages.parse` 取得結構化輸出（Pydantic schema），不讓迴圈解析自由文字。
- 固定的 system prompt 加上快取斷點；變動內容只放在 user 訊息。
- 依角色設定 effort；Opus 角色開啟 server-side fallback。
- 不論成功或失敗，都回報 token 用量與成本（CallMeta），供預算計量。
"""

from __future__ import annotations

import time
from typing import Any

import anthropic
import pydantic

from seo_intel.config import ModelsConfig
from seo_intel.env import load_env_file
from seo_intel.llm.cost import compute_usd
from seo_intel.llm.types import (
    CallMeta,
    LLMFatalError,
    LLMRefusalError,
    LLMResult,
    LLMSchemaError,
    LLMTransientError,
    LLMTruncatedError,
    T,
    TokenUsage,
)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
_TRANSIENT_STATUS = {408, 409, 429}


class AnthropicLLM:
    def __init__(self, models: ModelsConfig, client: Any | None = None) -> None:
        self.models = models
        if client is None:
            load_env_file()
            client = anthropic.Anthropic()
        self._client = client

    def build_request(self, role: str, *, system: str, user: str, schema: type[T]) -> dict[str, Any]:
        if role not in self.models.roles:
            raise LLMFatalError(f"未定義的 LLM 角色：{role}")
        cfg = self.models.roles[role]
        request: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_format": schema,
            "output_config": {"effort": cfg.effort},
        }
        if cfg.fallback:
            request["betas"] = [FALLBACK_BETA]
            request["fallbacks"] = "default"
        return request

    def call(self, role: str, *, system: str, user: str, schema: type[T]) -> LLMResult[T]:
        request = self.build_request(role, system=system, user=user, schema=schema)
        started = time.perf_counter()
        try:
            response = self._client.beta.messages.parse(**request)
        except anthropic.RateLimitError as e:
            raise LLMTransientError(f"速率限制：{e.message}") from e
        except anthropic.APIConnectionError as e:  # 含逾時
            raise LLMTransientError(f"連線失敗或逾時：{e}") from e
        except anthropic.APIStatusError as e:
            if e.status_code >= 500 or e.status_code in _TRANSIENT_STATUS:
                raise LLMTransientError(f"API 暫時性錯誤 {e.status_code}：{e.message}") from e
            raise LLMFatalError(f"API 錯誤 {e.status_code}：{e.message}") from e
        except pydantic.ValidationError as e:
            raise LLMSchemaError(f"輸出不符合 {schema.__name__}：{e}") from e
        except TypeError as e:
            # SDK 在送出請求時才發現沒有任何憑證，拋出的是一般 TypeError
            if "authentication" in str(e):
                raise LLMFatalError("找不到 Anthropic 憑證：請在專案根目錄的 .env 填入 ANTHROPIC_API_KEY") from e
            raise

        meta = self._meta(role, request["model"], response, started)

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None)
            raise LLMRefusalError(f"模型拒絕回應（類別：{category}）", meta, category=category)
        if response.stop_reason == "max_tokens":
            raise LLMTruncatedError(f"輸出觸及 max_tokens={request['max_tokens']}", meta)
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise LLMSchemaError(f"沒有取得 {schema.__name__} 結構化輸出", meta)
        return LLMResult(parsed=parsed, meta=meta)

    def _meta(self, role: str, requested_model: str, response: Any, started: float) -> CallMeta:
        u = response.usage
        usage = TokenUsage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", None) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", None) or 0,
        )
        served_model = getattr(response, "model", None) or requested_model
        pricing = self.models.pricing.get(served_model) or self.models.pricing[requested_model]
        iterations = getattr(u, "iterations", None) or []
        return CallMeta(
            role=role,
            model=served_model,
            usage=usage,
            usd=compute_usd(pricing, usage),
            stop_reason=response.stop_reason,
            served_by_fallback=any(getattr(it, "type", None) == "fallback_message" for it in iterations),
            request_id=getattr(response, "_request_id", None),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
