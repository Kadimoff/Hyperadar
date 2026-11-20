"""Technical stream quality from an HLS master playlist."""
from __future__ import annotations

import re
from typing import Optional

_INF = re.compile(r"#EXT-X-STREAM-INF:(.*)")
_RES = re.compile(r"RESOLUTION=(\d+)x(\d+)")
_FPS = re.compile(r"FRAME-RATE=([\d.]+)")
_BW = re.compile(r"(?<![-A-Z])BANDWIDTH=(\d+)")


def best_variant(master: str) -> Optional[dict]:
    best = None
    for m in _INF.finditer(master or ""):
        attrs = m.group(1)
        res = _RES.search(attrs)
        if not res:
            continue
        w, h = int(res.group(1)), int(res.group(2))
        fps = float(_FPS.search(attrs).group(1)) if _FPS.search(attrs) else 30.0
        bw = int(_BW.search(attrs).group(1)) if _BW.search(attrs) else 0
        cand = {"width": w, "height": h, "fps": round(fps), "bandwidth": bw}
        if best is None or (h, fps, bw) > (best["height"], best["fps"], best["bandwidth"]):
            best = cand
    return best


def quality_score(v: Optional[dict]) -> Optional[dict]:
    if not v:
        return None
    h = v["height"]
    base = 1.0 if h >= 1080 else 0.85 if h >= 900 else 0.7 if h >= 720 else 0.45 if h >= 480 else 0.2
    if v["fps"] >= 50:
        base = min(1.0, base + 0.1)
    else:
        base -= 0.05
    return {"score": round(base * 100), "label": f"{h}p{v['fps']}", "bitrate_kbps": v["bandwidth"] // 1000}
