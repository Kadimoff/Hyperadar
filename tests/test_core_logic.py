import json
from datetime import datetime, timezone
from pathlib import Path

from engine.actions.obs import auth_string
from engine.chat.meter import ChatMeter, is_hype, spike_z
from engine.connectors import kick, twitch, youtube
from engine.connectors.base import expand_tag, slugify
from engine.scoring import hype as H
from engine.scoring.quality import best_variant, quality_score

FX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


# --- tag expansion -------------------------------------------------------
def test_expand_tag_gta6():
    out = [o.lower() for o in expand_tag("GTA 6")]
    assert "grand theft auto vi" in out
    assert slugify("Grand Theft Auto VI") == "grand-theft-auto-vi"


# --- parsers ---------------------------------------------------------------
def test_twitch_streams_parse():
    streams, cursor, more = twitch.parse_streams(load("twitch_streams.json"))
    assert streams and streams[0].platform == "twitch"
    assert streams[0].viewers >= streams[-1].viewers
    assert streams[0].url.startswith("https://www.twitch.tv/")
    assert cursor and more


def test_twitch_clips_have_vod_offset():
    clips = twitch.parse_clips(load("twitch_clips.json"))
    assert clips
    c = clips[0]
    assert c.kind == "clip" and c.embed_id
    assert c.vod_url and "?t=" in c.vod_url


def test_kick_filters_out_other_categories():
    # fixture was fetched with a wrong slug: Kick returned a mixed fallback list
    payload = load("kick_livestreams.json")
    all_slugs = {c["slug"] for x in payload["data"] for c in x["categories"]}
    assert len(all_slugs) > 1
    streams = kick.parse_livestreams(payload, "grand-theft-auto-vi")
    assert streams
    assert all(s.category and "VI" in s.category for s in streams)
    assert len(streams) < len(payload["data"])


def test_kick_clips_parse():
    clips = kick.parse_clips(load("kick_clips.json"))
    assert clips and clips[0].extra["hls"].endswith(".m3u8")


def test_youtube_live_parse_and_filter():
    data = load("yt_live.json")
    terms = expand_tag("GTA 6")
    live = youtube.parse_live(data, terms, "GTA 6")
    assert live and all(s.viewers > 0 for s in live)
    assert all(youtube.matches(s.title, terms) for s in live)
    assert live[0].video_id


def test_youtube_today_videos():
    now = datetime(2026, 11, 20, 0, 40, tzinfo=timezone.utc)
    vids = youtube.parse_videos(load("yt_today.json"), expand_tag("GTA 6"), now)
    assert vids and vids[0].views > 1000 and vids[0].created_at.startswith("2026-11-1")


def test_ago_to_iso():
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    assert youtube.ago_to_iso("Streamed 3 hours ago", now) == "2026-11-20T09:00:00Z"
    assert youtube.ago_to_iso("Streamed 23h ago", now) == "2026-11-19T13:00:00Z"
    assert youtube.ago_to_iso("nonsense", now) is None


# --- scoring ---------------------------------------------------------------
def test_growth_ratio_needs_history():
    assert H.growth_ratio([], 1000, 500) is None
    assert H.growth_ratio([(950, 400)], 1000, 500) is None       # too recent
    g = H.growth_ratio([(400, 1000), (700, 1500)], 1000, 2000)    # closest to 10 min ago = ts 400
    assert abs(g - 1.0) < 1e-9


def test_missing_signals_are_neutral():
    full = {"size": 1.0, "growth": 1.0, "chat": 1.0, "clips": 1.0}
    yt = {"size": 1.0, "growth": 1.0, "chat": None, "clips": None}
    assert H.score(full) == 100.0
    # chat+clips weigh 0.45 and count as 0.5 -> 100 - 0.45*50
    assert H.score(yt) == 77.5
    # a one-signal stream can no longer beat a strong all-signal stream on size alone
    assert H.score({"size": 1.0, "growth": None, "chat": None, "clips": None}) < \
        H.score({"size": 0.9, "growth": 0.8, "chat": 0.8, "clips": 0.7})


def test_growing_small_stream_beats_flat_big_one():
    rows = [
        {"key": "a", "viewers": 100000, "growth": 0.0, "chat_per_min": 300, "clip_heat": 1.0},
        {"key": "b", "viewers": 5000, "growth": 0.6, "chat_per_min": 600, "clip_heat": 3.0},
        {"key": "c", "viewers": 2000, "growth": -0.2, "chat_per_min": 10, "clip_heat": 0.0},
    ]
    comps = H.components(rows)
    s = {k: H.score(v) for k, v in comps.items()}
    assert s["b"] > s["a"] > s["c"]


def test_percentile_ties():
    r = H.percentile_ranks({"a": 1, "b": 1, "c": 5})
    assert r["a"] == r["b"] == 0.25 and r["c"] == 1.0


# --- chat --------------------------------------------------------------------
def test_hype_words():
    assert is_hype("KEKW that was insane")
    assert is_hype("clip it!!")
    assert not is_hype("hello chat")


def test_spike_detection():
    hits = []
    m = ChatMeter(on_spike=lambda k, info: hits.append(info))
    t = 1_000_000.0
    for b in range(30):                       # 5 min of ~5 msgs / 10 s
        for i in range(5):
            m.add("twitch:x", "hi", now=t + b * 10 + i)
    for i in range(60):                       # burst
        m.add("twitch:x", "KEKW", now=t + 300 + i * 0.1)
    m.add("twitch:x", "after", now=t + 311)  # closes the burst bucket
    assert len(hits) == 1 and hits[0]["msgs"] == 60 and hits[0]["hype"] == 60
    assert spike_z([5] * 3, 50) is None


def test_chat_rate():
    m = ChatMeter()
    for i in range(30):
        m.add("k", "x", now=1000 + i)
    r = m.rate("k", window=60, now=1030)
    assert r["per_min"] == 30.0


# --- quality / obs -----------------------------------------------------------
MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=3422999,RESOLUTION=1280x720,FRAME-RATE=60.000
720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=9014194,RESOLUTION=1920x1080,FRAME-RATE=60.000
src.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=630000,RESOLUTION=640x360,FRAME-RATE=30.000
360.m3u8"""


def test_quality():
    v = best_variant(MASTER)
    assert v == {"width": 1920, "height": 1080, "fps": 60, "bandwidth": 9014194}
    q = quality_score(v)
    assert q["label"] == "1080p60" and q["score"] == 100
    assert quality_score(best_variant("")) is None


def test_obs_auth_follows_spec():
    # obs-websocket v5: secret = b64(sha256(password + salt)); auth = b64(sha256(secret + challenge))
    import base64, hashlib
    pw, salt, ch = "pw", "c2FsdA==", "Y2hhbGxlbmdl"
    secret = base64.b64encode(hashlib.sha256((pw + salt).encode()).digest()).decode()
    expected = base64.b64encode(hashlib.sha256((secret + ch).encode()).digest()).decode()
    assert auth_string(pw, salt, ch) == expected
    assert auth_string("other", salt, ch) != expected


def test_chopify_commands(tmp_path):
    from engine.actions.clipper import ClipperError, chopify_command
    (tmp_path / "chopify.py").write_text("")
    live = chopify_command(str(tmp_path), "https://www.twitch.tv/x", True, "/out")
    assert live[2:4] == ["--live", "https://www.twitch.tv/x"]
    assert "--projects-dir" in live and "--out" not in live
    assert live[live.index("--fit") + 1] == "cover"
    vod = chopify_command(str(tmp_path), "https://clip", False, "/out")
    assert vod[2] == "https://clip" and vod[vod.index("--out") + 1] == "/out"
    import pytest
    with pytest.raises(ClipperError):
        chopify_command(str(tmp_path / "missing"), "u", False, "/o")


def test_clip_source_never_sends_whole_vod():
    import pytest
    from engine.actions.clipper import ClipperError, clip_source
    clip = {"kind": "clip", "url": "https://www.twitch.tv/a/clip/X", "vod_url": "https://www.twitch.tv/videos/1?t=500s"}
    assert clip_source(clip) == ("https://www.twitch.tv/a/clip/X", None)
    spike = {"kind": "chat_spike", "url": "https://www.twitch.tv/a", "vod_url": "https://www.twitch.tv/videos/9?t=3600s"}
    url, sec = clip_source(spike)
    assert sec == ("https://www.twitch.tv/videos/9", 3570, 3640)
    with pytest.raises(ClipperError):
        clip_source({"kind": "chat_spike", "url": "https://kick.com/a", "vod_url": None})
    with pytest.raises(ClipperError):
        clip_source({"kind": "video", "url": "https://youtu.be/x", "duration": 36000})
    assert clip_source({"url": "https://www.twitch.tv/a", "rank": 1}) == ("https://www.twitch.tv/a", None)


def test_chopify_env_puts_venv_bin_first(tmp_path):
    from engine.actions.clipper import chopify_env
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    env = chopify_env(str(tmp_path))
    assert env["PATH"].split(":")[0] == str(tmp_path / ".venv" / "bin")
    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")


def test_progress_walks_through_a_real_chopify_log():
    from engine.actions.clipper import JOB_MARK, parse_progress
    log = (FX / "chopify_batch.log").read_text(errors="ignore")
    cut = lambda marker: log[: log.index(marker) + len(marker)]
    stages = [
        (cut("[download] Destination"), "Video yüklənir"),
        (cut("Detected language"), "Subtitr yazılır"),
        (cut("] discover"), "Anlar analiz olunur"),
        (cut("] render 1/2"), "Render 1/2"),
        (log, "Hazırdır"),
    ]
    last = 0
    for text, stage in stages:
        p = parse_progress(text)
        assert p["stage"] == stage, (stage, p)
        assert p["percent"] >= last
        last = p["percent"]
    assert last == 100
    # only the newest job in an appended log counts
    assert parse_progress(log + f"\n{JOB_MARK}2 ===\n")["percent"] == 1


def test_render_progress_uses_ffmpeg_time():
    from engine.actions.clipper import parse_progress
    text = "[x] render 2/2\n> ffmpeg -y -ss 1 -i a.mp4 -t 10.0 -filter_complex z\nframe= 1 time=00:00:05.00 bitrate"
    assert parse_progress(text) == {"percent": round(65 + 34 * 1.5 / 2), "stage": "Render 2/2"}


# --- moment quality ----------------------------------------------------------
from engine.scoring import moment_quality as MQ  # noqa: E402


def _samples(rows):
    return [(1000.0 + i, u, t) for i, (u, t) in enumerate(rows)]


def test_spam_spike_rejected():
    s = _samples([(f"bot{i % 2}", "FOLLOW ME !join") for i in range(40)])
    v = MQ.analyze(s, {"z": 8, "msgs": 40, "hype": 0}, "Valorant ranked", "Valorant")
    assert not v["ok"] and v["quality"] <= 30


def test_real_reaction_accepted():
    words = ["wtf", "how??", "no way", "bug?", "omg", "KEKW", "what was that", "clip it", "bro", "LMAO"]
    s = _samples([(f"user{i}", words[i % 10]) for i in range(30)])
    v = MQ.analyze(s, {"z": 6, "msgs": 30, "hype": 15}, "GTA 6 chaos", "Grand Theft Auto VI")
    assert v["ok"] and v["quality"] > 50


def test_unsafe_and_category_rejected():
    s = _samples([(f"u{i}", f"nsfw lol {i}") for i in range(12)])
    assert "18+" in MQ.analyze(s, {"z": 5, "msgs": 12, "hype": 3}, "t", "g")["reasons"]
    s2 = _samples([(f"u{i}", f"wow {i}") for i in range(12)])
    assert not MQ.analyze(s2, {"z": 5, "msgs": 12, "hype": 3}, "fun", "Pools, Hot Tubs & Beaches")["ok"]


def test_no_text_is_neutral():
    assert MQ.analyze(None, {"z": 4, "msgs": 20, "hype": 5}, "Fortnite", "Fortnite")["ok"]


def test_meter_keeps_samples():
    got = []
    m = ChatMeter(on_spike=lambda k, i: got.append(i))
    t = 2_000_000.0
    for b in range(30):
        for i in range(5):
            m.add("k", "hi", now=t + b * 10 + i, user=f"u{i}")
    for i in range(60):
        m.add("k", "wtf", now=t + 300 + i * 0.1, user=f"v{i}")
    m.add("k", "x", now=t + 311, user="z")
    assert got and got[0]["samples"] and got[0]["samples"][-1][1].startswith("v")
