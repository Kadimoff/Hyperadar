"""SQLite persistence: tags, per-stream viewer snapshots, settings, action jobs.

Live rankings are computed in memory; the database is what survives a restart
(so viewer growth does not start from zero) and what the history chart reads.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS tags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  mapping TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
  key TEXT NOT NULL, tag_id INTEGER NOT NULL, ts REAL NOT NULL, viewers INTEGER NOT NULL, hype REAL
);
CREATE INDEX IF NOT EXISTS snap_key_ts ON snapshots(key, ts);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, target TEXT NOT NULL,
  status TEXT NOT NULL, detail TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS permissions (key TEXT PRIMARY KEY, status TEXT NOT NULL);
"""


class DB:
    def __init__(self, path: Path):
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript("PRAGMA journal_mode=WAL;" + SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # tags -----------------------------------------------------------------
    async def tags(self) -> list[dict]:
        rows = await self.conn.execute_fetchall("SELECT * FROM tags ORDER BY created_at")
        return [{"id": r["id"], "name": r["name"], "mapping": json.loads(r["mapping"])} for r in rows]

    async def add_tag(self, name: str, mapping: dict) -> dict:
        cur = await self.conn.execute("INSERT INTO tags(name, mapping, created_at) VALUES (?,?,?)",
                                      (name, json.dumps(mapping), time.time()))
        await self.conn.commit()
        return {"id": cur.lastrowid, "name": name, "mapping": mapping}

    async def set_mapping(self, tag_id: int, mapping: dict) -> None:
        await self.conn.execute("UPDATE tags SET mapping=? WHERE id=?", (json.dumps(mapping), tag_id))
        await self.conn.commit()

    async def delete_tag(self, tag_id: int) -> None:
        await self.conn.execute("DELETE FROM tags WHERE id=?", (tag_id,))
        await self.conn.execute("DELETE FROM snapshots WHERE tag_id=?", (tag_id,))
        await self.conn.commit()

    # snapshots ------------------------------------------------------------
    async def add_snapshots(self, tag_id: int, rows: list[tuple[str, int, float]], ts: float) -> None:
        await self.conn.executemany("INSERT INTO snapshots(key, tag_id, ts, viewers, hype) VALUES (?,?,?,?,?)",
                                    [(k, tag_id, ts, v, h) for k, v, h in rows])
        await self.conn.execute("DELETE FROM snapshots WHERE ts < ?", (ts - 6 * 3600,))
        await self.conn.commit()

    async def history(self, tag_id: int, since: float) -> dict[str, list[tuple[float, int]]]:
        rows = await self.conn.execute_fetchall(
            "SELECT key, ts, viewers FROM snapshots WHERE tag_id=? AND ts>=? ORDER BY ts", (tag_id, since))
        out: dict[str, list] = {}
        for r in rows:
            out.setdefault(r["key"], []).append((r["ts"], r["viewers"]))
        return out

    async def series(self, key: str, since: float) -> list[dict]:
        rows = await self.conn.execute_fetchall(
            "SELECT ts, viewers, hype FROM snapshots WHERE key=? AND ts>=? ORDER BY ts", (key, since))
        return [{"ts": r["ts"], "viewers": r["viewers"], "hype": r["hype"]} for r in rows]

    # settings -------------------------------------------------------------
    async def get_setting(self, k: str, default: Any = None) -> Any:
        rows = await self.conn.execute_fetchall("SELECT v FROM settings WHERE k=?", (k,))
        return json.loads(rows[0]["v"]) if rows else default

    async def set_setting(self, k: str, v: Any) -> None:
        await self.conn.execute("INSERT INTO settings(k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                                (k, json.dumps(v)))
        await self.conn.commit()

    # permissions ----------------------------------------------------------
    async def permissions(self) -> dict[str, str]:
        rows = await self.conn.execute_fetchall("SELECT key, status FROM permissions")
        return {r["key"]: r["status"] for r in rows}

    async def set_permission(self, key: str, status: str) -> None:
        await self.conn.execute("INSERT INTO permissions(key, status) VALUES (?,?) "
                                "ON CONFLICT(key) DO UPDATE SET status=excluded.status", (key, status))
        await self.conn.commit()

    # jobs -----------------------------------------------------------------
    async def add_job(self, kind: str, target: str, status: str, detail: str = "") -> int:
        now = time.time()
        cur = await self.conn.execute(
            "INSERT INTO jobs(kind, target, status, detail, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (kind, target, status, detail, now, now))
        await self.conn.commit()
        return cur.lastrowid

    async def update_job(self, job_id: int, status: str, detail: str = "") -> None:
        await self.conn.execute("UPDATE jobs SET status=?, detail=?, updated_at=? WHERE id=?",
                                (status, detail, time.time(), job_id))
        await self.conn.commit()

    async def jobs(self, limit: int = 30) -> list[dict]:
        rows = await self.conn.execute_fetchall("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]
