"""HTTP API + SSE + static dashboard.  Run: uvicorn engine.main:app --port 8090"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .actions import clipper
from .actions.obs import OBSError, record, show_in_obs
from .core import Engine

ROOT = Path(__file__).resolve().parent.parent
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)   # request lines carry playback tokens
engine = Engine(ROOT / "data" / "hype-radar.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await engine.start()
    yield
    for p in engine._procs.values():
        if p.returncode is None:
            p.terminate()
    await engine.stop()


app = FastAPI(title="Hype Radar", lifespan=lifespan)


# --- tags -----------------------------------------------------------------
class TagIn(BaseModel):
    name: str


class MappingIn(BaseModel):
    platform: str
    category: Optional[dict] = None


@app.get("/api/tags")
async def list_tags():
    return [{**t, "updated": engine.updated.get(t["id"]), "streams": len(engine.ranked.get(t["id"], []))}
            for t in engine.tags.values()]


@app.post("/api/tags")
async def add_tag(body: TagIn):
    name = body.name.strip()
    if not name or len(name) > 60:
        raise HTTPException(400, "Tag adı 1–60 simvol olmalıdır")
    if any(t["name"].lower() == name.lower() for t in engine.tags.values()):
        raise HTTPException(409, "Bu tag artıq var")
    return await engine.add_tag(name)


@app.get("/api/resolve")
async def resolve(q: str):
    """Preview what a tag maps to on each platform, without saving it."""
    return await engine.resolve(q)


@app.patch("/api/tags/{tag_id}")
async def patch_mapping(tag_id: int, body: MappingIn):
    if tag_id not in engine.tags:
        raise HTTPException(404)
    if body.platform not in engine.connectors:
        raise HTTPException(400, "naməlum platforma")
    return await engine.update_mapping(tag_id, body.platform, body.category)


@app.delete("/api/tags/{tag_id}")
async def delete_tag(tag_id: int):
    await engine.delete_tag(tag_id)
    return {"ok": True}


@app.post("/api/tags/{tag_id}/refresh")
async def refresh(tag_id: int):
    if tag_id not in engine.tags:
        raise HTTPException(404)
    await engine._refresh_tag(tag_id)
    return {"ok": True}


# --- data -----------------------------------------------------------------
def _filter(rows: list[dict], platforms: Optional[str], lang: Optional[str], q: Optional[str],
            min_quality: int = 0) -> list[dict]:
    if platforms:
        allowed = set(platforms.split(","))
        rows = [r for r in rows if r["platform"] in allowed]
    if lang:
        rows = [r for r in rows if (r.get("language") or "").startswith(lang.lower())]
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in (r.get("title") or "").lower() or ql in (r.get("display_name") or "").lower()]
    if min_quality:
        rows = [r for r in rows if (r.get("quality") or {}).get("score", 0) >= min_quality]
    return rows


@app.get("/api/live")
async def live(tag: int, platforms: Optional[str] = None, lang: Optional[str] = None, q: Optional[str] = None,
               min_quality: int = 0, limit: int = Query(100, le=300)):
    rows = _filter(engine.ranked.get(tag, []), platforms, lang, q, min_quality)
    return {"updated": engine.updated.get(tag), "total": len(engine.ranked.get(tag, [])), "items": rows[:limit]}


@app.get("/api/moments")
async def moments(tag: int, platforms: Optional[str] = None, lang: Optional[str] = None, q: Optional[str] = None,
                  kind: Optional[str] = None, limit: int = Query(80, le=300)):
    rows = _filter(engine.moments_for(tag), platforms, lang, q)
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    return {"items": rows[:limit]}


@app.get("/api/history")
async def history(key: str, minutes: int = Query(60, le=360)):
    return await engine.db.series(key, time.time() - minutes * 60)


@app.get("/api/status")
async def status():
    return engine.status()


@app.get("/api/stream")
async def stream(request: Request):
    q: asyncio.Queue = asyncio.Queue()
    engine.subscribers.add(q)

    async def gen():
        try:
            yield f"event: hello\ndata: {json.dumps(engine.status())}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event, data = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            engine.subscribers.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --- settings & permissions -----------------------------------------------
@app.get("/api/settings")
async def get_settings():
    s = json.loads(json.dumps(engine.settings))
    if s.get("obs", {}).get("password"):
        s["obs"]["password"] = "••••••"
    return s


@app.put("/api/settings")
async def put_settings(body: dict):
    for k, v in body.items():
        if k not in engine.settings:
            continue
        if k == "obs" and v.get("password") == "••••••":
            v["password"] = engine.settings["obs"].get("password", "")
        if k == "weights":
            v = {wk: max(0.0, min(1.0, float(v.get(wk, 0)))) for wk in engine.settings["weights"]}
        engine.settings[k] = v
        await engine.db.set_setting(k, v)
    return await get_settings()


class PermIn(BaseModel):
    key: str
    status: str


@app.put("/api/permissions")
async def set_permission(body: PermIn):
    if body.status not in ("unknown", "asked", "allowed", "denied"):
        raise HTTPException(400)
    engine.permissions[body.key] = body.status
    await engine.db.set_permission(body.key, body.status)
    return {"ok": True}


# --- actions --------------------------------------------------------------
class ActionIn(BaseModel):
    key: str


@app.post("/api/actions/obs")
async def action_obs(body: ActionIn):
    item = engine.find_item(body.key)
    if not item:
        raise HTTPException(404, "Element tapılmadı (ranking yenilənib?)")
    o = engine.settings["obs"]
    url = f"http://localhost:{o.get('page_port', 8090)}/obs?key={quote(body.key)}"
    job = await engine.db.add_job("obs", body.key, "running")
    try:
        detail = await show_in_obs(o["host"], int(o["port"]), o.get("password", ""), url)
        await engine.db.update_job(job, "done", detail)
        engine.publish("job", {"id": job, "status": "done", "detail": detail})
        return {"ok": True, "detail": detail}
    except OBSError as e:
        await engine.db.update_job(job, "failed", str(e))
        raise HTTPException(502, str(e))


class RecordIn(BaseModel):
    action: str = "status"


@app.post("/api/actions/record")
async def action_record(body: RecordIn):
    if body.action not in ("start", "stop", "status"):
        raise HTTPException(400)
    o = engine.settings["obs"]
    try:
        r = await record(o["host"], int(o["port"]), o.get("password", ""), body.action)
    except OBSError as e:
        raise HTTPException(502, str(e))
    engine.publish("record", r)
    return r


@app.post("/api/actions/clip")
async def action_clip(body: ActionIn):
    item = engine.find_item(body.key)
    if not item:
        raise HTTPException(404, "Element tapılmadı")
    perm = engine.permissions.get(f"{item['platform']}:{item['channel']}", "unknown")
    if perm == "denied":
        raise HTTPException(403, "Bu yayımçı klipə icazə verməyib")
    missing = clipper.describe_missing(engine.settings.get("chopify_dir"))
    if missing:
        raise HTTPException(409, missing)
    live_item = item.get("kind") is None
    try:
        url, section = clipper.clip_source(item)
    except clipper.ClipperError as e:
        raise HTTPException(409, str(e))
    out_dir = str(ROOT / "data" / "clips" / body.key.replace(":", "_"))
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    channel_url = {"twitch": f"twitch.tv/{item['channel']}", "kick": f"kick.com/{item['channel']}"}.get(
        item["platform"], item["url"].replace("https://www.", ""))
    (Path(out_dir) / "source.json").write_text(json.dumps({
        "key": body.key, "platform": item["platform"], "channel": item["channel"],
        "display_name": item["display_name"], "title": item.get("title"), "url": item["url"],
        "channel_url": channel_url, "kind": item.get("kind") or "live", "thumbnail": item.get("thumbnail")},
        ensure_ascii=False))
    chopify_dir = engine.settings["chopify_dir"]
    try:
        clipper.chopify_command(chopify_dir, url, live_item, out_dir)   # fail fast if chopify is missing
    except clipper.ClipperError as e:
        raise HTTPException(409, str(e))
    job = await engine.db.add_job("clip", body.key, "running", url)
    log_path = Path(out_dir) / "chopify.log"
    with open(log_path, "a") as f:     # progress parsing starts from this job's marker
        f.write(f"\n{clipper.JOB_MARK}{job} ===\n" + (clipper.SECTION_MARK + "\n" if section else ""))
    engine._running[job] = {"key": body.key, "out_dir": out_dir, "live": live_item, "started": time.time()}

    async def run():
        nonlocal url
        try:
            if section:
                url = await clipper.download_section(chopify_dir, *section, out_dir)
            if job in engine._stopped:
                raise clipper.ClipperError("Dayandırıldı")
            proc = await clipper.start_chopify(chopify_dir, url, live_item, out_dir, clipper.min_score_for(item))
        except clipper.ClipperError as e:
            status = "stopped" if job in engine._stopped else "failed"
            await engine.db.update_job(job, status, str(e))
            engine._running.pop(job, None)
            engine.publish("job", {"id": job, "status": status, "detail": str(e), "key": body.key})
            return
        engine._procs[job] = proc
        rc = await proc.wait()
        engine._running.pop(job, None)
        status = "stopped" if job in engine._stopped else "done" if rc == 0 else "failed"
        clips = [c for c in Path(out_dir).rglob("*.mp4")
                 if not {"work", "buffer", "windows"} & set(c.parts) and not c.name.startswith("section-")]
        detail = (f"Dayandırıldı. {len(clips)} klip hazırdır" if status == "stopped"
                  else f"{len(clips)} klip hazırdır: {out_dir}" if rc == 0
                  else clipper.failure_reason(Path(out_dir) / "chopify.log") or f"exit {rc}")
        await engine.db.update_job(job, status, detail)
        engine.publish("job", {"id": job, "status": status, "detail": detail, "clips": [c.name for c in clips],
                               "key": body.key})
    asyncio.create_task(run())
    engine.publish("job", {"id": job, "status": "running", "key": body.key})
    return {"ok": True, "job": job, "out_dir": out_dir}


CLIPS_DIR = ROOT / "data" / "clips"


@app.get("/api/clips")
async def finished_clips():
    """Rendered clips on disk, newest first, with the creator credit for the description."""
    out = []
    if not CLIPS_DIR.exists():
        return out
    for mp4 in CLIPS_DIR.rglob("*.mp4"):
        rel = mp4.relative_to(CLIPS_DIR)
        # work/ = chopify scratch, buffer/ + windows/ = raw 16:9 pieces of a live session, section-* = VOD cut
        if {"work", "buffer", "windows"} & set(rel.parts) or mp4.name.startswith("section-"):
            continue
        job_dir = CLIPS_DIR / rel.parts[0]
        src, meta = {}, {}
        try:
            src = json.loads((job_dir / "source.json").read_text())
        except (OSError, ValueError):
            pass
        try:
            meta = json.loads(mp4.with_suffix(".meta.json").read_text())
        except (OSError, ValueError):
            pass
        poster = mp4.with_suffix(".png")
        title = src.get("title") or meta.get("title") or mp4.stem
        credit = f"🎮 {src['display_name']} · {src['channel_url']}" if src.get("display_name") else ""
        tags = " ".join(meta.get("hashtags") or ["#shorts"])
        out.append({
            "id": str(rel), "file": "/media/" + quote(str(rel)),
            "poster": "/media/" + quote(str(rel.with_suffix(".png"))) if poster.exists() else None,
            "title": title, "chopify_title": meta.get("title"), "score": meta.get("score"),
            "duration": (meta.get("end") or 0) - (meta.get("start") or 0) or None,
            "created": mp4.stat().st_mtime, "size": mp4.stat().st_size, "source": src,
            "description": "\n".join(x for x in (title, credit, tags) if x),
        })
    out.sort(key=lambda c: c["created"], reverse=True)
    return out


@app.get("/api/jobs")
async def jobs():
    return await engine.db.jobs()


@app.post("/api/jobs/{job_id}/stop")
async def stop_job(job_id: int):
    if job_id in engine._running:
        engine._stopped.add(job_id)
    p = engine._procs.get(job_id)
    if p and p.returncode is None:
        p.terminate()
    return {"ok": True}


@app.get("/api/jobs/active")
async def active_jobs():
    """Clip jobs whose process is still running, keyed by the item they were started for."""
    return {meta["key"]: job for job, meta in engine._running.items()}


@app.get("/api/jobs/progress")
async def jobs_progress():
    """Running clip jobs with a percentage parsed from chopify's log; live sessions report clips saved."""
    out = []
    for job, meta in list(engine._running.items()):
        d = Path(meta["out_dir"])
        try:
            src = json.loads((d / "source.json").read_text())
        except (OSError, ValueError):
            src = {}
        if meta["live"]:
            saved = [c for c in d.glob("live/*/clips/*.mp4")]
            p = {"percent": None, "stage": f"Canlı izlənir, {len(saved)} klip saxlanıb"}
        else:
            p = clipper.read_progress(d / "chopify.log")
        out.append({"id": job, "key": meta["key"], "live": meta["live"], "elapsed": round(time.time() - meta["started"]),
                    "display_name": src.get("display_name"), "title": src.get("title"),
                    "platform": src.get("platform"), "thumbnail": src.get("thumbnail"), **p})
    return out


# --- OBS player page ------------------------------------------------------
@app.get("/obs", response_class=HTMLResponse)
async def obs_page(key: str):
    item = engine.find_item(key)
    if not item:
        return HTMLResponse("<body style='background:#000;color:#888;font:24px sans-serif'>Yayım tapılmadı</body>")
    src = item.get("embed")
    hls = (item.get("extra") or {}).get("hls")
    if src:
        body = f'<iframe src="{escape(src)}" allow="autoplay; fullscreen" allowfullscreen></iframe>'
    elif hls:
        body = (f'<video id="v" autoplay playsinline></video>'
                '<script src="https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.15/hls.min.js"></script>'
                f'<script>const h=new Hls();h.loadSource({json.dumps(hls)});h.attachMedia(document.getElementById("v"));</script>')
    else:
        body = f'<p>Embed yoxdur: {escape(item["url"])}</p>'
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>HypeRadar · OBS</title>
<style>html,body{{margin:0;height:100%;background:#000}}iframe,video{{border:0;width:100%;height:100%}}
p{{color:#aaa;font:20px sans-serif;padding:2rem}}</style></head><body>{body}</body></html>"""


# --- dashboard ------------------------------------------------------------
@app.get("/")
async def index():
    return FileResponse(ROOT / "web" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")
CLIPS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=CLIPS_DIR), name="media")
