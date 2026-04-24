"""
Aux locking — exactly one session per aux bus (1..16).

Design:
  * claim(user, aux, ip) creates or renews a session. If aux is held by
    a DIFFERENT user, returns AuxOccupied. Same user re-claiming is allowed
    (handles reconnects / page refresh / aux swap).
  * heartbeat(session_token) extends last_heartbeat.
  * release(session_token) removes lock.
  * sweeper task runs every 1s and removes sessions whose last_heartbeat
    is older than ttl_seconds (default 15s).

Locks are in-memory only. Audit of claims/releases is logged.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .config import SessionConfig
from .auth import MaestroUser

log = logging.getLogger("audiomix.sessions")


@dataclass
class AuxSession:
    session_token: str
    user_id: str
    user_name: str
    client_ip: str
    aux_number: int
    claimed_at: datetime
    last_heartbeat: float  # monotonic seconds


@dataclass
class AuxOccupied(Exception):
    holder_name: str
    holder_user_id: str
    since: datetime

    def __str__(self):
        return f"aux occupied by {self.holder_name}"


class SessionManager:
    def __init__(self, cfg: SessionConfig):
        self.cfg = cfg
        self._by_aux: dict[int, AuxSession] = {}
        self._by_token: dict[str, AuxSession] = {}
        self._lock = asyncio.Lock()
        self._sweeper: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def start(self):
        self._stop.clear()
        self._sweeper = asyncio.create_task(self._sweep_loop(), name="session-sweeper")

    async def stop(self):
        self._stop.set()
        if self._sweeper:
            self._sweeper.cancel()
            try:
                await self._sweeper
            except (asyncio.CancelledError, Exception):
                pass

    async def claim(self, user: MaestroUser, aux: int, client_ip: str) -> AuxSession:
        now = time.monotonic()
        async with self._lock:
            current = self._by_aux.get(aux)
            if current and current.user_id != user.id:
                # Hard lock — reject
                raise AuxOccupied(
                    holder_name=current.user_name,
                    holder_user_id=current.user_id,
                    since=current.claimed_at,
                )

            if current and current.user_id == user.id:
                # Same user re-claiming (e.g. page refresh). Rotate token anyway.
                self._by_token.pop(current.session_token, None)

            # Release any OTHER aux held by this user (one-aux-per-user rule).
            to_release = [
                a for a, s in self._by_aux.items()
                if s.user_id == user.id and a != aux
            ]
            for a in to_release:
                released = self._by_aux.pop(a)
                self._by_token.pop(released.session_token, None)
                log.info("auto-released aux %d from user %s (switched to aux %d)",
                         a, user.name, aux)

            token = secrets.token_hex(16)
            sess = AuxSession(
                session_token=token,
                user_id=user.id,
                user_name=user.name,
                client_ip=client_ip,
                aux_number=aux,
                claimed_at=datetime.now(timezone.utc),
                last_heartbeat=now,
            )
            self._by_aux[aux] = sess
            self._by_token[token] = sess
            log.info("claim aux=%d user=%s ip=%s", aux, user.name, client_ip)
            return sess

    async def heartbeat(self, session_token: str) -> Optional[AuxSession]:
        async with self._lock:
            sess = self._by_token.get(session_token)
            if not sess:
                return None
            sess.last_heartbeat = time.monotonic()
            return sess

    async def release(self, session_token: str) -> bool:
        async with self._lock:
            sess = self._by_token.pop(session_token, None)
            if not sess:
                return False
            if self._by_aux.get(sess.aux_number) is sess:
                self._by_aux.pop(sess.aux_number)
            log.info("release aux=%d user=%s", sess.aux_number, sess.user_name)
            return True

    def get_by_token(self, session_token: str) -> Optional[AuxSession]:
        return self._by_token.get(session_token)

    def get_by_aux(self, aux: int) -> Optional[AuxSession]:
        return self._by_aux.get(aux)

    def all_sessions(self) -> list[AuxSession]:
        return list(self._by_aux.values())

    def expires_in(self, sess: AuxSession) -> int:
        deadline = sess.last_heartbeat + self.cfg.ttl_seconds
        return max(0, int(deadline - time.monotonic()))

    async def force_release(self, aux: int) -> bool:
        async with self._lock:
            sess = self._by_aux.pop(aux, None)
            if not sess:
                return False
            self._by_token.pop(sess.session_token, None)
            log.warning("force-released aux %d (was %s)", aux, sess.user_name)
            return True

    async def _sweep_loop(self):
        try:
            while not self._stop.is_set():
                await asyncio.sleep(1.0)
                now = time.monotonic()
                expired: list[AuxSession] = []
                async with self._lock:
                    for sess in list(self._by_aux.values()):
                        if now - sess.last_heartbeat > self.cfg.ttl_seconds:
                            expired.append(sess)
                            self._by_aux.pop(sess.aux_number, None)
                            self._by_token.pop(sess.session_token, None)
                for sess in expired:
                    log.info("expired aux=%d user=%s (no heartbeat for %ds)",
                             sess.aux_number, sess.user_name, self.cfg.ttl_seconds)
        except asyncio.CancelledError:
            pass
