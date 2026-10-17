"""M2：URL 範本價值的跨 episode 學習（Beta 分布，階層式退縮）。

獎勵：成功 episode 中，抓取的頁面屬於「成立主張」的範本且確認有變動 → 命中；否則未命中。
先驗查詢由具體到一般：同網站的同一範本 → 同類路徑（例如 guide/{slug}）→ 範本特徵（規模 × 更新爆發度），
最後一層讓經驗能遷移到沒見過的網站。
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_OBS = 4          # 至少累積這麼多次觀察才使用該層級
MAX_STRENGTH = 8     # 先驗最多相當於 8 次觀察，避免壓過本次 episode 的實測


@dataclass(frozen=True)
class PriorInfo:
    alpha0: float
    beta0: float
    mean: float
    n: float
    source: str


def path_of(tkey: str) -> str:
    return tkey.split("/", 1)[1] if "/" in tkey else tkey


def size_bucket(n_urls: int) -> str:
    return "S" if n_urls < 200 else "M" if n_urls < 2000 else "L"


def burst_bucket(ratio: float | None) -> str:
    if ratio is None:
        return "?"
    return "B" if ratio >= 2.0 else "N" if ratio >= 1.3 else "C"


def keys(goal_type: str, tkey: str, n_urls: int, burst: float | None) -> list[tuple[str, str]]:
    return [("site", f"{goal_type}|{tkey}"),
            ("path", f"{goal_type}|{path_of(tkey)}"),
            ("feat", f"{goal_type}|{size_bucket(n_urls)}|{burst_bucket(burst)}")]


LABEL = {"site": "同網站範本", "path": "同類路徑", "feat": "範本特徵"}


def lookup(store, goal_type: str, tkey: str, n_urls: int, burst: float | None) -> PriorInfo | None:
    for scope, key in keys(goal_type, tkey, n_urls, burst):
        s = store.stat(scope, key)
        if s is None:
            continue
        a, b, _ = s
        n = a + b
        if n < MIN_OBS:
            continue
        mean = a / n
        k = min(MAX_STRENGTH, n)
        return PriorInfo(alpha0=1 + mean * k, beta0=1 + (1 - mean) * k, mean=round(mean, 3), n=n,
                         source=f"{LABEL[scope]} {key.split('|', 1)[1]}")
    return None
