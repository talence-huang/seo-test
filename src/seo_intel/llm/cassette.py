"""LLM 呼叫的錄製與重播。

真實呼叫有隨機性且會花錢；錄製一次後即可免費、確定性地重播整個迴圈，
用於回歸測試與重現某次 episode 的決策過程。
以 (角色, schema, system, user) 的雜湊作為鍵；相同提示重複出現時依錄製順序取用。
失敗的呼叫（例外與已計費的用量）也會錄下，重播時丟出同一種例外，讓步驟層的重試與預算計量一致。
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from dataclasses import asdict
from pathlib import Path

from pydantic import BaseModel

from seo_intel.llm import types
from seo_intel.llm.types import CallMeta, CassetteMissError, LLMClient, LLMError, LLMResult, T, TokenUsage


def cassette_key(role: str, system: str, user: str, schema: type[BaseModel]) -> str:
    raw = json.dumps([role, schema.__name__, system, user], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class RecordingLLM:
    def __init__(self, inner: LLMClient, path: str | Path) -> None:
        self.inner = inner
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def call(self, role: str, *, system: str, user: str, schema: type[T]) -> LLMResult[T]:
        key = cassette_key(role, system, user, schema)
        try:
            result = self.inner.call(role, system=system, user=user, schema=schema)
        except LLMError as e:
            self._write({"key": key, "schema": schema.__name__, "error": type(e).__name__, "message": str(e),
                         "meta": _meta_dict(e.meta) if e.meta else None})
            raise
        self._write({"key": key, "schema": schema.__name__, "parsed": result.parsed.model_dump(mode="json"),
                     "meta": _meta_dict(result.meta)})
        return result

    def _write(self, record: dict) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _meta_dict(meta: CallMeta) -> dict:
    d = asdict(meta)
    d["usage"] = meta.usage.model_dump()
    return d


def _meta(d: dict) -> CallMeta:
    return CallMeta(**{**d, "usage": TokenUsage.model_validate(d["usage"])})


class ReplayLLM:
    def __init__(self, path: str | Path) -> None:
        self.records: dict[str, deque[dict]] = defaultdict(deque)
        with Path(path).open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    self.records[rec["key"]].append(rec)

    def call(self, role: str, *, system: str, user: str, schema: type[T]) -> LLMResult[T]:
        key = cassette_key(role, system, user, schema)
        queue = self.records.get(key)
        if not queue:
            raise CassetteMissError(f"錄製檔中沒有角色 {role} / {schema.__name__} 的這組提示（key={key}）")
        rec = queue.popleft()
        if "error" in rec:
            cls = getattr(types, rec["error"], LLMError)
            raise cls(rec["message"], _meta(rec["meta"]) if rec["meta"] else None)
        return LLMResult(parsed=schema.model_validate(rec["parsed"]), meta=_meta(rec["meta"]))
