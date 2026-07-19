"""
Admin authorization for AudioMix — QR pairing against a Maestro identity.

Reading the dashboard is open; **writing** (editing an aux mix, kicking a
holder) requires an admin session. Logging in never disturbs the operator's
Maestro session: the Maestro PWA hands its EXISTING token to AudioMix, which
verifies it via `MaestroAuth.verify` (a read-only check — no new Maestro login,
no token rotation) and then mints its OWN token. Exactly one admin session is
active per installation; a new pairing supersedes the previous one. The session
is persisted on the RPi so it survives a service restart within its 24h life.

Pairing flow (WhatsApp-Web style):
  1. notebook: POST /admin/auth/pair/new            -> {pid, nonce}
  2. notebook renders a QR of <origin>/admin/pair?pid=..&k=nonce and polls
  3. phone (PWA): POST /admin/auth/pair/{pid}/claim  {token, nonce}
  4. AudioMix verifies the token, mints the admin token, marks pairing authorized
  5. notebook: GET /admin/auth/pair/{pid} -> Set-Cookie admin token, unlocks edit
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from .auth import MaestroAuth

log = logging.getLogger("audiomix.admin_auth")

COOKIE_NAME = "audiomix_admin"
PAIR_TTL = 120            # seconds a pairing QR stays valid
ADMIN_TTL = 24 * 3600     # seconds an admin session lasts


@dataclass
class _Pairing:
    nonce: str
    expires_at: float
    status: str = "pending"      # pending -> authorized -> used
    user_name: str = ""
    admin_token: str = ""


@dataclass
class _AdminSession:
    token: str
    user_id: str
    user_name: str
    issued_at: float
    expires_at: float


class AdminAuth:
    def __init__(
        self,
        auth: MaestroAuth,
        storage_dir: Path,
        allowed_user_ids: Optional[set[str]] = None,
    ):
        self._auth = auth
        self._file = Path(storage_dir) / "admin_session.json"
        # empty set = any valid Maestro user may operate this installation
        self._allowed = allowed_user_ids or set()
        self._pairings: dict[str, _Pairing] = {}
        self._session: Optional[_AdminSession] = None
        self._lock = asyncio.Lock()
        self._load()

    # ---------------- persistence ----------------

    def _load(self):
        try:
            data = json.loads(self._file.read_text("utf-8"))
            s = _AdminSession(**data)
            if s.expires_at > time.time():
                self._session = s
                log.info(
                    "restored admin session for %s (%dh left)",
                    s.user_name, int((s.expires_at - time.time()) / 3600),
                )
        except (FileNotFoundError, ValueError, TypeError):
            self._session = None

    def _save(self):
        try:
            if self._session:
                self._file.write_text(json.dumps(asdict(self._session)), "utf-8")
            elif self._file.exists():
                self._file.unlink()
        except OSError as e:
            log.warning("could not persist admin session: %s", e)

    def _prune(self):
        now = time.time()
        self._pairings = {
            k: v for k, v in self._pairings.items() if v.expires_at > now
        }

    # ---------------- pairing ----------------

    def new_pairing(self) -> tuple[str, str, int]:
        self._prune()
        pid = secrets.token_urlsafe(9)
        nonce = secrets.token_urlsafe(16)
        self._pairings[pid] = _Pairing(nonce=nonce, expires_at=time.time() + PAIR_TTL)
        return pid, nonce, PAIR_TTL

    async def claim_pairing(self, pid: str, nonce: str, maestro_token: str) -> dict:
        """Phone side: prove identity with an existing Maestro token."""
        self._prune()
        p = self._pairings.get(pid)
        if not p or p.status != "pending":
            return {"ok": False, "reason": "NOT_FOUND"}
        if not nonce or not secrets.compare_digest(p.nonce, nonce):
            return {"ok": False, "reason": "FORBIDDEN"}
        user = await self._auth.verify(maestro_token)
        if not user:
            return {"ok": False, "reason": "INVALID_TOKEN"}
        if self._allowed and str(user.id) not in self._allowed:
            return {"ok": False, "reason": "FORBIDDEN"}
        async with self._lock:
            token = secrets.token_urlsafe(32)
            now = time.time()
            self._session = _AdminSession(
                token=token,
                user_id=str(user.id),
                user_name=user.name,
                issued_at=now,
                expires_at=now + ADMIN_TTL,
            )
            self._save()
            p.status = "authorized"
            p.user_name = user.name
            p.admin_token = token
        log.info("admin paired: %s — edição liberada por 24h", user.name)
        return {"ok": True, "user_name": user.name}

    def poll_pairing(self, pid: str) -> Optional[_Pairing]:
        self._prune()
        return self._pairings.get(pid)

    def consume_pairing(self, pid: str) -> Optional[str]:
        """Notebook side: fetch the admin token once, then burn the pairing."""
        p = self._pairings.get(pid)
        if p and p.status == "authorized":
            p.status = "used"
            return p.admin_token
        return None

    # ---------------- session ----------------

    def validate(self, token: Optional[str]) -> Optional[_AdminSession]:
        if not token or not self._session:
            return None
        if self._session.expires_at <= time.time():
            self._session = None
            self._save()
            return None
        if not secrets.compare_digest(self._session.token, token):
            return None
        return self._session

    def logout(self, token: Optional[str]):
        if self._session and token and secrets.compare_digest(self._session.token, token):
            self._session = None
            self._save()
            log.info("admin session encerrada")
