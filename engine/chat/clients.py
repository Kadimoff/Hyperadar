"""Anonymous chat readers: Twitch IRC (justinfan) and Kick Pusher.

Both keep one websocket, follow a desired channel set the scheduler updates,
and reconnect with backoff. Messages go straight into the ChatMeter.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re as _re
import uuid

import httpx
import websockets

from ..connectors.base import BROWSER_UA
from .meter import ChatMeter

log = logging.getLogger("chat")


class TwitchChat:
    URL = "wss://irc-ws.chat.twitch.tv:443"

    def __init__(self, meter: ChatMeter):
        self.meter = meter
        self.wanted: set[str] = set()
        self.joined: set[str] = set()
        self.status = "idle"
        self._ws = None

    def set_channels(self, logins: set[str]) -> None:
        self.wanted = {l.lower() for l in logins}

    async def _sync(self, ws) -> None:
        for ch in list(self.joined - self.wanted):
            await ws.send(f"PART #{ch}")
            self.joined.discard(ch)
            self.meter.drop(f"twitch:{ch}")
        for ch in list(self.wanted - self.joined)[:15]:   # stay under the anonymous JOIN rate limit
            await ws.send(f"JOIN #{ch}")
            self.joined.add(ch)

    async def run(self) -> None:
        delay = 2
        while True:
            try:
                async with websockets.connect(self.URL, open_timeout=15, ping_interval=60) as ws:
                    self._ws, self.joined, self.status = ws, set(), "ok"
                    await ws.send("PASS SCHMOOPIIE")
                    await ws.send(f"NICK justinfan{random.randint(10000, 99999)}")
                    delay = 2
                    sync_task = asyncio.create_task(self._sync_loop(ws))
                    try:
                        async for raw in ws:
                            for line in raw.split("\r\n"):
                                if line.startswith("PING"):
                                    await ws.send("PONG :tmi.twitch.tv")
                                elif " PRIVMSG #" in line:
                                    rest = line.split(" PRIVMSG #", 1)[1]
                                    ch, _, text = rest.partition(" :")
                                    self.meter.add(f"twitch:{ch}", text)
                    finally:
                        sync_task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network blips: reconnect
                self.status = f"reconnecting ({type(e).__name__})"
                log.warning("twitch chat: %s", e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _sync_loop(self, ws) -> None:
        while True:
            await self._sync(ws)
            await asyncio.sleep(10)


class KickChat:
    CONFIG = "https://web.kick.com/api/v1/realtime/connection"
    FALLBACK = {"app_key": "34bf7a0ff419a2a775b9", "cluster": "us2"}

    def __init__(self, meter: ChatMeter, client: httpx.AsyncClient):
        self.meter = meter
        self.client = client
        self.wanted: set[str] = set()
        self.subscribed: dict[str, str] = {}      # slug -> pusher channel
        self.room_ids: dict[str, int] = {}
        self.status = "idle"

    def set_channels(self, slugs: set[str]) -> None:
        self.wanted = set(slugs)

    async def _credentials(self) -> dict:
        """Kick hands the Pusher key out at runtime; it has changed before, so never hardcode it."""
        body = {"client": {"id": str(uuid.uuid4()), "type": "web"},
                "capabilities": {"accepted_providers": [{"provider": "pusher"}]}}
        try:
            r = await self.client.post(self.CONFIG, json=body, headers={"User-Agent": BROWSER_UA})
            for c in r.json()["data"]["connections"]:
                if c.get("provider") == "pusher":
                    return c["credentials"]
        except Exception as e:
            log.warning("kick realtime config failed, using fallback key: %s", e)
        return self.FALLBACK

    async def _room_id(self, slug: str):
        if slug not in self.room_ids:
            r = await self.client.get(f"https://kick.com/api/v2/channels/{slug}",
                                      headers={"User-Agent": BROWSER_UA, "Accept": "application/json"})
            if r.status_code != 200:
                return None
            self.room_ids[slug] = r.json()["chatroom"]["id"]
        return self.room_ids[slug]

    async def _sync(self, ws) -> None:
        for slug in list(set(self.subscribed) - self.wanted):
            await ws.send(json.dumps({"event": "pusher:unsubscribe", "data": {"channel": self.subscribed.pop(slug)}}))
            self.meter.drop(f"kick:{slug}")
        for slug in list(self.wanted - set(self.subscribed)):
            try:
                rid = await self._room_id(slug)
            except Exception:
                continue
            if rid:
                chan = f"chatrooms.{rid}.v2"
                await ws.send(json.dumps({"event": "pusher:subscribe", "data": {"auth": "", "channel": chan}}))
                self.subscribed[slug] = chan

    async def run(self) -> None:
        delay = 2
        while True:
            try:
                cred = await self._credentials()
                url = (f"wss://ws-{cred['cluster']}.pusher.com/app/{cred['app_key']}"
                       "?protocol=7&client=js&version=8.4.0&flash=false")
                async with websockets.connect(url, open_timeout=15, ping_interval=None, max_size=2**22) as ws:
                    self.subscribed, self.status = {}, "ok"
                    delay = 2
                    by_chan = {}
                    sync_task = asyncio.create_task(self._sync_loop(ws))
                    try:
                        async for raw in ws:
                            m = json.loads(raw)
                            ev = m.get("event", "")
                            if ev == "pusher:ping":
                                await ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                            elif ev.endswith("ChatMessageEvent"):
                                if len(by_chan) != len(self.subscribed):
                                    by_chan = {v: k for k, v in self.subscribed.items()}
                                slug = by_chan.get(m.get("channel"))
                                if slug:
                                    try:
                                        text = json.loads(m["data"]).get("content", "")
                                    except Exception:
                                        text = ""
                                    self.meter.add(f"kick:{slug}", text)
                    finally:
                        sync_task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.status = f"reconnecting ({type(e).__name__})"
                log.warning("kick chat: %s", e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _sync_loop(self, ws) -> None:
        while True:
            await self._sync(ws)
            await asyncio.sleep(10)


class YouTubeChat:
    """Polls YouTube live chat through the web client's innertube endpoint — no API key.

    One lightweight poll loop per video; YouTube tells us how long to wait (timeoutMs).
    """
    PAGE = "https://www.youtube.com/live_chat"
    POLL = "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat"
    HEADERS = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}

    def __init__(self, meter: ChatMeter, client: httpx.AsyncClient):
        self.meter = meter
        self.client = client
        self.wanted: dict[str, str] = {}      # meter key -> video id
        self.tasks: dict[str, asyncio.Task] = {}
        self.status = "idle"

    def set_channels(self, wanted: dict[str, str]) -> None:
        self.wanted = dict(wanted)

    @property
    def joined(self) -> set[str]:
        return {k for k, t in self.tasks.items() if not t.done()}

    async def _bootstrap(self, video_id: str):
        r = await self.client.get(self.PAGE, params={"v": video_id, "is_popout": "1"}, headers=self.HEADERS)
        h = r.text
        key = _re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', h)
        ver = _re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', h)
        m = _re.search(r'(?:window\["ytInitialData"\]|var ytInitialData)\s*=\s*(\{.*?\});\s*</script>', h, _re.S)
        if not (key and ver and m):
            return None
        data = json.loads(m.group(1))
        conts = data["contents"]["liveChatRenderer"]["continuations"][0]
        return key.group(1), ver.group(1), next(iter(conts.values()))["continuation"]

    async def _follow(self, mkey: str, video_id: str) -> None:
        boot = await self._bootstrap(video_id)
        if not boot:
            return
        api_key, version, cont = boot
        body = {"context": {"client": {"clientName": "WEB", "clientVersion": version}}}
        while mkey in self.wanted:
            r = await self.client.post(self.POLL, params={"key": api_key, "prettyPrint": "false"},
                                       json={**body, "continuation": cont}, headers=self.HEADERS)
            lc = (r.json().get("continuationContents") or {}).get("liveChatContinuation")
            if not lc:
                return   # stream ended or chat disabled
            for a in lc.get("actions") or []:
                item = (a.get("addChatItemAction") or {}).get("item") or {}
                msg = item.get("liveChatTextMessageRenderer")
                if msg:
                    text = "".join(run.get("text", "") for run in (msg.get("message") or {}).get("runs") or [])
                    self.meter.add(mkey, text)
            nxt = next(iter(lc["continuations"][0].values()))
            cont = nxt["continuation"]
            await asyncio.sleep(min(max(nxt.get("timeoutMs", 5000), 1500), 10000) / 1000)

    async def run(self) -> None:
        while True:
            try:
                for mkey in list(self.tasks):
                    if mkey not in self.wanted:
                        self.tasks.pop(mkey).cancel()
                        self.meter.drop(mkey)
                for mkey, vid in self.wanted.items():
                    t = self.tasks.get(mkey)
                    if t is None or t.done():
                        self.tasks[mkey] = asyncio.create_task(self._follow(mkey, vid))
                self.status = "ok"
            except asyncio.CancelledError:
                for t in self.tasks.values():
                    t.cancel()
                raise
            except Exception as e:
                self.status = f"error ({type(e).__name__})"
                log.warning("youtube chat: %s", e)
            await asyncio.sleep(15)
