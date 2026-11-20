"""Text filters for clip safety (YouTube Shorts 18+ / community guidelines).

Pure keyword heuristics: they only see text (stream title, chat, transcript), never frames or audio.
`hard` hits reject a moment; `soft` hits only lower its score. Extend with the
`custom_blocklist` setting (list of words/phrases, treated as hard).
"""
from __future__ import annotations

import re
from typing import Iterable

# category -> (severity, regex). Kept deliberately conservative to limit false positives on game chat.
_RULES = {
    "18+": ("hard", r"\b(porn\w*|nsfw|onlyfans|nude\w*|nudes|hentai|sex\s?(tape|cam)|xxx|boobs?|tits|naked|"
                    r"strip\s?tease|fetish|cumshot|blowjob|18\s?\+)\b"),
    "nifrət": ("hard", r"\b(nazi\w*|heil|white\s?power|kkk|genocide\s+(is|was)\s+good)\b"),
    "özünə zərər": ("hard", r"\b(kys|kill\s+yourself|suicide|self[\s-]?harm|slit\s+(my|your)\s+wrist)\b"),
    "şiddət": ("hard", r"\b(beheading|gore|snuff|school\s+shooting)\b"),
    "söyüş": ("soft", r"\b(fuck\w*|shit\w*|bitch\w*|asshole|cunt|dick(head)?|bastard|motherfucker)\b"),
    "təhqir": ("soft", r"\b(retard\w*|idiot|moron|stfu)\b"),
}
_COMPILED = {k: (sev, re.compile(rx, re.I)) for k, (sev, rx) in _RULES.items()}

# Streams in these categories/titles are not game content; reject the spike outright.
_BAD_CATEGORY = re.compile(r"\b(pools?|hot\s*tubs?|beaches|asmr|slots|gambling|casino|18\s?\+|adult|nsfw)\b", re.I)


def scan(texts: Iterable[str], custom: Iterable[str] = ()) -> dict:
    """Return {"hard": [labels], "soft": [labels]} for the given texts."""
    blob = "\n".join(t for t in texts if t)
    out = {"hard": [], "soft": []}
    for label, (sev, rx) in _COMPILED.items():
        if rx.search(blob):
            out[sev].append(label)
    for word in custom or ():
        w = (word or "").strip()
        if w and re.search(r"\b" + re.escape(w) + r"\b", blob, re.I):
            out["hard"].append("xüsusi siyahı")
            break
    return out


def bad_category(*fields: str) -> bool:
    return any(f and _BAD_CATEGORY.search(f) for f in fields)
