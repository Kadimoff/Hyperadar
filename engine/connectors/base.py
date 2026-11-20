"""The contract every platform connector fulfils.

A connector only reads public data. Adding a platform means one new module
that subclasses ``Connector`` and a line in ``registry.py``.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

import httpx

from ..models import Moment, Stream

BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

ROMAN = {"1": "i", "2": "ii", "3": "iii", "4": "iv", "5": "v", "6": "vi",
         "7": "vii", "8": "viii", "9": "ix", "10": "x"}
ALIASES = {"gta": "grand theft auto", "cod": "call of duty", "rdr": "red dead redemption",
           "lol": "league of legends", "cs": "counter-strike"}


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def expand_tag(tag: str) -> list[str]:
    """'GTA 6' -> ['GTA 6', 'grand theft auto 6', 'grand theft auto vi', ...] (unique, ordered)."""
    words = tag.strip().lower().split()
    out = [tag.strip()]
    expanded = [ALIASES.get(w, w) for w in words]
    out.append(" ".join(expanded))
    out.append(" ".join(ROMAN.get(w, w) for w in expanded))
    out.append(" ".join(ROMAN.get(w, w) for w in words))
    seen, uniq = set(), []
    for o in out:
        if o.lower() not in seen:
            seen.add(o.lower())
            uniq.append(o)
    return uniq


class ConnectorError(Exception):
    pass


class Connector:
    name: str = ""
    label: str = ""
    has_clips: bool = False

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def resolve_category(self, tag: str) -> Optional[dict]:
        """Map a free-text tag to this platform's category: {'id', 'name', ...} or None."""
        raise NotImplementedError

    async def live_streams(self, category: dict, limit: int = 100) -> list[Stream]:
        raise NotImplementedError

    async def moments(self, category: dict, limit: int = 60) -> list[Moment]:
        return []

    def embed_url(self, item: dict, parent: str = "localhost") -> Optional[str]:
        return None
