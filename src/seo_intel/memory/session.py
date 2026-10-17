"""每個 episode 的記憶工作階段：載入適用的經驗與 M2 先驗，並記錄哪些經驗在哪裡被套用。

套用紀錄存在 EpisodeState.lessons_applied（可寫入檢查點），REFLECT 依此更新經驗的效用。
"""

from __future__ import annotations

from seo_intel.core.models import EpisodeState
from seo_intel.memory import priors
from seo_intel.memory.models import Bias, Lesson
from seo_intel.memory.store import MemoryStore


class MemorySession:
    def __init__(self, store: MemoryStore | None, ablate: frozenset[str] = frozenset()) -> None:
        self.store = store
        self.use_m1 = store is not None and "M1" not in ablate
        self.use_m2 = store is not None and "M2" not in ablate
        self.lessons: list[Lesson] = []
        self.goal_type = "other"
        self.state: EpisodeState | None = None   # 綁定後，avoid() 命中時會自動記錄套用位置

    def load(self, goal_type: str) -> list[Lesson]:
        self.goal_type = goal_type
        if self.use_m1:
            self.lessons = [lesson for lesson in self.store.lessons("active") if lesson.matches(goal_type)]
        return self.lessons

    def biases(self, kind: str) -> list[tuple[Lesson, Bias]]:
        return [(lesson, b) for lesson in self.lessons for b in lesson.biases if b.type == kind]

    def texts(self) -> list[str]:
        return [f"[{lesson.id}] {lesson.lesson}" for lesson in self.lessons]

    @staticmethod
    def mark(state: EpisodeState, lesson: Lesson, where: str) -> None:
        places = state.lessons_applied.setdefault(lesson.id, [])
        if where not in places:
            places.append(where)

    # ---------- 給 Frontier 的掛勾 ----------

    def prior(self, tkey: str, n_urls: int, burst: float | None) -> priors.PriorInfo | None:
        if not self.use_m2:
            return None
        return priors.lookup(self.store, self.goal_type, tkey, n_urls, burst)

    def avoid(self, tkey: str) -> tuple[float, Lesson] | None:
        domain, path = tkey.split("/", 1)
        for lesson, b in self.biases("avoid_path"):
            if b.domain in ("*", domain) and f"/{path}".startswith(b.path):
                if self.state is not None:
                    self.mark(self.state, lesson, f"avoid:{tkey}")
                return 1.0, lesson
        return None
