"""
mDNS / Bonjour advertisement: register `audiomix.local` on the LAN so the PWA
can find the server without hard-coding an IP.

Uses python-zeroconf. iOS + modern Android resolve `.local` hostnames natively
via mDNS/mDNSResponder.
"""
from __future__ import annotations

import asyncio
import logging
import socket
from typing import Optional

from zeroconf import ServiceInfo
from zeroconf._exceptions import NonUniqueNameException
from zeroconf.asyncio import AsyncZeroconf

from .config import MdnsConfig

log = logging.getLogger("audiomix.mdns")


def _lan_ip() -> str:
    """Best-effort detection of the outbound LAN IP (not 127.0.0.1)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class MdnsAdvertiser:
    def __init__(self, cfg: MdnsConfig, port: int, version: str):
        self.cfg = cfg
        self.port = port
        self.version = version
        self._zc: Optional[AsyncZeroconf] = None
        self._info: Optional[ServiceInfo] = None

    async def start(self):
        if not self.cfg.enabled:
            log.info("mDNS disabled")
            return
        ip = _lan_ip()
        self._info = ServiceInfo(
            type_=self.cfg.service_type,
            name=f"{self.cfg.instance_name}.{self.cfg.service_type}",
            addresses=[socket.inet_aton(ip)],
            port=self.port,
            server=self.cfg.hostname,
            properties={
                "version": self.version,
                "service": "audiomix",
            },
        )
        # Retry loop: a previous instance may have left a stale record if its
        # goodbye packet hadn't finished propagating when it was killed.
        for attempt in range(3):
            self._zc = AsyncZeroconf()
            try:
                await self._zc.async_register_service(self._info)
                break
            except NonUniqueNameException:
                await self._zc.async_close()
                self._zc = None
                if attempt < 2:
                    wait = (attempt + 1) * 3
                    log.warning(
                        "mDNS: name conflict (stale record?), retrying in %ds...", wait
                    )
                    await asyncio.sleep(wait)
                else:
                    raise
        log.info("mDNS registered: %s -> %s:%s", self.cfg.hostname, ip, self.port)

    async def stop(self):
        if self._zc and self._info:
            try:
                await self._zc.async_unregister_service(self._info)
            except Exception:
                pass
            await self._zc.async_close()
            self._zc = None
            self._info = None
            log.info("mDNS unregistered")
