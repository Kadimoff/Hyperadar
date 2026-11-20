"""Minimal obs-websocket v5 client (built into OBS 28+, Tools → WebSocket Server Settings).

Creates or updates one Browser Source named "HypeRadar" in the current program
scene. The source loads this engine's /obs page, which embeds the chosen stream
with the right player `parent`, so Twitch's embed check passes.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import shutil
import socket
import uuid

import websockets

SOURCE = "HypeRadar"


def auth_string(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class OBSError(Exception):
    pass


async def _request(ws, rtype: str, data: dict | None = None) -> dict:
    rid = str(uuid.uuid4())
    await ws.send(json.dumps({"op": 6, "d": {"requestType": rtype, "requestId": rid, "requestData": data or {}}}))
    while True:
        m = json.loads(await ws.recv())
        if m.get("op") == 7 and m["d"].get("requestId") == rid:
            return m["d"]


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def obs_command() -> list[str] | None:
    """How to start OBS on this machine: a native binary, else the Flatpak."""
    if shutil.which("obs"):
        return ["obs", "--disable-shutdown-check"]
    if shutil.which("flatpak"):
        return ["flatpak", "run", "com.obsproject.Studio", "--disable-shutdown-check"]
    return None


async def ensure_running(host: str, port: int, wait: float = 75) -> bool:
    """Start OBS when it is local and closed, then wait for its WebSocket. Returns True if it was launched."""
    if _port_open(host, port):
        return False
    if host not in ("localhost", "127.0.0.1", "::1"):
        raise OBSError(f"OBS-ə qoşulmaq olmadı ({host}:{port}). Uzaq kompüterdə OBS açıq olmalıdır.")
    cmd = obs_command()
    if not cmd:
        raise OBSError("OBS bu kompüterdə tapılmadı. Quraşdırın: flatpak install flathub com.obsproject.Studio")
    # own session: OBS keeps running when the engine restarts
    await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                                         start_new_session=True)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    while loop.time() < deadline:
        if _port_open(host, port):
            await asyncio.sleep(1.5)   # let obs-websocket finish starting before we identify
            return True
        await asyncio.sleep(0.5)
    raise OBSError("OBS açıldı, amma WebSocket cavab vermir. OBS-də Tools → WebSocket Server Settings bölməsində "
                   f"serveri aktiv edin (port {port}).")


async def show_in_obs(host: str, port: int, password: str, url: str, autostart: bool = True) -> str:
    launched = await ensure_running(host, port) if autostart else False
    try:
        ws = await websockets.connect(f"ws://{host}:{port}", open_timeout=5)
    except OSError as e:
        raise OBSError(f"OBS-ə qoşulmaq olmadı ({host}:{port}). OBS açıqdır və WebSocket Server aktivdir?") from e
    result = await _apply(ws, password, url)
    return f"OBS açıldı, {result}" if launched else result


async def _identify(ws, password: str) -> None:
    hello = json.loads(await ws.recv())["d"]
    ident = {"rpcVersion": 1}
    if hello.get("authentication"):
        if not password:
            raise OBSError("OBS WebSocket parol istəyir — Ayarlarda daxil edin.")
        a = hello["authentication"]
        ident["authentication"] = auth_string(password, a["salt"], a["challenge"])
    await ws.send(json.dumps({"op": 1, "d": ident}))
    if json.loads(await ws.recv()).get("op") != 2:
        raise OBSError("OBS identifikasiyanı rədd etdi (parol səhvdir?).")


async def record(host: str, port: int, password: str, action: str) -> dict:
    """action: start | stop | status. Recording needs no stream key; OBS writes the file itself."""
    await ensure_running(host, port)
    async with websockets.connect(f"ws://{host}:{port}", open_timeout=5) as ws:
        await _identify(ws, password)
        st = (await _request(ws, "GetRecordStatus"))["responseData"]
        folder = (await _request(ws, "GetRecordDirectory"))["responseData"]["recordDirectory"]
        if action == "start" and not st["outputActive"]:
            r = await _request(ws, "StartRecord")
            if not r["requestStatus"]["result"]:
                raise OBSError(r["requestStatus"].get("comment") or "Yazı başlamadı")
            return {"recording": True, "folder": folder}
        if action == "stop" and st["outputActive"]:
            r = await _request(ws, "StopRecord")
            return {"recording": False, "folder": folder, "file": (r.get("responseData") or {}).get("outputPath")}
        return {"recording": st["outputActive"], "folder": folder, "duration_ms": st.get("outputDuration")}


async def _apply(ws, password: str, url: str) -> str:
    async with ws:
        hello = json.loads(await ws.recv())["d"]
        ident = {"rpcVersion": 1}
        if hello.get("authentication"):
            if not password:
                raise OBSError("OBS WebSocket parol istəyir — Ayarlarda daxil edin.")
            a = hello["authentication"]
            ident["authentication"] = auth_string(password, a["salt"], a["challenge"])
        await ws.send(json.dumps({"op": 1, "d": ident}))
        m = json.loads(await ws.recv())
        if m.get("op") != 2:
            raise OBSError("OBS identifikasiyanı rədd etdi (parol səhvdir?).")
        settings = {"url": url, "width": 1920, "height": 1080, "reroute_audio": False}
        existing = await _request(ws, "GetInputSettings", {"inputName": SOURCE})
        if existing["requestStatus"]["result"]:
            r = await _request(ws, "SetInputSettings", {"inputName": SOURCE, "inputSettings": settings})
            if not r["requestStatus"]["result"]:
                raise OBSError(r["requestStatus"].get("comment") or "SetInputSettings failed")
            return "updated"
        scene = (await _request(ws, "GetCurrentProgramScene"))["responseData"]["currentProgramSceneName"]
        r = await _request(ws, "CreateInput", {"sceneName": scene, "inputName": SOURCE,
                                               "inputKind": "browser_source", "inputSettings": settings,
                                               "sceneItemEnabled": True})
        if not r["requestStatus"]["result"]:
            raise OBSError(r["requestStatus"].get("comment") or "CreateInput failed")
        return f"created in scene '{scene}'"
