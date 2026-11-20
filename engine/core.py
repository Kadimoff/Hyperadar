"""The engine: polls every tag on every platform, scores, and pushes updates."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from .chat.clients import KickChat, TwitchChat, YouTubeChat
from .chat.meter import ChatMeter
from .connectors.base import ConnectorError
from .connectors.registry import CONNECTORS
from .db import DB
from .models import Moment, Stream
from .scoring import hype as H
from .scoring.quality import best_variant, quality_score

log = logging.getLogger("engine")

DEFAULT_SETTINGS = {
    "weights": H.DEFAULT_WEIGHTS,
    "live_interval": 60,
    "moments_interval": 300,
    "chat_top_n": 15,
    "obs": {"host": "localhost", "port": 4455, "password": ""},
    "chopify_dir": "",
}

VOD_Q = """query($login: String!) { user(login: $login) {
  videos(first: 1, type: ARCHIVE, sort: TIME) { edges { node { id createdAt } } } } }"""


def _iso_ts(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Engine:
    def __init__(self, db_path: Path):
        self.db = DB(db_path)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(15, connect=8), follow_redirects=True,
                                        limits=httpx.Limits(max_connections=40))
        self.connectors = {C.name: C(self.client) for C in CONNECTORS}
        self.meter = ChatMeter(on_spike=self._on_spike)
        self.twitch_chat = TwitchChat(self.meter)
        self.kick_chat = KickChat(self.meter, self.client)
        self.youtube_chat = YouTubeChat(self.meter, self.client)
        self.settings: dict = {}
        self.tags: dict[int, dict] = {}
        self.ranked: dict[int, list[dict]] = {}
        self.updated: dict[int, float] = {}
        self.moments: dict[int, list[dict]] = {}
        self.spikes: dict[int, deque] = defaultdict(lambda: deque(maxlen=50))
        self.hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=40))
        self.quality: dict[str, dict] = {}
        self.permissions: dict[str, str] = {}
        self.prev_rank: dict[int, dict[str, int]] = {}
        self.health: dict[str, dict] = {c: {"ok": None, "error": None, "last_ok": None, "ms": None}
                                        for c in self.connectors}
        self.subscribers: set[asyncio.Queue] = set()
        self._wake: dict[int, asyncio.Event] = {}
        self._tasks: list[asyncio.Task] = []
        self._procs: dict[int, asyncio.subprocess.Process] = {}
        self._stopped: set[int] = set()
        self._running: dict[int, dict] = {}   # clip jobs in flight: key, out_dir, live, started

    # lifecycle --------------------------------------------------------------
    async def start(self) -> None:
        await self.db.open()
        stored = {k: await self.db.get_setting(k, v) for k, v in DEFAULT_SETTINGS.items()}
        self.settings = stored
        self.permissions = await self.db.permissions()
        for t in await self.db.tags():
            self.tags[t["id"]] = t
            for key, rows in (await self.db.history(t["id"], time.time() - 1200)).items():
                self.hist[key].extend(rows)
        self._tasks = [asyncio.create_task(self._live_loop()), asyncio.create_task(self._moments_loop()),
                       asyncio.create_task(self._quality_loop()), asyncio.create_task(self.twitch_chat.run()),
                       asyncio.create_task(self.kick_chat.run()), asyncio.create_task(self.youtube_chat.run())]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.client.aclose()
        await self.db.close()

    # pub/sub ----------------------------------------------------------------
    def publish(self, event: str, data: dict) -> None:
        for q in list(self.subscribers):
            if q.qsize() < 100:
                q.put_nowait((event, data))

    # tags -------------------------------------------------------------------
    async def resolve(self, name: str) -> dict:
        async def one(p, c):
            try:
                return p, await c.resolve_category(name)
            except Exception as e:
                log.warning("resolve %s on %s: %s", name, p, e)
                return p, None
        return dict(await asyncio.gather(*(one(p, c) for p, c in self.connectors.items())))

    async def add_tag(self, name: str) -> dict:
        mapping = await self.resolve(name)
        tag = await self.db.add_tag(name.strip(), mapping)
        self.tags[tag["id"]] = tag
        asyncio.create_task(self._refresh_tag(tag["id"]))
        return tag

    async def update_mapping(self, tag_id: int, platform: str, category: Optional[dict]) -> dict:
        tag = self.tags[tag_id]
        tag["mapping"][platform] = category
        await self.db.set_mapping(tag_id, tag["mapping"])
        asyncio.create_task(self._refresh_tag(tag_id))
        return tag

    async def delete_tag(self, tag_id: int) -> None:
        await self.db.delete_tag(tag_id)
        for d in (self.tags, self.ranked, self.moments, self.prev_rank, self.updated):
            d.pop(tag_id, None)
        self._update_chat_targets()

    async def _refresh_tag(self, tag_id: int) -> None:
        await self.poll_moments(tag_id)
        await self.poll_live(tag_id)

    # live -------------------------------------------------------------------
    async def _fetch(self, platform: str, coro):
        t0 = time.perf_counter()
        h = self.health[platform]
        try:
            res = await coro
            h.update(ok=True, error=None, last_ok=time.time(), ms=round((time.perf_counter() - t0) * 1000))
            return res
        except (ConnectorError, httpx.HTTPError, ValueError, KeyError) as e:
            h.update(ok=False, error=f"{type(e).__name__}: {e}"[:200])
            log.warning("%s: %s", platform, e)
            return None

    async def poll_live(self, tag_id: int) -> None:
        tag = self.tags.get(tag_id)
        if not tag:
            return
        jobs = [(p, self._fetch(p, self.connectors[p].live_streams(cat)))
                for p, cat in tag["mapping"].items() if cat and p in self.connectors]
        results = await asyncio.gather(*(j for _, j in jobs))
        now = time.time()
        streams: list[Stream] = []
        for (p, _), res in zip(jobs, results):
            if res is None:   # platform failed this round: keep its last known rows instead of dropping them
                streams += [Stream(**{k: v for k, v in r.items() if k in Stream.__dataclass_fields__})
                            for r in self.ranked.get(tag_id, []) if r["platform"] == p]
            else:
                streams += res
        heat = H.clip_heat([m for m in self.moments.get(tag_id, []) if m["kind"] == "clip"])
        rows = []
        for s in streams:
            rate = self.meter.rate(s.key)
            clip_platform = self.connectors[s.platform].has_clips
            rows.append({"key": s.key, "viewers": s.viewers,
                         "growth": H.growth_ratio(list(self.hist[s.key]), now, s.viewers),
                         "chat_per_min": rate["per_min"] if rate else None,
                         "clip_heat": heat.get(s.key, 0.0) if clip_platform else None,
                         "_s": s, "_rate": rate})
        comps = H.components(rows)
        w = self.settings["weights"]
        prev = self.prev_rank.get(tag_id, {})
        out = []
        for r in rows:
            s: Stream = r["_s"]
            c = comps[s.key]
            d = s.to_dict()
            started = _iso_ts(s.started_at)
            d.update(hype=H.score(c, w), components=c, growth=r["growth"], chat=r["_rate"],
                     clip_heat=r["clip_heat"], quality=self.quality.get(s.key, {}).get("q"),
                     uptime=(now - started) if started else None,
                     embed=self.connectors[s.platform].embed_url(d),
                     permission=self.permissions.get(s.key, "unknown"))
            out.append(d)
        out.sort(key=lambda d: d["hype"], reverse=True)
        for i, d in enumerate(out, 1):
            d["rank"] = i
            d["rank_change"] = (prev[d["key"]] - i) if d["key"] in prev else None
        self.prev_rank[tag_id] = {d["key"]: d["rank"] for d in out}
        self.ranked[tag_id] = out
        self.updated[tag_id] = now
        for d in out:
            self.hist[d["key"]].append((now, d["viewers"]))
        await self.db.add_snapshots(tag_id, [(d["key"], d["viewers"], d["hype"]) for d in out], now)
        self._update_chat_targets()
        self.publish("live", {"tag_id": tag_id, "ts": now, "count": len(out)})

    def _update_chat_targets(self) -> None:
        n = self.settings.get("chat_top_n", 15)
        best: dict[str, dict[str, int]] = {"twitch": {}, "kick": {}, "youtube": {}}
        video_of: dict[str, str] = {}
        for rows in self.ranked.values():
            for d in rows:
                if d["platform"] in best:
                    best[d["platform"]][d["channel"]] = max(best[d["platform"]].get(d["channel"], 0), d["viewers"])
                    if d.get("video_id"):
                        video_of[d["channel"]] = d["video_id"]
        top = {p: {ch for ch, _ in sorted(v.items(), key=lambda kv: -kv[1])[:n]} for p, v in best.items()}
        self.twitch_chat.set_channels(top["twitch"])
        self.kick_chat.set_channels(top["kick"])
        # YouTube is polled per video, so follow fewer of them
        yt = sorted(top["youtube"], key=lambda ch: -best["youtube"][ch])[:max(1, n // 2)]
        self.youtube_chat.set_channels({f"youtube:{ch}": video_of[ch] for ch in yt if ch in video_of})

    async def _live_loop(self) -> None:
        while True:
            start = time.time()
            await asyncio.gather(*(self.poll_live(t) for t in list(self.tags)), return_exceptions=True)
            interval = self.settings.get("live_interval", 60)
            await asyncio.sleep(max(5, interval - (time.time() - start)))

    # moments ----------------------------------------------------------------
    async def poll_moments(self, tag_id: int) -> None:
        tag = self.tags.get(tag_id)
        if not tag:
            return
        jobs = [(p, self._fetch(p, self.connectors[p].moments(cat)))
                for p, cat in tag["mapping"].items() if cat and p in self.connectors]
        results = await asyncio.gather(*(j for _, j in jobs))
        items: list[dict] = []
        for (p, _), res in zip(jobs, results):
            if res is None:
                items += [m for m in self.moments.get(tag_id, []) if m["platform"] == p and m["kind"] != "chat_spike"]
            else:
                for m in res:
                    d = m.to_dict()
                    d["embed"] = self.connectors[p].embed_url(d)
                    items.append(d)
        self.moments[tag_id] = self._score_moments(items)
        self.publish("moments", {"tag_id": tag_id})

    def _score_moments(self, items: list[dict]) -> list[dict]:
        now = time.time()
        vph = {}
        for m in items:
            age_h = max(0.25, (now - (_iso_ts(m["created_at"]) or now)) / 3600)
            m["views_per_hour"] = round(m["views"] / age_h)
            vph[m["key"]] = m["views_per_hour"]
        r_vph, r_views = {}, {}
        for kind in {m["kind"] for m in items}:   # a 30 s clip and a 10 h VOD are not comparable
            group = [m for m in items if m["kind"] == kind]
            r_vph.update(H.percentile_ranks({m["key"]: vph[m["key"]] for m in group}))
            r_views.update(H.percentile_ranks({m["key"]: m["views"] for m in group}))
        for m in items:
            m["hype"] = round(100 * (0.7 * r_vph.get(m["key"], 0) + 0.3 * r_views.get(m["key"], 0)), 1)
            m["permission"] = self.permissions.get(f"{m['platform']}:{m['channel']}", "unknown")
        return sorted(items, key=lambda m: m["hype"], reverse=True)

    def moments_for(self, tag_id: int) -> list[dict]:
        spikes = list(self.spikes.get(tag_id, []))
        return sorted(spikes + self.moments.get(tag_id, []), key=lambda m: m["hype"], reverse=True)

    async def _moments_loop(self) -> None:
        await asyncio.sleep(1)
        while True:
            await asyncio.gather(*(self.poll_moments(t) for t in list(self.tags)), return_exceptions=True)
            await asyncio.sleep(self.settings.get("moments_interval", 300))

    # chat spikes ------------------------------------------------------------
    def _on_spike(self, key: str, info: dict) -> None:
        for tag_id, rows in self.ranked.items():
            s = next((d for d in rows if d["key"] == key), None)
            if s:
                asyncio.create_task(self._record_spike(tag_id, s, info))

    async def _record_spike(self, tag_id: int, s: dict, info: dict) -> None:
        started = _iso_ts(s.get("started_at"))
        offset = int(info["at"] - started - 20) if started else None
        vod_url = None
        if s["platform"] == "twitch" and offset is not None:
            try:
                data = await self.connectors["twitch"]._gql(VOD_Q, {"login": s["channel"]})
                edges = (((data.get("data") or {}).get("user") or {}).get("videos") or {}).get("edges") or []
                if edges:
                    vod_created = _iso_ts(edges[0]["node"]["createdAt"]) or started
                    vod_url = f"https://www.twitch.tv/videos/{edges[0]['node']['id']}?t={max(0, int(info['at'] - vod_created - 20))}s"
            except Exception as e:
                log.info("vod lookup failed for %s: %s", s["channel"], e)
        m = Moment(platform=s["platform"], id=f"{s['channel']}-{int(info['at'])}", kind="chat_spike",
                   title=f"Chat partlayışı · {info['msgs']} mesaj/10 san (z={info['z']})",
                   channel=s["channel"], display_name=s["display_name"], views=s["viewers"],
                   created_at=datetime.fromtimestamp(info["at"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   url=s["url"], thumbnail=s.get("thumbnail"), language=s.get("language"), vod_url=vod_url,
                   extra={**info, "offset": offset})
        d = m.to_dict()
        d["embed"] = s.get("embed")
        d["hype"] = round(min(100.0, 60 + info["z"] * 5 + (info["hype"] / max(info["msgs"], 1)) * 20), 1)
        d["permission"] = s.get("permission", "unknown")
        self.spikes[tag_id].appendleft(d)
        self.publish("spike", {"tag_id": tag_id, "moment": d})

    # quality ----------------------------------------------------------------
    async def _quality_loop(self) -> None:
        await asyncio.sleep(8)
        while True:
            now = time.time()
            todo = []
            for rows in self.ranked.values():
                for d in rows[:30]:
                    q = self.quality.get(d["key"])
                    if d["platform"] in ("twitch", "kick") and (not q or now - q["ts"] > 900):
                        todo.append(d)
            for d in todo[:6]:
                try:
                    master = await self.connectors[d["platform"]].hls_master(d["channel"])
                    self.quality[d["key"]] = {"ts": time.time(), "q": quality_score(best_variant(master or ""))}
                except Exception as e:
                    self.quality[d["key"]] = {"ts": time.time(), "q": None}
                    log.info("quality probe %s: %s", d["key"], e)
            await asyncio.sleep(20)

    # helpers for the API ----------------------------------------------------
    def find_item(self, key: str) -> Optional[dict]:
        for rows in self.ranked.values():
            for d in rows:
                if d["key"] == key:
                    return d
        for tag_id in self.tags:
            for m in self.moments_for(tag_id):
                if m["key"] == key:
                    return m
        return None

    def status(self) -> dict:
        return {"platforms": self.health,
                "chat": {"twitch": {"status": self.twitch_chat.status, "channels": len(self.twitch_chat.joined)},
                         "kick": {"status": self.kick_chat.status, "channels": len(self.kick_chat.subscribed)},
                         "youtube": {"status": self.youtube_chat.status, "channels": len(self.youtube_chat.joined)}},
                "tags": {t: {"updated": self.updated.get(t), "streams": len(self.ranked.get(t, []))} for t in self.tags}}
