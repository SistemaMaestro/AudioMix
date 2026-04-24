"""
Asyncio TCP client for StudioLive III (UCNET, port 53000).

Maintains a flat state dict {path: value} populated from:
  - ZB packets (inflated UBJSON 'Synchronize' tree)
  - CK packets (chunked ZB payload)
  - PS packets (string values, e.g. channel names)
  - PV packets (float/boolean values, e.g. fader, mute, aux sends)
"""
import asyncio
import logging
import struct
from typing import Optional, Any
from . import protocol, state as state_mod

log = logging.getLogger("studiolive")


class StudioLiveClient:
    def __init__(self, host: str, port: int = protocol.CONTROL_PORT):
        self.host = host
        self.port = port
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self._ka_task: Optional[asyncio.Task] = None
        self._rx_task: Optional[asyncio.Task] = None
        self._closed = asyncio.Event()
        self.last_codes: list[str] = []
        self.state: dict[str, Any] = {}
        self._chunker = state_mod.Chunker()
        self.synced = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self.writer is not None and not self.writer.is_closing()

    async def connect(self):
        log.info("connecting to %s:%s", self.host, self.port)
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        self.writer.write(protocol.subscribe_packet())
        await self.writer.drain()
        self._rx_task = asyncio.create_task(self._rx_loop(), name="sl-rx")
        self._ka_task = asyncio.create_task(self._ka_loop(), name="sl-ka")

    async def _rx_loop(self):
        buf = b""
        try:
            while not self._closed.is_set():
                chunk = await self.reader.read(16384)
                if not chunk:
                    if not self._closed.is_set():
                        log.warning("socket closed by remote")
                    break
                buf += chunk
                pkts, buf = protocol.drain_packets(buf)
                for code, data in pkts:
                    self._track_code(code)
                    try:
                        self._handle(code, data)
                    except Exception:
                        log.exception("error handling %s", code)
        except (asyncio.CancelledError, ConnectionError):
            pass
        finally:
            self._closed.set()

    def _track_code(self, code):
        self.last_codes.append(code or "??")
        if len(self.last_codes) > 50:
            self.last_codes = self.last_codes[-50:]

    def _handle(self, code: str, data: bytes):
        if code == "ZB":
            tree = state_mod.parse_zb(data[4:])
            self._ingest_tree(tree)
            self.synced.set()
        elif code == "CK":
            tree = self._chunker.push(data)
            if tree is not None:
                self._ingest_tree(tree)
                self.synced.set()
        elif code == "PS":
            path, value = state_mod.parse_ps_payload(data)
            if path:
                self.state[path] = value
        elif code == "PC":
            path, tail = state_mod.parse_pv_payload(data)
            if path:
                # PC is usually hex-encodable bytes (e.g. colors). Store raw for now.
                self.state[path] = tail.hex()
        elif code == "PV":
            path, tail = state_mod.parse_pv_payload(data)
            if path and len(tail) >= 4:
                try:
                    f = struct.unpack("<f", tail[:4])[0]
                except struct.error:
                    return
                self.state[path] = f
        # other codes (JM, BO, PL, MS, FD) ignored for now

    def _ingest_tree(self, tree: dict):
        flat = state_mod.flatten(tree)
        self.state.update(flat)

    async def _ka_loop(self):
        import random
        try:
            while not self._closed.is_set():
                await asyncio.sleep(1.0)
                if not (self.writer and not self.writer.is_closing()):
                    break
                try:
                    self.writer.write(protocol.keepalive_packet())
                    self.writer.write(protocol.ftbr_probe_packet(random.randint(1, 0xFFFF)))
                    await self.writer.drain()
                except ConnectionError:
                    break
        except asyncio.CancelledError:
            pass

    async def close(self):
        self._closed.set()
        if self.writer:
            try:
                self.writer.write(protocol.unsubscribe_packet())
                await self.writer.drain()
            except Exception:
                pass
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
        for t in (self._ka_task, self._rx_task):
            if t:
                t.cancel()

    # ---- high-level commands ----

    async def _send(self, pkt: bytes):
        if not self.connected:
            raise RuntimeError("not connected")
        self.writer.write(pkt)
        await self.writer.drain()

    async def set_volume(self, ch_type: str, channel: int, level_0_100: float):
        await self._send(protocol.set_volume_packet(ch_type, channel, level_0_100))

    async def set_mute(self, ch_type: str, channel: int, muted: bool):
        await self._send(protocol.set_mute_packet(ch_type, channel, muted))

    async def set_aux_send(self, source_type: str, source_channel: int, aux_number: int, level_0_100: float):
        """Send level of source channel into aux mix <aux_number>.
        Path: <source_type>/ch<N>/aux<M>   (float 0..1)
        """
        path = f"{source_type}/ch{int(source_channel)}/aux{int(aux_number)}"
        level = max(0.0, min(100.0, level_0_100)) / 100.0
        await self._send(protocol.pv_float_packet(path, level))

    async def send_raw_pv(self, path: str, value: float):
        await self._send(protocol.pv_float_packet(path, value))
