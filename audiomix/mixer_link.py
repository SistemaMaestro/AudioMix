"""
Resilient wrapper around studiolive.StudioLiveClient.

Maintains a single long-lived connection to the mixer:
  * runs auto-discovery (UDP 47809) if config.mixer.host is empty
  * two separate backoff ladders, because the two failure modes differ:
      - mixer absent (powered off — the normal state outside a service):
        idle ladder, 15s → 2min cap. Gentle: nobody is waiting.
      - live connection dropped: reconnect ladder, 1s → 30s cap. Fast:
        someone is mixing right now.
  * exposes current state dict + `connected` flag for API/admin
  * forwards command helpers (set_volume, set_mute, set_aux_send, ...)
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
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
        self.discovery_misses: int = 0
        self.mixer_absent_since: Optional[float] = None
        self.next_retry_seconds: Optional[float] = None

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
        idle_backoff = self.cfg.idle_retry_min_seconds
        reconnect_backoff = self.cfg.reconnect_retry_min_seconds
        announced_idle: Optional[float] = None
        while not self._stop.is_set():
            try:
                host = await self._resolve_host()
                if not host:
                    self.last_error = "mixer not found on network"
                    # Só anuncia quando o intervalo muda — depois que a escada
                    # satura no teto, o journal fica em silêncio.
                    if idle_backoff != announced_idle:
                        log.info(
                            "mixer absent (%d scans) — next scan in %.0fs",
                            self.discovery_misses,
                            idle_backoff,
                        )
                        announced_idle = idle_backoff
                    await self._sleep(idle_backoff)
                    idle_backoff = min(idle_backoff * 2, self.cfg.idle_retry_max_seconds)
                    continue

                announced_idle = None
                log.info("connecting to mixer %s:%s", host, self.current_port)
                self._client = StudioLiveClient(host, self.current_port)
                await self._client.connect()
                await asyncio.wait_for(self._client.synced.wait(), timeout=10.0)

                # Read identity from state if available, otherwise keep discovery info.
                self.last_connected_at = asyncio.get_event_loop().time()
                self.connect_failures = 0
                self.last_error = None
                idle_backoff = self.cfg.idle_retry_min_seconds
                log.info("mixer synced — %d state keys", len(self._client.state))

                # Park until the socket dies (reader closes _closed event).
                held_from = time.monotonic()
                await self._client._closed.wait()
                held = time.monotonic() - held_from
                # Só zera a escada rápida se a conexão se sustentou. Mesa que
                # conecta e cai em seguida (flap) continua desacelerando, em vez
                # de virar reconexão a 1 Hz para sempre.
                if held >= self.cfg.stable_connection_seconds:
                    reconnect_backoff = self.cfg.reconnect_retry_min_seconds
                log.warning("mixer disconnected after %.0fs — will retry", held)
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
            await self._sleep(reconnect_backoff)
            reconnect_backoff = min(
                reconnect_backoff * 2, self.cfg.reconnect_retry_max_seconds
            )

    async def _sleep(self, seconds: float):
        seconds = self._jittered(seconds)
        self.next_retry_seconds = seconds
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        finally:
            self.next_retry_seconds = None

    def _jittered(self, seconds: float) -> float:
        pct = max(0.0, min(self.cfg.retry_jitter_pct, 0.5))
        if pct <= 0.0:
            return seconds
        return seconds * (1.0 + random.uniform(-pct, pct))

    async def _resolve_host(self) -> Optional[str]:
        if self.current_host:
            return self.current_host
        # A 1ª varredura de cada ausência vai a INFO; as repetições caem para
        # DEBUG — com a mesa desligada isso rodaria para sempre.
        log.log(
            logging.INFO if self.discovery_misses == 0 else logging.DEBUG,
            "running UDP discovery (%.1fs)...",
            self.cfg.discovery_timeout_seconds,
        )
        devices = await discover(timeout=self.cfg.discovery_timeout_seconds)
        # Prefer real console broadcast (is_console=True) over UC rebroadcasts.
        devices.sort(key=lambda d: (not d.get("is_console"), d.get("is_loopback")))
        if not devices:
            self.discovery_misses += 1
            if self.mixer_absent_since is None:
                self.mixer_absent_since = time.time()
            return None
        d = devices[0]
        self.current_host = d["ip"]
        self.mixer_name = d.get("name")
        self.mixer_serial = d.get("serial")
        if self.mixer_absent_since is not None:
            log.info(
                "mixer back after %.0fs absent (%d scans)",
                time.time() - self.mixer_absent_since,
                self.discovery_misses,
            )
        self.discovery_misses = 0
        self.mixer_absent_since = None
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
