"""Hand a stream or moment to a clip engine.

- chopify (local CLI): live channels -> ``chopify --live``; VODs/clips -> batch run.
- OpenShorts (Docker REST): not wired yet — needs its API contract checked
  against a running instance first.

The engine only launches the job and tracks the process; the clip files stay
in the clip engine's own output folder.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import Optional


class ClipperError(Exception):
    pass


def chopify_command(chopify_dir: str, url: str, live: bool, out_dir: str, min_score: float = 7.0) -> list[str]:
    root = Path(chopify_dir).expanduser()
    py = root / ".venv" / "bin" / "python"
    script = root / "chopify.py"
    if not script.exists():
        raise ClipperError(f"chopify tapılmadı: {script}. Ayarlarda chopify qovluğunu göstərin.")
    python = str(py) if py.exists() else (shutil.which("python3") or "python3")
    # --fit must be explicit: chopify 1.4.0 leaves it None when omitted and then rejects it.
    common = ["--aspect", "9:16", "--layout", "stacked", "--fit", "cover", "--no-input"]
    if live:
        # live sessions write to <projects-dir>/live/<id>/clips; a small model keeps rolling Whisper real-time on CPU
        return [python, str(script), "--live", url, *common, "--projects-dir", out_dir,
                "--whisper-model", "base", "--live-threshold", "8", "--live-pre-roll", "30"]
    return [python, str(script), url, *common, "--out", out_dir, "--workdir", str(Path(out_dir) / "work"),
            "--whisper-model", "small", "--min-score", str(min_score), "--min-len", "10"]


def min_score_for(item: dict) -> float:
    """Viewers already picked a platform clip as worth clipping, so chopify's own score must not veto it."""
    return {"clip": 0.0, "chat_spike": 5.0}.get(item.get("kind"), 7.0)


def failure_reason(log_path: Path) -> str:
    try:
        lines = log_path.read_text(errors="ignore").strip().splitlines()
    except OSError:
        return ""
    for line in reversed(lines[-40:]):
        if line.startswith("Chopify:") or "Error" in line:
            return line.removeprefix("Chopify:").strip()[:300]
    return lines[-1][:300] if lines else ""


MAX_SOURCE_SECONDS = 30 * 60


def clip_source(item: dict) -> tuple[str, Optional[tuple[str, int, int]]]:
    """What chopify should get for a ranking item: (url, section) where section = (vod_url, start, end).

    Never hand chopify a whole multi-hour VOD: it ignores ?t= and would download all of it.
    """
    kind = item.get("kind")
    if kind is None:                        # a live stream row
        return item["url"], None
    if kind == "clip":
        return item["url"], None            # the clip page itself, not the VOD it came from
    if kind == "chat_spike":
        vod = item.get("vod_url") or ""
        m = re.search(r"\?t=(\d+)s", vod)
        if not m:
            raise ClipperError("Bu chat partlayışının VOD anı məlum deyil (Kick/YouTube). Canlı yayımın özündən klip hazırlayın.")
        t = int(m.group(1))
        return vod.split("?")[0], (vod.split("?")[0], max(0, t - 30), t + 40)
    if kind == "video":
        if (item.get("duration") or 0) > MAX_SOURCE_SECONDS:
            raise ClipperError("Video 30 dəqiqədən uzundur. Bütün videonu CPU-da emal etmək saatlar çəkər; "
                               "qısa klip və ya chat partlayışı seçin.")
        return item["url"], None
    raise ClipperError(f"Naməlum element növü: {kind}")


async def download_section(chopify_dir: str, vod_url: str, start: int, end: int, out_dir: str) -> str:
    """Fetch only [start, end] seconds of a VOD with yt-dlp (from chopify's venv)."""
    root = Path(chopify_dir).expanduser()
    py = root / ".venv" / "bin" / "python"
    target = Path(out_dir) / f"section-{start}-{end}.mp4"
    if target.exists() and target.stat().st_size > 0:
        return str(target)
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        str(py) if py.exists() else "yt-dlp", *(["-m", "yt_dlp"] if py.exists() else []),
        "--download-sections", f"*{start}-{end}", "--force-keyframes-at-cuts", "-f", "best[height<=1080]/best",
        "--merge-output-format", "mp4", "-o", str(target), "--no-part", "--quiet", vod_url,
        env=chopify_env(chopify_dir), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0 or not target.exists():
        raise ClipperError(f"VOD hissəsi yüklənmədi: {(err or b'').decode(errors='ignore')[-300:]}")
    return str(target)


def chopify_env(chopify_dir: str) -> dict:
    """The environment an activated chopify venv would have.

    chopify's live mode runs the `yt-dlp` executable by name, which lives in
    .venv/bin; launching the venv's python directly does not put that on PATH.
    """
    env = dict(os.environ)
    venv = Path(chopify_dir).expanduser() / ".venv"
    if venv.exists():
        env["VIRTUAL_ENV"] = str(venv)
        env["PATH"] = f"{venv / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONUNBUFFERED"] = "1"   # live progress reaches chopify.log as it happens
    return env


async def start_chopify(chopify_dir: str, url: str, live: bool, out_dir: str,
                        min_score: float = 7.0) -> asyncio.subprocess.Process:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    cmd = chopify_command(chopify_dir, url, live, out_dir, min_score)
    log = open(Path(out_dir) / "chopify.log", "ab")
    return await asyncio.create_subprocess_exec(*cmd, cwd=str(Path(chopify_dir).expanduser()),
                                                env=chopify_env(chopify_dir),
                                                stdout=log, stderr=asyncio.subprocess.STDOUT)


def describe_missing(chopify_dir: Optional[str]) -> Optional[str]:
    if not chopify_dir:
        return "Klip motoru qoşulmayıb: Ayarlarda chopify qovluğunu göstərin (mərhələ 4)."
    return None


# --- progress -------------------------------------------------------------------
JOB_MARK = "=== hype-radar job "
SECTION_MARK = "HYPERADAR: section download"
_NUM = r"(\d+(?:\.\d+)?)"


def _secs(h: str, m: str, s: str) -> float:
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_progress(text: str) -> dict:
    """Turn a chopify batch log into {percent, stage}. Only the latest job's part of the log counts.

    Weights follow where the time goes on CPU: download 3–25, transcription 25–60,
    analysis 60–65, rendering 65–99 (split evenly across the clips being rendered).
    """
    i = text.rfind(JOB_MARK)
    if i >= 0:
        text = text[i:]
    if "CHOPIFY COMPLETE" in text:
        return {"percent": 100, "stage": "Hazırdır"}
    renders = list(re.finditer(r"\] render (\d+)/(\d+)", text))
    if renders:
        k, n = int(renders[-1].group(1)), int(renders[-1].group(2))
        tail = text[renders[-1].end():]
        frac = 0.0
        dur = re.search(r" -t " + _NUM, tail)
        times = re.findall(r"time=(\d+):(\d+):" + _NUM, tail)
        if dur and times and float(dur.group(1)) > 0:
            frac = min(1.0, _secs(*times[-1]) / float(dur.group(1)))
        if "SAVED " in tail:
            frac = 1.0
        return {"percent": round(65 + 34 * ((k - 1) + frac) / max(n, 1)), "stage": f"Render {k}/{n}"}
    if re.search(r"\] (render|select)\s", text):
        return {"percent": 64, "stage": "Ən yaxşı anlar seçilir"}
    if re.search(r"\] (detect|discover)\s", text):
        return {"percent": 61, "stage": "Anlar analiz olunur"}
    if re.search(r"\] transcribe\s", text):
        frac = 0.0
        audio = re.findall(r"audio " + _NUM + "s", text)
        ends = re.findall(r"\[\s*" + _NUM + r" ->\s*" + _NUM + r"\]", text)
        if audio and ends and float(audio[-1]) > 0:
            frac = min(1.0, float(ends[-1][1]) / float(audio[-1]))
        if "transcript.json" in text:
            frac = 1.0
        return {"percent": round(25 + 35 * frac), "stage": "Subtitr yazılır"}
    if re.search(r"\] source\s", text):
        pct = 0.0
        frags = re.findall(r"\(frag (\d+)/(\d+)\)", text)
        dl = re.findall(r"\[download\]\s+" + _NUM + "%", text)
        if dl and float(dl[-1]) > 0:
            pct = float(dl[-1])
        elif frags and int(frags[-1][1]) > 0:
            pct = 100 * int(frags[-1][0]) / int(frags[-1][1])
        return {"percent": round(3 + 22 * min(pct, 100) / 100), "stage": "Video yüklənir"}
    if SECTION_MARK in text:
        return {"percent": 2, "stage": "VOD-dan an kəsilir"}
    return {"percent": 1, "stage": "Başlayır"}


def read_progress(log_path: Path, max_bytes: int = 400_000) -> dict:
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - max_bytes))
            text = f.read().decode(errors="ignore")
    except OSError:
        return {"percent": 0, "stage": "Gözləyir"}
    return parse_progress(text)
