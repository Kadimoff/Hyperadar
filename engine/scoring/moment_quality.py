"""Judges a chat spike: is it real, game-related reaction worth clipping, or spam / empty / unsafe?

Inputs are the chat messages inside the spike window plus the stream title/category.
`samples` is a list of (ts, user, text); None means the meter had no text (verdict stays neutral).
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Optional

from . import blocklist

# Reactions that suggest something strange/unexpected happened in the game.
WEIRD_RE = re.compile(
    r"\b(bug(ged)?|glitch\w*|wtf|what\s+the|how|no\s*way|omg|bro+|wait+|what|huh|impossible|"
    r"clip\s*(it|that)|insane|crazy|unreal|lmao+|kekw|omegalul|lol+|ahah+|haha+|xd+)\b", re.I)
CONFUSED_RE = re.compile(r"\?{1,}|\b(how|why|what)\b", re.I)
COMMAND_RE = re.compile(r"^\s*!\w+")
LINK_RE = re.compile(r"https?://|www\.|\.(com|tv|gg|ly)\b", re.I)

DEFAULTS = {"min_chatters": 4, "min_unique_ratio": 0.4, "custom_blocklist": []}


def _entropy(tokens: list[str]) -> float:
    if not tokens:
        return 0.0
    c = Counter(tokens)
    n = len(tokens)
    return -sum(v / n * math.log2(v / n) for v in c.values())


def chat_signals(samples: list) -> dict:
    texts = [(u, (t or "").strip()) for _, u, t in samples if (t or "").strip()]
    n = len(texts)
    if not n:
        return {"n": 0}
    users = {u for u, _ in texts if u}
    msgs = [t.lower() for _, t in texts]
    top_share = Counter(msgs).most_common(1)[0][1] / n
    cmd_share = sum(1 for m in msgs if COMMAND_RE.match(m)) / n
    link_share = sum(1 for m in msgs if LINK_RE.search(m)) / n
    reactions = [m for m in msgs if WEIRD_RE.search(m)]
    weird = sum(1 for m in msgs if CONFUSED_RE.search(m) or WEIRD_RE.search(m)) / n
    # reaction spread: reaction words in many *different* messages, not one copied line
    distinct_reactions = len(set(reactions))
    return {
        "n": n, "chatters": len(users) if users else None,
        "unique_ratio": (len(users) / n) if users else None,
        "top_share": top_share, "cmd_share": cmd_share, "link_share": link_share,
        "weird": weird, "distinct_reactions": distinct_reactions,
        "entropy": _entropy([w for m in msgs for w in m.split()]),
    }


def analyze(samples: Optional[list], info: dict, title: str = "", category: str = "",
            cfg: Optional[dict] = None) -> dict:
    """Return {ok, quality 0-100, reasons[], flags[]}. ok=False moments are hidden by default."""
    cfg = {**DEFAULTS, **(cfg or {})}
    reasons: list[str] = []
    flags: list[str] = []

    if blocklist.bad_category(category, title):
        reasons.append("18+ / oyun deyil")

    texts = [t for _, _, t in samples or []]
    scan = blocklist.scan([title, *texts], cfg.get("custom_blocklist"))
    reasons += [r for r in scan["hard"] if r not in reasons]
    flags += scan["soft"]

    z_norm = min(1.0, max(0.0, (info.get("z", 0) - 3) / 7))
    hype_ratio = info.get("hype", 0) / max(info.get("msgs", 1), 1)

    if not samples:     # no text kept: cannot judge chat quality, rely on the old signals
        chat_q, weird = 0.5, 0.0
    else:
        s = chat_signals(samples)
        if s["n"]:
            if s["chatters"] is not None and s["chatters"] < cfg["min_chatters"]:
                reasons.append("bot / az chatter")
            elif s["unique_ratio"] is not None and s["unique_ratio"] < cfg["min_unique_ratio"]:
                reasons.append("spam")
            if s["top_share"] > 0.5 and s["n"] >= 8:
                reasons.append("copypasta / təkrar")
            if s["cmd_share"] > 0.4 or s["link_share"] > 0.3:
                reasons.append("komanda / link spamı")
            if s["entropy"] < 1.5 and s["n"] >= 8:
                reasons.append("boş chat")
            ur = s["unique_ratio"] if s["unique_ratio"] is not None else 0.6
            chat_q = max(0.0, min(1.0, 0.5 * min(1.0, ur / 0.7) + 0.5 * (1 - s["top_share"])))
            weird = min(1.0, s["weird"] * 1.2) * (1.0 if s["distinct_reactions"] >= 3 else 0.5)
        else:
            chat_q, weird = 0.0, 0.0
            reasons.append("boş chat")

    quality = 100 * (0.4 * chat_q + 0.3 * weird + 0.2 * z_norm + 0.1 * min(1.0, hype_ratio * 2))
    if flags:
        quality *= 0.8
    reasons = list(dict.fromkeys(reasons))
    if reasons:
        quality = min(quality, 30)
    return {"ok": not reasons, "quality": round(quality, 1), "reasons": reasons, "flags": flags}
