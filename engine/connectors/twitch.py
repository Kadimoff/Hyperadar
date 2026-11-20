"""Twitch via the public web GQL endpoint — no developer key.

Uses the same client id twitch.tv itself sends. It is unofficial: when Twitch
changes the schema this module is the only thing to fix. Official Helix keys
can be slotted in later behind the same methods.
"""
from __future__ import annotations

import json
from typing import Optional

from ..models import Moment, Stream
from .base import Connector, ConnectorError, expand_tag

GQL = "https://gql.twitch.tv/gql"
WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

STREAMS_Q = """query($id: ID!, $first: Int!, $after: Cursor) {
  game(id: $id) { id name viewersCount
    streams(first: $first, after: $after, options: {sort: VIEWER_COUNT}) {
      edges { cursor node { id title viewersCount createdAt language
        previewImageURL(width: 440, height: 248)
        broadcaster { id login displayName profileImageURL(width: 70) } } }
      pageInfo { hasNextPage } } } }"""

CLIPS_Q = """query($id: ID!, $first: Int!) {
  game(id: $id) { clips(first: $first, criteria: {filter: LAST_DAY}) {
    edges { node { id slug title viewCount createdAt durationSeconds url thumbnailURL language
      broadcaster { login displayName } video { id } videoOffsetSeconds } } } } }"""

SEARCH_Q = """query($q: String!) { searchCategories(query: $q, first: 5) {
  edges { node { id name viewersCount } } } }"""

TOKEN_Q = """query($login: String!) { streamPlaybackAccessToken(channelName: $login,
  params: {platform: "web", playerBackend: "mediaplayer", playerType: "site"}) { value signature } }"""


def parse_streams(payload: dict) -> tuple[list[Stream], Optional[str], bool]:
    game = (payload.get("data") or {}).get("game") or {}
    conn = game.get("streams") or {}
    out, cursor = [], None
    for edge in conn.get("edges") or []:
        n = edge.get("node") or {}
        b = n.get("broadcaster") or {}
        if not b.get("login"):
            continue
        cursor = edge.get("cursor")
        out.append(Stream(
            platform="twitch", channel=b["login"], display_name=b.get("displayName") or b["login"],
            title=n.get("title") or "", viewers=int(n.get("viewersCount") or 0),
            started_at=n.get("createdAt"), language=(n.get("language") or "").lower() or None,
            thumbnail=n.get("previewImageURL"), avatar=b.get("profileImageURL"),
            url=f"https://www.twitch.tv/{b['login']}", category=game.get("name")))
    return out, cursor, bool((conn.get("pageInfo") or {}).get("hasNextPage"))


def parse_clips(payload: dict) -> list[Moment]:
    game = (payload.get("data") or {}).get("game") or {}
    out = []
    for edge in ((game.get("clips") or {}).get("edges") or []):
        n = edge.get("node") or {}
        b = n.get("broadcaster") or {}
        vod = (n.get("video") or {}).get("id")
        off = n.get("videoOffsetSeconds")
        out.append(Moment(
            platform="twitch", id=n["slug"], title=n.get("title") or "", channel=b.get("login") or "",
            display_name=b.get("displayName") or b.get("login") or "", views=int(n.get("viewCount") or 0),
            created_at=n.get("createdAt"), url=n.get("url") or "", kind="clip",
            duration=n.get("durationSeconds"), thumbnail=n.get("thumbnailURL"),
            language=(n.get("language") or "").lower() or None,
            vod_url=(f"https://www.twitch.tv/videos/{vod}?t={int(off)}s" if vod and off is not None else None),
            embed_id=n["slug"]))
    return out


class Twitch(Connector):
    name, label, has_clips = "twitch", "Twitch", True

    async def _gql(self, query: str, variables: dict) -> dict:
        r = await self.client.post(GQL, headers={"Client-ID": WEB_CLIENT_ID},
                                   content=json.dumps({"query": query, "variables": variables}))
        if r.status_code != 200:
            raise ConnectorError(f"twitch gql http {r.status_code}")
        data = r.json()
        if data.get("errors") and not data.get("data"):
            raise ConnectorError(f"twitch gql: {data['errors'][0].get('message')}")
        return data

    async def resolve_category(self, tag: str) -> Optional[dict]:
        best = None
        for q in expand_tag(tag):
            data = await self._gql(SEARCH_Q, {"q": q})
            edges = (((data.get("data") or {}).get("searchCategories") or {}).get("edges") or [])
            for e in edges:
                n = e["node"]
                exact = n["name"].lower() == q.lower()
                cand = {"id": n["id"], "name": n["name"], "viewers": n.get("viewersCount") or 0, "exact": exact}
                if exact:
                    return cand
                if best is None or cand["viewers"] > best["viewers"]:
                    best = cand
            if best:
                break
        return best

    async def live_streams(self, category: dict, limit: int = 100) -> list[Stream]:
        out, after = [], None
        while len(out) < limit:
            data = await self._gql(STREAMS_Q, {"id": category["id"], "first": min(100, limit - len(out)), "after": after})
            page, after, more = parse_streams(data)
            out.extend(page)
            if not more or not page:
                break
        return out

    async def moments(self, category: dict, limit: int = 60) -> list[Moment]:
        return parse_clips(await self._gql(CLIPS_Q, {"id": category["id"], "first": min(limit, 100)}))

    async def hls_master(self, login: str) -> Optional[str]:
        """Master playlist text for quality probing, or None when Twitch refuses."""
        data = await self._gql(TOKEN_Q, {"login": login})
        tok = ((data.get("data") or {}).get("streamPlaybackAccessToken") or {})
        if not tok.get("value"):
            return None
        r = await self.client.get(f"https://usher.ttvnw.net/api/channel/hls/{login}.m3u8",
                                  params={"sig": tok["signature"], "token": tok["value"], "allow_source": "true",
                                          "allow_audio_only": "true", "fast_bread": "true", "p": "1"})
        return r.text if r.status_code == 200 else None

    def embed_url(self, item: dict, parent: str = "localhost") -> Optional[str]:
        if item.get("kind") == "clip":
            return f"https://clips.twitch.tv/embed?clip={item['embed_id']}&parent={parent}&autoplay=true"
        return f"https://player.twitch.tv/?channel={item['channel']}&parent={parent}&autoplay=true&muted=false"
