"""trace.jsonl：每一步一行，供重播、timeline 與事後分析。"""

from __future__ import annotations

from pathlib import Path

from seo_intel.core.models import EpisodeState
from seo_intel.observe.record import StepRecord


class TraceWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, record: StepRecord) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(record.model_dump_json() + "\n")

    def finish(self, state: EpisodeState) -> None:
        pass


def read_trace(path: Path) -> list[StepRecord]:
    if not path.is_file():
        return []
    return [StepRecord.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def truncate_trace(path: Path, max_seq: int) -> None:
    """續跑時捨棄檢查點之後的紀錄，避免重複。"""
    records = [r for r in read_trace(path) if r.seq <= max_seq]
    path.write_text("".join(r.model_dump_json() + "\n" for r in records), encoding="utf-8")
