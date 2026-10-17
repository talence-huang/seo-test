"""排名時間序列的統計。所有數字由這裡計算，LLM 只負責詮釋。"""

from __future__ import annotations

from datetime import date
from statistics import mean, median, pstdev


def change_stats(points: list[dict], baseline_end: date, recent_days: int = 14) -> dict | None:
    """以 baseline_end 之前為基準期，比較最近 recent_days 天的平均排名；正值代表排名變差。"""
    series = [(date.fromisoformat(p["date"]), float(p["position"])) for p in points]
    base = [v for d, v in series if d < baseline_end]
    recent = [v for _, v in series[-recent_days:]]
    if len(base) < 7 or len(recent) < 7:
        return None
    b, r = mean(base), mean(recent)
    delta = r - b
    threshold = max(1.0, 2 * pstdev(base))
    onset = None
    if abs(delta) >= threshold:
        sign = 1 if delta > 0 else -1
        values = [v for _, v in series]
        for i in range(6, len(series)):
            d = series[i][0]
            if d < baseline_end:
                continue
            rolling = mean(values[i - 6: i + 1])
            if sign * (rolling - b) >= threshold:
                onset = series[i - 6][0]  # 7 日窗口的起點
                break
    urls_before = [p["url"] for p in points if date.fromisoformat(p["date"]) < baseline_end]
    return {
        "baseline": round(b, 2),
        "recent": round(r, 2),
        "delta": round(delta, 2),
        "threshold": round(threshold, 2),
        "onset": onset.isoformat() if onset else None,
        "url_before": urls_before[-1] if urls_before else None,
        "url_recent": points[-1]["url"] if points else None,
    }


def aggregate(per_query: dict[str, dict]) -> dict:
    deltas = [s["delta"] for s in per_query.values()]
    onsets = sorted(s["onset"] for s in per_query.values() if s["onset"])
    if not deltas:
        return {"n_queries": 0}
    movers = sorted(per_query.items(), key=lambda kv: -abs(kv[1]["delta"]))
    return {
        "n_queries": len(deltas),
        "mean_delta": round(mean(deltas), 2),
        "median_delta": round(median(deltas), 2),
        "n_significant": sum(1 for s in per_query.values() if s["onset"]),
        "onset_median": onsets[len(onsets) // 2] if onsets else None,
        "top_movers": [q for q, _ in movers[:3]],
    }
