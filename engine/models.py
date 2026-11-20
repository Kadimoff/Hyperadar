"""Platform-neutral records every connector returns."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class Stream:
    platform: str            # twitch | kick | youtube
    channel: str             # login / slug / channel id — unique within platform
    display_name: str
    title: str
    viewers: int
    started_at: Optional[str] = None   # ISO-8601 UTC
    language: Optional[str] = None
    thumbnail: Optional[str] = None
    avatar: Optional[str] = None
    url: str = ""                      # watch page
    video_id: Optional[str] = None     # YouTube video id
    playback_url: Optional[str] = None  # direct HLS when the platform hands it out (Kick)
    category: Optional[str] = None

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.channel}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        return d


@dataclass
class Moment:
    platform: str
    id: str
    title: str
    channel: str
    display_name: str
    views: int
    created_at: str                    # ISO-8601 UTC
    url: str
    kind: str = "clip"                 # clip | video | chat_spike
    duration: Optional[float] = None
    thumbnail: Optional[str] = None
    language: Optional[str] = None
    vod_url: Optional[str] = None      # source VOD at the moment's offset, when known
    embed_id: Optional[str] = None     # what the platform's embed needs (clip slug, video id)
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.kind}:{self.id}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        return d
