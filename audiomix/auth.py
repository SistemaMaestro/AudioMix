"""
Validate Maestro tokens by calling `POST {base_url}/Auth/verificar-token`.

The Maestro API returns:
    { "sucesso": true, "token": "...", "usuario": {"id","nome","email",...} }
    { "sucesso": false, "mensagem": "Token inválido" }  (HTTP 401)

We cache successful validations for `token_cache_ttl_seconds` to avoid hammering
the Maestro backend on every heartbeat.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from .config import MaestroConfig

log = logging.getLogger("audiomix.auth")


@dataclass(frozen=True)
class MaestroUser:
    id: str
    name: str
    email: Optional[str] = None


@dataclass
class _CacheEntry:
    user: MaestroUser
    expires_at: float


def _mask_token(token: str) -> str:
    if len(token) <= 12:
        return "tok_***"
    return f"tok_{token[:4]}...{token[-4:]}"


class MaestroAuth:
    def __init__(self, cfg: MaestroConfig):
        self.cfg = cfg
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()
        self._client: Optional[httpx.AsyncClient] = None

    async def start(self):
        self._client = httpx.AsyncClient(
            base_url=self.cfg.base_url,
            timeout=self.cfg.request_timeout_seconds,
        )

    async def stop(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def verify(self, token: str) -> Optional[MaestroUser]:
        """Return the authenticated user or None if token is invalid/expired."""
        if not token or not isinstance(token, str):
            return None

        now = time.time()
        cached = self._cache.get(token)
        if cached and cached.expires_at > now:
            return cached.user

        # Single-flight: don't launch 20 parallel verifications for the same token.
        async with self._lock:
            # Re-check after acquiring lock
            cached = self._cache.get(token)
            if cached and cached.expires_at > now:
                return cached.user

            user = await self._call_maestro(token)
            if user:
                self._cache[token] = _CacheEntry(
                    user=user,
                    expires_at=now + self.cfg.token_cache_ttl_seconds,
                )
            return user

    async def _call_maestro(self, token: str) -> Optional[MaestroUser]:
        assert self._client is not None, "call start() before verify()"
        try:
            resp = await self._client.post(
                self.cfg.verify_token_path,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as e:
            log.warning("maestro unreachable while verifying %s: %s", _mask_token(token), e)
            return None

        if resp.status_code == 401:
            log.info("maestro rejected %s", _mask_token(token))
            return None
        if resp.status_code != 200:
            log.warning("maestro returned %s for %s", resp.status_code, _mask_token(token))
            return None

        try:
            body = resp.json()
        except ValueError:
            log.warning("maestro returned non-JSON for %s", _mask_token(token))
            return None

        if not body.get("sucesso"):
            return None
        u = body.get("usuario") or {}
        uid = u.get("id")
        nome = u.get("nome")
        if not uid or not nome:
            log.warning("maestro missing id/nome fields for %s", _mask_token(token))
            return None
        return MaestroUser(id=str(uid), name=str(nome), email=u.get("email"))

    def invalidate(self, token: str):
        self._cache.pop(token, None)
