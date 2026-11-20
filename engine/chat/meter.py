"""Counts chat messages per stream in 10-second buckets and spots spikes.

A spike is a bucket far above the stream's own recent baseline, so a 100K
channel and a 500-viewer channel are judged against themselves.
"""
from __future__ import annotations

import math
import re
import time
from collections import defaultdict, deque
from typing import Callable, Optional

BUCKET = 10          # seconds
KEEP = 60            # buckets kept (10 min)
BASELINE = 30        # buckets in the spike baseline (5 min)
SPIKE_Z = 3.0
SPIKE_MIN_MSGS = 8
SPIKE_COOLDOWN = 120  # seconds between spikes of one stream

HYPE_RE = re.compile(
    r"\b(kekw|lul|lmao+|omegalul|pog+|poggers|pogchamp|w+|ww+|clip\s*(it|that)|holy|no\s*way|wtf|"
    r"jaja+|xd+|ahah+|haha+|kkkk+|sheesh|insane|goat)\b", re.I)


def is_hype(text: str) -> bool:
    return bool(HYPE_RE.search(text or ""))


def spike_z(history: list[int], current: int) -> Optional[float]:
    """z-score of the current bucket against the baseline; None when the baseline is too short."""
    if len(history) < 6:
        return None
    mean = sum(history) / len(history)
    var = sum((h - mean) ** 2 for h in history) / len(history)
    std = max(math.sqrt(var), 1.0, mean * 0.15)
    return (current - mean) / std


class ChatMeter:
    def __init__(self, on_spike: Optional[Callable[[str, dict], None]] = None):
        self.buckets: dict[str, deque] = defaultdict(lambda: deque(maxlen=KEEP))  # (bucket_ts, msgs, hype)
        self.last_spike: dict[str, float] = {}
        self.samples: dict[str, deque] = defaultdict(lambda: deque(maxlen=600))  # (ts, user, text)
        self.on_spike = on_spike

    def add(self, key: str, text: str, now: Optional[float] = None, user: Optional[str] = None) -> None:
        now = now or time.time()
        self.samples[key].append((now, user, text))
        b = int(now // BUCKET) * BUCKET
        dq = self.buckets[key]
        if dq and dq[-1][0] == b:
            ts, n, h = dq[-1]
            dq[-1] = (ts, n + 1, h + (1 if is_hype(text) else 0))
        else:
            if dq:
                self._check_spike(key, dq[-1], b)
            dq.append((b, 1, 1 if is_hype(text) else 0))

    def _check_spike(self, key: str, closed: tuple, now_bucket: float) -> None:
        dq = self.buckets[key]
        hist = [n for ts, n, _ in list(dq)[-BASELINE - 1:-1]]
        z = spike_z(hist, closed[1])
        if z is None or z < SPIKE_Z or closed[1] < SPIKE_MIN_MSGS:
            return
        if now_bucket - self.last_spike.get(key, 0) < SPIKE_COOLDOWN:
            return
        self.last_spike[key] = now_bucket
        if self.on_spike:
            # messages of the spike bucket and the one before it (the ramp-up)
            win = [x for x in self.samples.get(key, ()) if closed[0] - BUCKET <= x[0] < closed[0] + BUCKET]
            self.on_spike(key, {"at": closed[0], "msgs": closed[1], "hype": closed[2], "z": round(z, 1),
                                "samples": win})

    def rate(self, key: str, window: int = 60, now: Optional[float] = None) -> Optional[dict]:
        """Messages and hype-word messages per minute over the last ``window`` seconds."""
        dq = self.buckets.get(key)
        if not dq:
            return None
        now = now or time.time()
        cutoff = now - window - BUCKET
        rows = [(n, h) for ts, n, h in dq if ts >= cutoff]
        if not rows:
            return {"per_min": 0.0, "hype_per_min": 0.0}
        span = max(window, BUCKET) / 60
        return {"per_min": round(sum(n for n, _ in rows) / span, 1),
                "hype_per_min": round(sum(h for _, h in rows) / span, 1)}

    def drop(self, key: str) -> None:
        self.buckets.pop(key, None)
        self.samples.pop(key, None)
