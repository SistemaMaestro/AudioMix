"""
Resilient wrapper around studiolive.StudioLiveClient.

Maintains a single long-lived connection to the mixer:
  * runs auto-discovery (UDP 47809) if config.mixer.host is empty
  * reconnects with exponential backoff (1s → 30s cap) on any disconnect
  * exposes current state dict + `connected` flag for API/admin
  * forwards command helpers (set_volume, set_mute, set_aux_send, ...)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from studiolive import StudioLiveClient, discover
from .config import MixerConfig

log = logging.getLogger("audiomix.mixer_link")


class MixerLink:
    def __init__(self, cfg: MixerConfig):
        self.cfg = cfg
        self._client: Optional[StudioLiveClient] = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.current_host: Optional[str] = cfg.host or None
        self.current_port: int = cfg.port
        self.mixer_name: Optional[str] = None
        self.mixer_serial: Optional[str] = None
        self.connect_failures: int = 0
        self.last_connected_at: Optional[float] = None
        self.last_error: Optional[str] = None

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.connected and self._client.synced.is_set()

    @property
    def state(self) -> dict[str, Any]:
        return self._client.state if self._client else {}

    async def start(self):
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_forever(), name="mixer-link")

    async def stop(self):
        self._stop.set()
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run_forever(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                host = await self._resolve_host()
                if not host:
                    self.last_error = "mixer not found on network"
                    await self._sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue

                log.info("connecting to mixer %s:%s", host, self.current_port)
                self._client = StudioLiveClient(host, self.current_port)
                await self._client.connect()
                await asyncio.wait_for(self._client.synced.wait(), timeout=10.0)

                # Read identity from state if available, otherwise keep discovery info.
                self.last_connected_at = asyncio.get_event_loop().time()
                self.connect_failures = 0
                self.last_error = None
                backoff = 1.0
                log.info("mixer synced — %d state keys", len(self._client.state))

                # Park until the socket dies (reader closes _closed event).
                await self._client._closed.wait()
                log.warning("mixer disconnected — will retry")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connect_failures += 1
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("mixer connect cycle failed: %s", self.last_error)
                # Force re-discovery on repeated failures when host was auto-resolved.
                if self.connect_failures >= 3 and not self.cfg.host:
                    self.current_host = None
            finally:
                if self._client:
                    try:
                        await self._client.close()
                    except Exception:
                        pass
                    self._client = None

            if self._stop.is_set():
                break
            await self._sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _sleep(self, seconds: float):
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _resolve_host(self) -> Optional[str]:
        if self.current_host:
            return self.current_host
        log.info("running UDP discovery (%.1fs)...", self.cfg.discovery_timeout_seconds)
        devices = await discover(timeout=self.cfg.discovery_timeout_seconds)
        # Prefer real console broadcast (is_console=True) over UC rebroadcasts.
        devices.sort(key=lambda d: (not d.get("is_console"), d.get("is_loopback")))
        if not devices:
            return None
        d = devices[0]
        self.current_host = d["ip"]
        self.mixer_name = d.get("name")
        self.mixer_serial = d.get("serial")
        log.info("discovered %s (%s) at %s", self.mixer_name, self.mixer_serial, d["ip"])
        return self.current_host

    # ---- command helpers (raise if not connected) ----

    def _require(self) -> StudioLiveClient:
        if not self.connected or self._client is None:
            raise RuntimeError("mixer offline")
        return self._client

    async def set_volume(self, ch_type: str, channel: int, level_0_100: float):
        await self._require().set_volume(ch_type, channel, level_0_100)

    async def set_mute(self, ch_type: str, channel: int, muted: bool):
        await self._require().set_mute(ch_type, channel, muted)

    async def set_aux_send(self, source_type: str, source_channel: int, aux: int, level_0_100: float):
        await self._require().set_aux_send(source_type, source_channel, aux, level_0_100)

    async def send_raw_pv(self, path: str, value: float):
        await self._require().send_raw_pv(path, value)

    def get(self, key: str, default=None):
        return self.state.get(key, default)
