"""Hype score: 0–100 from four signals, each normalised within the tag's streams.

A signal a stream does not have (YouTube has no clips, chat not tracked yet,
growth before 2.5 min of history) counts as neutral (0.5): the stream is neither
punished nor rewarded for what its platform does not expose. Dropping the
signal and renormalising instead lets a one-signal stream outrank everything
on that one signal alone.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

DEFAULT_WEIGHTS = {"size": 0.25, "growth": 0.30, "chat": 0.25, "clips": 0.20}
GROWTH_WINDOW = 600     # compare with ~10 minutes ago
GROWTH_MIN_AGE = 150    # need at least 2.5 minutes of history


def growth_ratio(history: list[tuple[float, int]], now_ts: float, viewers: int) -> Optional[float]:
    """Relative viewer change versus the snapshot closest to GROWTH_WINDOW ago."""
    past = [(ts, v) for ts, v in history if now_ts - ts >= GROWTH_MIN_AGE]
    if not past:
        return None
    ts, v = min(past, key=lambda p: abs((now_ts - p[0]) - GROWTH_WINDOW))
    return (viewers - v) / max(v, 50)


def percentile_ranks(values: dict[str, float]) -> dict[str, float]:
    """Rank in 0..1 (ties share the average rank). One value -> 0.5."""
    if not values:
        return {}
    items = sorted(values.items(), key=lambda kv: kv[1])
    n = len(items)
    if n == 1:
        return {items[0][0]: 0.5}
    out, i = {}, 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        r = ((i + j) / 2) / (n - 1)
        for k in range(i, j + 1):
            out[items[k][0]] = r
        i = j + 1
    return out


def clip_heat(clips: list[dict], now: Optional[datetime] = None) -> dict[str, float]:
    """Per channel: recency-weighted clip activity from the category's last-day clips."""
    now = now or datetime.now(timezone.utc)
    heat: dict[str, float] = {}
    for c in clips:
        try:
            created = datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))
        except (KeyError, ValueError, AttributeError):
            continue
        age_h = max(0.0, (now - created).total_seconds() / 3600)
        w = math.exp(-age_h / 6) * (1 + math.log10(1 + int(c.get("views") or 0)))
        key = f"{c['platform']}:{c['channel']}"
        heat[key] = heat.get(key, 0.0) + w
    return heat


def components(streams: list[dict]) -> dict[str, dict[str, Optional[float]]]:
    """streams: [{key, viewers, growth (ratio|None), chat_per_min (|None), clip_heat (|None)}]."""
    if not streams:
        return {}
    size_rank = percentile_ranks({s["key"]: s["viewers"] for s in streams})
    chat_rel = {s["key"]: s["chat_per_min"] / max(s["viewers"] / 1000, 0.2)
                for s in streams if s.get("chat_per_min") is not None}
    chat_rank = percentile_ranks(chat_rel)
    clip_rank = percentile_ranks({s["key"]: s["clip_heat"] for s in streams if s.get("clip_heat") is not None})
    out = {}
    for s in streams:
        g = s.get("growth")
        out[s["key"]] = {
            "size": size_rank[s["key"]] if len(streams) > 1 else 1.0,
            "growth": None if g is None else (max(-0.3, min(0.6, g)) + 0.3) / 0.9,
            "chat": chat_rank.get(s["key"]),
            "clips": clip_rank.get(s["key"]),
        }
    return out


NEUTRAL = 0.5


def score(comp: dict[str, Optional[float]], weights: Optional[dict] = None) -> float:
    weights = weights or DEFAULT_WEIGHTS
    num = den = 0.0
    for k, w in weights.items():
        if w <= 0:
            continue
        v = comp.get(k)
        num += w * (NEUTRAL if v is None else v)
        den += w
    return round(100 * num / den, 1) if den else 0.0
