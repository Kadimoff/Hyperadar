"""Kick via the endpoints kick.com's own web app calls — no developer key.

Gotcha: ``/stream/livestreams`` silently falls back to the global popular list
when the subcategory slug is wrong, so the slug is validated against
``/api/v1/subcategories/{slug}`` first and results are filtered by category.
"""
from __future__ import annotations

from typing import Optional

from ..models import Moment, Stream
from .base import BROWSER_UA, Connector, ConnectorError, expand_tag, slugify

BASE = "https://kick.com"
HEADERS = {"User-Agent": BROWSER_UA, "Accept": "application/json"}


def _iso(kick_time: Optional[str]) -> Optional[str]:
    # "2026-11-19 08:19:10" (UTC) -> "2026-11-19T08:19:10Z"
    if not kick_time:
        return None
    return kick_time.replace(" ", "T") + ("" if kick_time.endswith("Z") else "Z")


def parse_livestreams(payload: dict, slug: str) -> list[Stream]:
    out = []
    for x in payload.get("data") or []:
        if not any(c.get("slug") == slug for c in x.get("categories") or []):
            continue
        ch = x.get("channel") or {}
        user = ch.get("user") or {}
        if not ch.get("slug"):
            continue
        cat = next((c.get("name") for c in x.get("categories") or [] if c.get("slug") == slug), None)
        out.append(Stream(
            platform="kick", channel=ch["slug"], display_name=user.get("username") or ch["slug"],
            title=x.get("session_title") or "", viewers=int(x.get("viewer_count") or 0),
            started_at=_iso(x.get("start_time")), language=(x.get("language") or "").lower() or None,
            thumbnail=(x.get("thumbnail") or {}).get("src"), avatar=user.get("profilepic"),
            url=f"https://kick.com/{ch['slug']}", playback_url=ch.get("playback_url"), category=cat))
    return out


def parse_clips(payload: dict) -> list[Moment]:
    out = []
    for c in payload.get("clips") or []:
        ch = c.get("channel") or {}
        out.append(Moment(
            platform="kick", id=c["id"], title=c.get("title") or "", channel=ch.get("slug") or "",
            display_name=ch.get("username") or ch.get("slug") or "", views=int(c.get("view_count") or c.get("views") or 0),
            created_at=c.get("created_at"), url=f"https://kick.com/{ch.get('slug')}/clips/{c['id']}",
            kind="clip", duration=c.get("duration"), thumbnail=c.get("thumbnail_url"),
            extra={"hls": c.get("video_url") or c.get("clip_url")}))
    return out


class Kick(Connector):
    name, label, has_clips = "kick", "Kick", True

    async def _get(self, path: str, params: Optional[dict] = None):
        r = await self.client.get(BASE + path, params=params, headers=HEADERS)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise ConnectorError(f"kick http {r.status_code} {path}")
        return r.json()

    async def resolve_category(self, tag: str) -> Optional[dict]:
        tried = set()
        for q in expand_tag(tag):
            slug = slugify(q)
            if slug in tried:
                continue
            tried.add(slug)
            data = await self._get(f"/api/v1/subcategories/{slug}")
            if data and data.get("slug"):
                return {"id": data["slug"], "name": data.get("name"), "viewers": data.get("viewers") or 0, "exact": True}
        return None

    async def live_streams(self, category: dict, limit: int = 96) -> list[Stream]:
        out, page = [], 1
        while len(out) < limit and page <= 4:
            data = await self._get("/stream/livestreams/en", {"page": page, "limit": 32,
                                                              "subcategory": category["id"], "sort": "desc"})
            if not data:
                break
            batch = parse_livestreams(data, category["id"])
            out.extend(batch)
            # a page with nothing from our category means we are in fallback territory: stop
            if not batch or not data.get("next_page_url"):
                break
            page += 1
        seen, uniq = set(), []
        for s in out:
            if s.channel not in seen:
                seen.add(s.channel)
                uniq.append(s)
        return uniq[:limit]

    async def moments(self, category: dict, limit: int = 60) -> list[Moment]:
        data = await self._get(f"/api/v2/categories/{category['id']}/clips", {"sort": "view", "time": "day"})
        return parse_clips(data or {})[:limit]

    async def hls_master(self, slug: str) -> Optional[str]:
        """The listing's playback_url has no token (403); the channel endpoint's does."""
        data = await self._get(f"/api/v2/channels/{slug}")
        url = (data or {}).get("playback_url")
        if not url:
            return None
        r = await self.client.get(url, headers={"User-Agent": BROWSER_UA, "Origin": BASE, "Referer": BASE + "/"})
        return r.text if r.status_code == 200 else None

    def embed_url(self, item: dict, parent: str = "localhost") -> Optional[str]:
        if item.get("kind") == "clip":
            return None  # no iframe for clips: the dashboard plays extra.hls with hls.js
        return f"https://player.kick.com/{item['channel']}?autoplay=true"
