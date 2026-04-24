"""
SQLite-backed preset repository, indexed by Maestro user_id.

Each preset is:
  * owned by a single user (user_id)
  * holds master_level + per-channel levels + hidden flags
  * associated with an `aux_number` as the "last used" aux, but can be
    applied to any aux (the user may swap)

Schema:
    preset(id, user_id, name, aux_number, master_level, created_at, updated_at)
    preset_channel(preset_id, source_type, source_channel, level, hidden)

All DB access is wrapped in `asyncio.to_thread` since `sqlite3` is blocking.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("audiomix.presets")

SCHEMA = """
CREATE TABLE IF NOT EXISTS preset (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    aux_number INTEGER NOT NULL,
    master_level REAL NOT NULL DEFAULT 0.72,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_preset_user ON preset(user_id);

CREATE TABLE IF NOT EXISTS preset_channel (
    preset_id INTEGER NOT NULL REFERENCES preset(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL DEFAULT 'line',
    source_channel INTEGER NOT NULL,
    level REAL NOT NULL,
    hidden INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (preset_id, source_type, source_channel)
);
"""


@dataclass
class PresetChannel:
    source_type: str
    source_channel: int
    level: float
    hidden: bool = False


@dataclass
class Preset:
    id: int
    user_id: str
    name: str
    aux_number: int
    master_level: float
    created_at: str
    updated_at: str
    channels: list[PresetChannel] = field(default_factory=list)


class PresetRepo:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    async def init(self):
        await asyncio.to_thread(self._init_sync)

    def _init_sync(self):
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- CRUD ----

    async def list_for_user(self, user_id: str) -> list[Preset]:
        return await asyncio.to_thread(self._list_sync, user_id)

    def _list_sync(self, user_id: str) -> list[Preset]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM preset WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
            out: list[Preset] = []
            for r in rows:
                channels = self._read_channels(c, r["id"])
                out.append(self._row_to_preset(r, channels))
            return out

    async def get(self, user_id: str, preset_id: int) -> Optional[Preset]:
        return await asyncio.to_thread(self._get_sync, user_id, preset_id)

    def _get_sync(self, user_id: str, preset_id: int) -> Optional[Preset]:
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM preset WHERE id = ? AND user_id = ?",
                (preset_id, user_id),
            ).fetchone()
            if not r:
                return None
            channels = self._read_channels(c, preset_id)
            return self._row_to_preset(r, channels)

    async def create(
        self,
        user_id: str,
        name: str,
        aux_number: int,
        master_level: float,
        channels: list[PresetChannel],
    ) -> Preset:
        return await asyncio.to_thread(
            self._create_sync, user_id, name, aux_number, master_level, channels
        )

    def _create_sync(self, user_id, name, aux_number, master_level, channels) -> Preset:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO preset (user_id, name, aux_number, master_level, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, name, aux_number, master_level, now, now),
            )
            preset_id = cur.lastrowid
            self._replace_channels(c, preset_id, channels)
            return Preset(
                id=preset_id, user_id=user_id, name=name,
                aux_number=aux_number, master_level=master_level,
                created_at=now, updated_at=now, channels=list(channels),
            )

    async def update(
        self,
        user_id: str,
        preset_id: int,
        name: Optional[str] = None,
        aux_number: Optional[int] = None,
        master_level: Optional[float] = None,
        channels: Optional[list[PresetChannel]] = None,
    ) -> Optional[Preset]:
        return await asyncio.to_thread(
            self._update_sync, user_id, preset_id, name, aux_number, master_level, channels
        )

    def _update_sync(self, user_id, preset_id, name, aux_number, master_level, channels):
        with self._conn() as c:
            existing = c.execute(
                "SELECT * FROM preset WHERE id = ? AND user_id = ?",
                (preset_id, user_id),
            ).fetchone()
            if not existing:
                return None
            now = datetime.now(timezone.utc).isoformat()
            new_name = name if name is not None else existing["name"]
            new_aux = aux_number if aux_number is not None else existing["aux_number"]
            new_master = master_level if master_level is not None else existing["master_level"]
            c.execute(
                "UPDATE preset SET name = ?, aux_number = ?, master_level = ?, updated_at = ? "
                "WHERE id = ?",
                (new_name, new_aux, new_master, now, preset_id),
            )
            if channels is not None:
                self._replace_channels(c, preset_id, channels)
            # return fresh
            new_channels = self._read_channels(c, preset_id)
            return Preset(
                id=preset_id, user_id=user_id, name=new_name,
                aux_number=new_aux, master_level=new_master,
                created_at=existing["created_at"], updated_at=now,
                channels=new_channels,
            )

    async def delete(self, user_id: str, preset_id: int) -> bool:
        return await asyncio.to_thread(self._delete_sync, user_id, preset_id)

    def _delete_sync(self, user_id: str, preset_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM preset WHERE id = ? AND user_id = ?",
                (preset_id, user_id),
            )
            return cur.rowcount > 0

    # ---- helpers ----

    def _row_to_preset(self, r, channels) -> Preset:
        return Preset(
            id=r["id"], user_id=r["user_id"], name=r["name"],
            aux_number=r["aux_number"], master_level=r["master_level"],
            created_at=r["created_at"], updated_at=r["updated_at"],
            channels=channels,
        )

    def _read_channels(self, c, preset_id: int) -> list[PresetChannel]:
        rows = c.execute(
            "SELECT source_type, source_channel, level, hidden "
            "FROM preset_channel WHERE preset_id = ? "
            "ORDER BY source_channel",
            (preset_id,),
        ).fetchall()
        return [
            PresetChannel(
                source_type=r["source_type"],
                source_channel=r["source_channel"],
                level=r["level"],
                hidden=bool(r["hidden"]),
            )
            for r in rows
        ]

    def _replace_channels(self, c, preset_id: int, channels: list[PresetChannel]):
        c.execute("DELETE FROM preset_channel WHERE preset_id = ?", (preset_id,))
        c.executemany(
            "INSERT INTO preset_channel (preset_id, source_type, source_channel, level, hidden) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (preset_id, ch.source_type, ch.source_channel, ch.level, 1 if ch.hidden else 0)
                for ch in channels
            ],
        )
