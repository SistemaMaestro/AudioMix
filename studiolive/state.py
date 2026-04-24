"""
State tree helpers for StudioLive III.

The mixer's initial 'Synchronize' payload (ZB or chunked CK) deserialises to a
nested structure with 'children', 'values', 'strings', 'ranges' at each level.
We flatten it into a dict keyed by slash-separated path, e.g.:

    line/ch1/username   -> "Vocal"
    line/ch1/volume     -> 0.72
    line/ch1/mute       -> 0
    line/ch1/aux3       -> 0.45          (send level ch1 -> aux 3)
    aux/ch1/username    -> "Monitor 1"

PV/PS/PC packets received afterwards update the same flat dict.
"""
import zlib
from typing import Any
from . import ubjson


def parse_zb(raw: bytes) -> dict:
    """Parse a ZB-body (after the 4-byte prefix has been stripped).
    Deflates and UBJSON-decodes.
    """
    inflated = zlib.decompress(raw)
    return ubjson.deserialize(inflated)


def flatten(tree: dict) -> dict:
    """Collapse the Synchronize tree into {path: value} with '/'-separated paths.

    Walks `children` recursively, and emits every key in `values` at the current
    path. `ranges` and `strings` are ignored (metadata).
    """
    out: dict[str, Any] = {}

    def recur(node: dict, prefix: str):
        if not isinstance(node, dict):
            return
        values = node.get("values")
        if isinstance(values, dict):
            for k, v in values.items():
                out[f"{prefix}/{k}" if prefix else k] = v
        children = node.get("children")
        if isinstance(children, dict):
            for k, sub in children.items():
                recur(sub, f"{prefix}/{k}" if prefix else k)

    recur(tree, "")
    return out


class Chunker:
    """Accumulates CK packets and returns inflated UBJSON tree when complete.

    Each CK packet body (after the first 4 bytes stripped) is:
        u32 LE chunk_offset
        u32 LE total_size
        u32 LE chunk_size
        raw chunk bytes (compressed, concatenate then inflate)
    """
    def __init__(self):
        self._chunks: list[bytes] = []

    def push(self, body: bytes):
        import struct as _s
        body = body[4:]  # skip 4-byte identifier
        chunk_offset = _s.unpack_from("<I", body, 0)[0]
        total_size = _s.unpack_from("<I", body, 4)[0]
        chunk_size = _s.unpack_from("<I", body, 8)[0]
        self._chunks.append(body[12:])
        if chunk_offset + chunk_size >= total_size:
            full = b"".join(self._chunks)
            self._chunks = []
            return parse_zb(full)
        return None


def parse_pv_payload(data: bytes):
    """Return (path, raw_tail_bytes) from a PV packet body."""
    idx = data.find(b"\x00")
    if idx == -1:
        return None, None
    path = data[:idx].decode("ascii", errors="replace")
    # Skip key NUL + 2 partA bytes
    tail = data[idx + 3:]
    return path, tail


def parse_ps_payload(data: bytes):
    """Return (path, string_value) from a PS packet body."""
    idx = data.find(b"\x00")
    if idx == -1:
        return None, None
    path = data[:idx].decode("ascii", errors="replace")
    # Skip 3 bytes after NUL, drop trailing NUL
    value = data[idx + 3:].rstrip(b"\x00").decode("utf-8", errors="replace")
    return path, value
