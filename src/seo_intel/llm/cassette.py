"""LLM 呼叫的錄製與重播。

真實呼叫有隨機性且會花錢；錄製一次後即可免費、確定性地重播整個迴圈，
用於回歸測試與重現某次 episode 的決策過程。
以 (角色, schema, system, user) 的雜湊作為鍵；相同提示重複出現時依錄製順序取用。
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from dataclasses import asdict
from pathlib import Path

from pydantic import BaseModel

from seo_intel.llm.types import CallMeta, CassetteMissError, LLMClient, LLMResult, T, TokenUsage


def cassette_key(role: str, system: str, user: str, schema: type[BaseModel]) -> str:
    raw = json.dumps([role, schema.__name__, system, user], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class RecordingLLM:
    def __init__(self, inner: LLMClient, path: str | Path) -> None:
        self.inner = inner
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def call(self, role: str, *, system: str, user: str, schema: type[T]) -> LLMResult[T]:
        result = self.inner.call(role, system=system, user=user, schema=schema)
        meta = asdict(result.meta)
        meta["usage"] = result.meta.usage.model_dump()
        record = {
            "key": cassette_key(role, system, user, schema),
            "schema": schema.__name__,
            "parsed": result.parsed.model_dump(mode="json"),
            "meta": meta,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return result


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
        m = dict(rec["meta"])
        m["usage"] = TokenUsage.model_validate(m["usage"])
        return LLMResult(parsed=schema.model_validate(rec["parsed"]), meta=CallMeta(**m))
