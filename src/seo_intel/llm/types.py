"""LLM 層的共用型別：呼叫結果、token 用量、錯誤分類與 client 介面。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class CallMeta:
    """每次呼叫（不論成功或失敗）都會產生的可觀察資訊，供預算計量與 trace 使用。"""

    role: str
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    usd: float = 0.0
    stop_reason: str | None = None
    served_by_fallback: bool = False
    request_id: str | None = None
    latency_ms: int = 0


@dataclass(frozen=True)
class LLMResult(Generic[T]):
    parsed: T
    meta: CallMeta


class LLMClient(Protocol):
    def call(self, role: str, *, system: str, user: str, schema: type[T]) -> LLMResult[T]: ...


# ---------- 錯誤分類 ----------
# retryable 表示「由迴圈在步驟層級重試是否有意義」；SDK 本身已對 429 / 5xx 做過重試。

class LLMError(Exception):
    retryable = False

    def __init__(self, message: str, meta: CallMeta | None = None) -> None:
        super().__init__(message)
        self.meta = meta  # 已計費的呼叫也要計入預算


class LLMTransientError(LLMError):
    """速率限制、伺服器錯誤、連線失敗或逾時。"""

    retryable = True


class LLMFatalError(LLMError):
    """請求本身有誤（400 / 401 / 403 / 404），重試無意義。"""


class LLMRefusalError(LLMError):
    """模型（含 fallback 鏈）拒絕回應。"""

    def __init__(self, message: str, meta: CallMeta | None = None, category: str | None = None) -> None:
        super().__init__(message, meta)
        self.category = category


class LLMTruncatedError(LLMError):
    """輸出觸及 max_tokens 被截斷，結構化結果不完整。"""


class LLMSchemaError(LLMError):
    """輸出不符合 schema。"""

    retryable = True


class CassetteMissError(LLMError):
    """重播模式下找不到對應的錄製紀錄。"""
