"""YouTube by reading the search page's ytInitialData — no Data API key, no quota.

YouTube has no "category" for games in search, so the tag is the query and the
results are filtered to titles that mention the tag (or its expansions).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from ..models import Moment, Stream
from .base import BROWSER_UA, Connector, ConnectorError, expand_tag

SEARCH = "https://www.youtube.com/results"
SP_LIVE = "EgJAAQ=="            # filter: live
SP_TODAY_BY_VIEWS = "CAMSBAgCEAE="  # upload date: today, type: video, sort: view count
HEADERS = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
_DATA = re.compile(r"var ytInitialData = (\{.*?\});</script>", re.S)


def _walk(o) -> Iterator[dict]:
    if isinstance(o, dict):
        if "videoRenderer" in o:
            yield o["videoRenderer"]
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _num(text: Optional[str]) -> int:
    if not text:
        return 0
    digits = re.sub(r"[^\d]", "", text.split(" ")[0])
    return int(digits) if digits else 0


def _text(node: Optional[dict]) -> str:
    if not node:
        return ""
    if "simpleText" in node:
        return node["simpleText"]
    return "".join(r.get("text", "") for r in node.get("runs") or [])


_AGO = re.compile(r"(\d+)\s*(second|minute|hour|day|week|month|year)s?\s*ago")
_UNIT = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800, "month": 2592000, "year": 31536000}


def ago_to_iso(text: str, now: Optional[datetime] = None) -> Optional[str]:
    m = _AGO.search(text or "")
    if not m:
        m2 = re.search(r"(\d+)\s*([smhdw])\s*ago", text or "")
        if not m2:
            return None
        n, u = int(m2.group(1)), {"s": "second", "m": "minute", "h": "hour", "d": "day", "w": "week"}[m2.group(2)]
    else:
        n, u = int(m.group(1)), m.group(2)
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(seconds=n * _UNIT[u])).strftime("%Y-%m-%dT%H:%M:%SZ")


def matches(title: str, terms: list[str]) -> bool:
    t = re.sub(r"\s+", " ", title.lower())
    return any(term.lower() in t for term in terms)


def _owner(v: dict) -> tuple[str, str, Optional[str]]:
    runs = (v.get("ownerText") or v.get("longBylineText") or {}).get("runs") or [{}]
    name = runs[0].get("text") or ""
    browse = ((runs[0].get("navigationEndpoint") or {}).get("browseEndpoint") or {})
    handle = (browse.get("canonicalBaseUrl") or "").lstrip("/") or browse.get("browseId") or name
    thumbs = ((((v.get("channelThumbnailSupportedRenderers") or {}).get("channelThumbnailWithLinkRenderer") or {})
               .get("thumbnail") or {}).get("thumbnails") or [{}])
    return handle, name, thumbs[-1].get("url")


def parse_live(data: dict, terms: list[str], category: str) -> list[Stream]:
    out = []
    for v in _walk(data):
        badges = [b.get("metadataBadgeRenderer", {}).get("style") for b in v.get("badges") or []]
        if "BADGE_STYLE_TYPE_LIVE_NOW" not in badges:
            continue
        title = _text(v.get("title"))
        if terms and not matches(title, terms):
            continue
        handle, name, avatar = _owner(v)
        thumbs = (v.get("thumbnail") or {}).get("thumbnails") or [{}]
        out.append(Stream(
            platform="youtube", channel=handle, display_name=name, title=title,
            viewers=_num(_text(v.get("viewCountText"))), thumbnail=thumbs[-1].get("url"), avatar=avatar,
            url=f"https://www.youtube.com/watch?v={v['videoId']}", video_id=v["videoId"], category=category))
    return out


def parse_videos(data: dict, terms: list[str], now: Optional[datetime] = None) -> list[Moment]:
    out = []
    for v in _walk(data):
        title = _text(v.get("title"))
        if terms and not matches(title, terms):
            continue
        created = ago_to_iso(_text(v.get("publishedTimeText")), now)
        if not created:
            continue
        handle, name, _ = _owner(v)
        thumbs = (v.get("thumbnail") or {}).get("thumbnails") or [{}]
        length = _text(v.get("lengthText"))
        secs = 0
        for part in length.split(":"):
            secs = secs * 60 + (int(part) if part.isdigit() else 0)
        out.append(Moment(
            platform="youtube", id=v["videoId"], title=title, channel=handle, display_name=name,
            views=_num(_text(v.get("viewCountText"))), created_at=created,
            url=f"https://www.youtube.com/watch?v={v['videoId']}", kind="video", duration=secs or None,
            thumbnail=thumbs[-1].get("url"), embed_id=v["videoId"],
            extra={"was_live": "Streamed" in _text(v.get("publishedTimeText"))}))
    return out


class YouTube(Connector):
    name, label, has_clips = "youtube", "YouTube", False

    async def _search(self, query: str, sp: str) -> dict:
        r = await self.client.get(SEARCH, params={"search_query": query, "sp": sp, "hl": "en", "gl": "US"},
                                  headers=HEADERS)
        if r.status_code != 200:
            raise ConnectorError(f"youtube http {r.status_code}")
        m = _DATA.search(r.text)
        if not m:
            raise ConnectorError("youtube: ytInitialData not found (consent page or layout change)")
        return json.loads(m.group(1))

    async def resolve_category(self, tag: str) -> Optional[dict]:
        terms = expand_tag(tag)
        return {"id": tag.strip(), "name": f'Axtarış: "{tag.strip()}"', "terms": terms, "exact": False}

    async def live_streams(self, category: dict, limit: int = 40) -> list[Stream]:
        data = await self._search(category["id"], SP_LIVE)
        return parse_live(data, category.get("terms") or [category["id"]], category["id"])[:limit]

    async def moments(self, category: dict, limit: int = 30) -> list[Moment]:
        data = await self._search(category["id"], SP_TODAY_BY_VIEWS)
        return parse_videos(data, category.get("terms") or [category["id"]])[:limit]

    def embed_url(self, item: dict, parent: str = "localhost") -> Optional[str]:
        vid = item.get("video_id") or item.get("embed_id")
        return f"https://www.youtube.com/embed/{vid}?autoplay=1" if vid else None
